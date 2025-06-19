import asyncio
import json
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import config
from utils.logger import Logger
from pathlib import Path

logger = Logger(f"{__name__}")

START_CMD = """🚀 **Welcome To TG Drive's Bot Mode**

You can use this bot to upload files to your TG Drive website directly instead of doing it from website.

🗄 **Commands:**
/set_folder - Set folder for file uploads
/current_folder - Check current folder

📤 **How To Upload Files:** Send a file to this bot and it will be uploaded to your TG Drive website. You can also set a folder for file uploads using /set_folder command.

Read more about [TG Drive's Bot Mode](https://github.com/TechShreyash/TGDrive#tg-drives-bot-mode)
"""

SET_FOLDER_PATH_CACHE = {}
DRIVE_DATA = None
BOT_MODE = None 

session_cache_path = Path(f"./cache")
session_cache_path.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_FOLDER_CONFIG_FILE = Path("./default_folder_config.json")

main_bot = Client(
    name="main_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.MAIN_BOT_TOKEN,
    sleep_threshold=config.SLEEP_THRESHOLD,
    workdir=session_cache_path,
)

# --- Manual 'ask' implementation setup ---
# Stores {chat_id: (asyncio.Queue, asyncio.Event, pyrogram.filters)}
_pending_requests = {}

async def manual_ask(client: Client, chat_id: int, text: str, timeout: int = 60, filters=None) -> Message:
    """
    A manual implementation of the 'ask' functionality for older Pyrogram versions.
    Sends a message and waits for a response from the specified chat_id.
    """
    queue = asyncio.Queue(1)
    event = asyncio.Event()
    
    _pending_requests[chat_id] = (queue, event, filters)

    await client.send_message(chat_id, text)

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        response_message = await queue.get()
        return response_message
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError # Re-raise if timed out
    finally:
        # Clean up the pending request regardless of outcome
        if chat_id in _pending_requests:
            del _pending_requests[chat_id]

@main_bot.on_message(filters.private & filters.user(config.TELEGRAM_ADMIN_IDS) & filters.text)
async def _handle_all_messages(client: Client, message: Message):
    """
    This handler listens for all private text messages from authorized users.
    If a pending 'ask' request exists for this chat, it fulfills it.
    """
    chat_id = message.chat.id
    if chat_id in _pending_requests:
        queue, event, msg_filters = _pending_requests[chat_id]

        # Check if the message matches the expected filters
        # Note: This is a simplified filter check. Pyrogram's internal filters are more robust.
        # For 'filters.text', this check is usually redundant if the main handler already uses filters.text.
        # If more complex filters (e.g., filters.photo) were needed for 'ask', this would need expansion.
        if msg_filters is None or msg_filters(None, message): # Simplistic filter check
            await queue.put(message)
            event.set() # Signal that a response has been received
        else:
            # If the message doesn't match the expected filter, ignore it for the 'ask' context
            logger.debug(f"Message from {chat_id} did not match pending ask filter. Ignoring for ask context.")
    # If no pending request, let other handlers process the message normally.
# --- End Manual 'ask' implementation setup ---


@main_bot.on_message(
    filters.command(["start", "help"])
    & filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS),
)
async def start_handler(client: Client, message: Message):
    """
    Handles the /start and /help commands, sending the welcome message.
    """
    await message.reply_text(START_CMD)


@main_bot.on_message(
    filters.command("set_folder")
    & filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS),
)
async def set_folder_handler(client: Client, message: Message):
    """
    Handles the /set_folder command.
    Prompts the user for a folder name, searches for it, and presents a list
    of found folders for selection via inline buttons.
    Uses the manual_ask() function.
    """
    global SET_FOLDER_PATH_CACHE, DRIVE_DATA

    while True:
        try:
            # --- MODIFICATION HERE: Using manual_ask() ---
            folder_name_input = await manual_ask(
                client=client,
                chat_id=message.chat.id,
                text="Send the folder name where you want to upload files\n\n/cancel to cancel",
                timeout=60,
                filters=filters.text, # Pass filters to manual_ask if needed, though handled generically in _handle_all_messages
            )
            # --- END MODIFICATION ---
        except asyncio.TimeoutError:
            await message.reply_text("Timeout\n\nUse /set_folder to set folder again")
            return

        if folder_name_input.text.lower() == "/cancel":
            await message.reply_text("Cancelled")
            return

        folder_name = folder_name_input.text.strip()
        search_result = DRIVE_DATA.search_file_folder(folder_name)

        folders = {}
        for item in search_result.values():
            if item.type == "folder":
                folders[item.id] = item

        if len(folders) == 0:
            await message.reply_text(f"No Folder found with name '{folder_name}'")
        else:
            break

    buttons = []
    folder_cache = {}
    folder_cache_id = len(SET_FOLDER_PATH_CACHE) + 1

    for folder in folders.values():
        path_segments = [seg for seg in folder.path.strip("/").split("/") if seg]
        folder_path = "/" + ("/".join(path_segments + [folder.id]))
        
        folder_cache[folder.id] = (folder_path, folder.name)
        buttons.append(
            [
                InlineKeyboardButton(
                    folder.name,
                    callback_data=f"set_folder_{folder_cache_id}_{folder.id}",
                )
            ]
        )
    SET_FOLDER_PATH_CACHE[folder_cache_id] = folder_cache

    await message.reply_text(
        "Select the folder where you want to upload files",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@main_bot.on_callback_query(
    filters.user(config.TELEGRAM_ADMIN_IDS) & filters.regex(r"set_folder_")
)
async def set_folder_callback(client: Client, callback_query: Message):
    """
    Handles the callback query when a user selects a folder from the inline buttons.
    Sets the selected folder as the current default and saves it to a config file.
    """
    global SET_FOLDER_PATH_CACHE, BOT_MODE

    folder_cache_id_str, folder_id = callback_query.data.split("_")[2:]
    folder_cache_id = int(folder_cache_id_str)

    folder_path_cache = SET_FOLDER_PATH_CACHE.get(folder_cache_id)
    if folder_path_cache is None:
        await callback_query.answer("Request Expired, Send /set_folder again")
        await callback_query.message.delete()
        return

    folder_path, name = folder_path_cache.get(folder_id)
    if folder_path is None:
        await callback_query.answer("Selected folder not found in cache. Please try again.")
        await callback_query.message.delete()
        return

    del SET_FOLDER_PATH_CACHE[folder_cache_id]

    BOT_MODE.set_folder(folder_path, name)

    try:
        with open(DEFAULT_FOLDER_CONFIG_FILE, "w") as f:
            json.dump({"current_folder": folder_path, "current_folder_name": name}, f)
        logger.info(f"Saved default folder to config: {name} -> {folder_path}")
    except Exception as e:
        logger.error(f"Failed to save default folder config: {e}")

    await callback_query.answer(f"Folder Set Successfully To : {name}")
    await callback_query.message.edit(
        f"Folder Set Successfully To : {name}\n\nNow you can send / forward files to me and it will be uploaded to this folder."
    )


@main_bot.on_message(
    filters.command("current_folder")
    & filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS),
)
async def current_folder_handler(client: Client, message: Message):
    """
    Handles the /current_folder command, displaying the currently set default folder.
    """
    global BOT_MODE

    await message.reply_text(f"Current Folder: {BOT_MODE.current_folder_name}")


