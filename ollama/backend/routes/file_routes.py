"""File endpoints: upload, inspect, delete, and the format allowlist."""

# Custom libraries
from db_pool import get_db
from logger import configure_logging
from services import extract_service, library_service, storage_service

# Default libraries
import sqlite3

# Installed libraries
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

logger = configure_logging(__name__)

file_router = APIRouter(tags=["Files"])


@file_router.get("/formats")
async def get_formats():
    """One source of truth for the upload allowlist -- the UI renders this."""
    return {
        "groups": extract_service.FORMAT_GROUPS,
        "max_mb": storage_service.MAX_UPLOAD_BYTES // (1024 * 1024),
    }


@file_router.get("/folders/{folder_id}/files")
async def get_files(folder_id: int, db: sqlite3.Connection = Depends(get_db)):
    if not library_service.get_folder(db, folder_id):
        raise HTTPException(404, "no such folder")
    return library_service.list_files(db, folder_id)


@file_router.post("/folders/{folder_id}/files", status_code=201)
async def post_files(
    folder_id: int,
    files: list[UploadFile],
    db: sqlite3.Connection = Depends(get_db),
):
    """Multi-upload. A rejected file doesn't abort the ones beside it."""
    saved, failed = [], []
    for upload in files:
        try:
            # Extraction is synchronous and CPU-bound -- a big PDF would block
            # the event loop, and with it every in-flight chat stream.
            # UploadFile.file is a sync SpooledTemporaryFile, so the read side
            # belongs on the same thread.
            saved.append(
                await run_in_threadpool(
                    library_service.save_file,
                    db,
                    folder_id,
                    upload.filename,
                    upload.file,
                    upload.content_type,
                )
            )
        except LookupError:
            raise HTTPException(404, "no such folder")
        except ValueError as exc:
            logger.info("rejected %s: %s", upload.filename, exc)
            failed.append({"name": upload.filename, "error": str(exc)})
    return {"saved": saved, "failed": failed}


@file_router.post("/extract")
async def post_extract(db: sqlite3.Connection = Depends(get_db)):
    """Backfill: convert any file that has no extracted text yet."""
    return await run_in_threadpool(library_service.backfill, db)


@file_router.get("/files/{file_id}/text")
async def get_file_text(file_id: int, db: sqlite3.Connection = Depends(get_db)):
    """The extracted markdown -- what retrieval will chunk, not the original."""
    row = library_service.file_text(db, file_id)
    if not row:
        raise HTTPException(404, "no such file")
    return row


@file_router.delete("/files/{file_id}", status_code=204)
async def remove_file(file_id: int, db: sqlite3.Connection = Depends(get_db)):
    if not library_service.delete_file(db, file_id):
        raise HTTPException(404, "no such file")
