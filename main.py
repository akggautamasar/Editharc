import asyncio
import gc
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form, Response
from fastapi.responses import FileResponse, JSONResponse

from config import ADMIN_PASSWORD, MAX_FILE_SIZE, STORAGE_CHANNEL
from utils.clients import initialize_clients  # We'll call it lazily
from utils.directoryHandler import getRandomID
from utils.downloader import download_file, get_file_info_from_url
from utils.extra import auto_ping_website, convert_class_to_dict, reset_cache_dir
from utils.logger import Logger
from utils.streamer import media_streamer
from utils.uploader import start_file_uploader

# Global clients cache (lazy init)
_clients = None


async def get_clients():
    """Lazy initialization of Telegram clients to save memory at startup."""
    global _clients
    if _clients is None:
        try:
            logger.info("Initializing Telegram clients (lazy)...")
            _clients = await initialize_clients()
            logger.info("Telegram clients initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize clients: {e}", exc_info=True)
            raise
    return _clients


# ====================== Lifespan ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Memory optimization
    gc.enable()
    gc.collect()

    try:
        reset_cache_dir()
        logger.info("Cache directory reset")

        # Start background task
        asyncio.create_task(auto_ping_website())
        logger.info("Auto-ping task started")

        # Note: We do NOT initialize clients here anymore (lazy instead)

    except Exception as e:
        logger.error(f"Lifespan startup error: {e}", exc_info=True)
        raise

    yield  # App runs here

    # Shutdown cleanup
    logger.info("Shutting down...")
    if _clients is not None:
        try:
            # Add any client cleanup if your initialize_clients returns objects with .stop() or similar
            pass
        except Exception as e:
            logger.warning(f"Error during client cleanup: {e}")


# ====================== App ======================
app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
logger = Logger(__name__)


# ====================== Routes ======================
@app.get("/")
async def home_page():
    return FileResponse("website/home.html")


@app.get("/stream")
async def stream_page():
    return FileResponse("website/VideoPlayer.html")


@app.get("/static/{file_path:path}")
async def static_files(file_path: str):
    if "apiHandler.js" in file_path:
        try:
            with open(Path("website/static/js/apiHandler.js")) as f:
                content = f.read()
                content = content.replace("MAX_FILE_SIZE__SDGJDG", str(MAX_FILE_SIZE))
            return Response(content=content, media_type="application/javascript")
        except Exception as e:
            logger.error(f"Error serving apiHandler.js: {e}")
            raise HTTPException(status_code=500, detail="Static file error")
    return FileResponse(f"website/static/{file_path}")


@app.get("/file")
async def dl_file(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    path = request.query_params.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="Missing path")

    file = DRIVE_DATA.get_file(path)
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    return await media_streamer(STORAGE_CHANNEL, file.file_id, file.name, request)


# ====================== API Routes ======================
@app.post("/api/checkPassword")
async def check_password(request: Request):
    data = await request.json()
    return JSONResponse({"status": "ok" if data.get("pass") == ADMIN_PASSWORD else "Invalid password"})


@app.post("/api/createNewFolder")
async def api_new_folder(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()
    if data.get("password") != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    logger.info(f"createNewFolder {data}")
    # ... (rest of your logic remains same)
    folder_data = DRIVE_DATA.get_directory(data["path"]).contents
    for f in folder_data.values():
        if f.type == "folder" and f.name == data["name"]:
            return JSONResponse({"status": "Folder with the name already exist in current directory"})

    DRIVE_DATA.new_folder(data["path"], data["name"])
    return JSONResponse({"status": "ok"})


@app.post("/api/getDirectory")
async def api_get_directory(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()
    is_admin = data.get("password") == ADMIN_PASSWORD
    auth = data.get("auth")

    logger.info(f"getDirectory {data}")

    if data["path"] == "/trash":
        contents = DRIVE_DATA.get_trashed_files_folders()
        folder_data = convert_class_to_dict({"contents": contents}, isObject=False, showtrash=True)

    elif "/search_" in data["path"]:
        query = urllib.parse.unquote(data["path"].split("_", 1)[1])
        contents = DRIVE_DATA.search_file_folder(query)
        folder_data = convert_class_to_dict({"contents": contents}, isObject=False, showtrash=False)

    elif "/share_" in data["path"]:
        path = data["path"].split("_", 1)[1]
        folder_obj, auth_home_path = DRIVE_DATA.get_directory(path, is_admin, auth)
        auth_home_path = auth_home_path.replace("//", "/") if auth_home_path else None
        folder_data = convert_class_to_dict(folder_obj, isObject=True, showtrash=False)
        return JSONResponse({
            "status": "ok",
            "data": folder_data,
            "auth_home_path": auth_home_path
        })

    else:
        folder_obj = DRIVE_DATA.get_directory(data["path"])
        folder_data = convert_class_to_dict(folder_obj, isObject=True, showtrash=False)

    return JSONResponse({"status": "ok", "data": folder_data, "auth_home_path": None})


SAVE_PROGRESS = {}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form(...),
    password: str = Form(...),
    id: str = Form(...),
    total_size: str = Form(...),
):
    global SAVE_PROGRESS

    if password != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    total_size = int(total_size)
    SAVE_PROGRESS[id] = ("running", 0, total_size)

    ext = Path(file.filename).suffix.lower().lstrip(".")

    cache_dir = Path("./cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    file_location = cache_dir / f"{id}.{ext}"

    file_size = 0
    chunk_size = 1024 * 1024  # 1MB chunks (good balance)

    try:
        async with aiofiles.open(file_location, "wb") as buffer:
            while chunk := await file.read(chunk_size):
                file_size += len(chunk)
                SAVE_PROGRESS[id] = ("running", file_size, total_size)

                if file_size > MAX_FILE_SIZE:
                    await buffer.close()
                    file_location.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=400,
                        detail=f"File size exceeds {MAX_FILE_SIZE} bytes limit"
                    )

                await buffer.write(chunk)

        SAVE_PROGRESS[id] = ("completed", file_size, file_size)

        # Start uploader in background
        asyncio.create_task(
            start_file_uploader(file_location, id, path, file.filename, file_size)
        )

        return JSONResponse({"id": id, "status": "ok"})

    except Exception as e:
        logger.error(f"Upload error for {id}: {e}")
        file_location.unlink(missing_ok=True)
        SAVE_PROGRESS[id] = ("failed", file_size, total_size)
        raise


# Keep your other API endpoints (getSaveProgress, getUploadProgress, cancelUpload, etc.) as they are.
# I didn't repeat all of them here to keep it short, but you can copy-paste the rest unchanged.

# Example of one more (add the rest similarly):
@app.post("/api/getSaveProgress")
async def get_save_progress(request: Request):
    data = await request.json()
    if data.get("password") != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    try:
        progress = SAVE_PROGRESS[data["id"]]
        return JSONResponse({"status": "ok", "data": progress})
    except KeyError:
        return JSONResponse({"status": "not found"})


# ... (add all your remaining endpoints: getUploadProgress, cancelUpload, rename, trash, delete, getFileInfoFromUrl, startFileDownloadFromUrl, etc.)