@main_bot.on_message(
    filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS)
    & (
        filters.document
        | filters.video
        | filters.audio
        | filters.photo
        | filters.sticker
    )
)
async def file_handler(client: Client, message: Message):
    """
    Handles incoming file messages (documents, videos, audio, photos, stickers).
    Uploads the file to the currently set default folder.
    """
    global BOT_MODE, DRIVE_DATA

    if not BOT_MODE.current_folder:
        await message.reply_text(
            "Error: No default folder set. Please use /set_folder to set one before uploading files."
        )
        return

    copied_message = await message.copy(config.STORAGE_CHANNEL)
    file = (
        copied_message.document
        or copied_message.video
        or copied_message.audio
        or copied_message.photo
        or copied_message.sticker
    )

    DRIVE_DATA.new_file(
        BOT_MODE.current_folder,
        file.file_name,
        copied_message.id,
        file.file_size,
    )

    await message.reply_text(
        f"""✅ File Uploaded Successfully To Your TG Drive Website
                             
**File Name:** {file.file_name}
**Folder:** {BOT_MODE.current_folder_name}
"""
    )


async def start_bot_mode(d, b):
    """
    Initializes the bot mode, starts the main bot client, and sets the initial
    default folder based on saved configuration or falls back to 'grammar'.
    """
    global DRIVE_DATA, BOT_MODE
    DRIVE_DATA = d
    BOT_MODE = b

    logger.info("Starting Main Bot")
    await main_bot.start()

    default_folder_path = None
    default_folder_name_to_use = None

    if DEFAULT_FOLDER_CONFIG_FILE.exists():
        try:
            with open(DEFAULT_FOLDER_CONFIG_FILE, "r") as f:
                config_data = json.load(f)
                default_folder_path = config_data.get("current_folder")
                default_folder_name_to_use = config_data.get("current_folder_name")
            if default_folder_path and default_folder_name_to_use:
                logger.info(f"Loaded default folder from config: {default_folder_name_to_use} -> {default_folder_path}")
            else:
                logger.warning("Default folder config file found but data is incomplete. Falling back to 'grammar'.")
                default_folder_path = None
                default_folder_name_to_use = None
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error reading default folder config file: {e}. Falling back to 'grammar'.")
            default_folder_path = None
            default_folder_name_to_use = None

    if default_folder_path and default_folder_name_to_use:
        BOT_MODE.set_folder(default_folder_path, default_folder_name_to_use)
        message_to_send = f"Main Bot Started -> TG Drive's Bot Mode Enabled with previously set folder: {default_folder_name_to_use}"
    else:
        hardcoded_default_folder_name = "grammar"
        search_result = DRIVE_DATA.search_file_folder(hardcoded_default_folder_name)
        found_grammar = False
        for item in search_result.values():
            if item.type == "folder":
                path_segments = [seg for seg in item.path.strip("/").split("/") if seg]
                folder_path = "/" + ("/".join(path_segments + [item.id]))
                
                BOT_MODE.set_folder(folder_path, item.name)
                logger.info(f"Default folder set to: {item.name} -> {folder_path}")
                try:
                    with open(DEFAULT_FOLDER_CONFIG_FILE, "w") as f:
                        json.dump({"current_folder": folder_path, "current_folder_name": item.name}, f)
                    logger.info(f"Saved initial 'grammar' default folder to config.")
                except Exception as e:
                    logger.error(f"Failed to save initial default folder config: {e}")
                found_grammar = True
                break
        if not found_grammar:
            logger.warning(f"No folder found with name '{hardcoded_default_folder_name}'. No default folder set initially.")
            BOT_MODE.set_folder(None, "No default folder set. Please use /set_folder.") 
            message_to_send = "Main Bot Started -> TG Drive's Bot Mode Enabled. No 'grammar' folder found, please use /set_folder to choose one."

        else:
            message_to_send = "Main Bot Started -> TG Drive's Bot Mode Enabled with default folder Grammar"

    await main_bot.send_message(
        config.STORAGE_CHANNEL,
        message_to_send,
    )
    logger.info(message_to_send)
    
