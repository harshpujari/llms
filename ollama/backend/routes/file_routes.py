"""File endpoints: upload, inspect, delete, and the format allowlist."""

# Custom libraries
from db_pool import get_db
from logger import configure_logging
from services import (
    extract_service,
    extraction_worker,
    library_service,
    storage_service,
)

# Default libraries
import sqlite3

# Installed libraries
from fastapi import APIRouter, Depends, HTTPException, UploadFile
# Aliased: schemas.file_schema.FileResponse is a different thing entirely.
from fastapi.responses import FileResponse as RawFile
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
    """Multi-upload. Returns as soon as the bytes are stored -- text extraction
    is queued and reported through each file's status. A rejected file doesn't
    abort the ones beside it."""
    saved, failed = [], []
    for upload in files:
        try:
            # Writing the bytes is blocking I/O, so it goes to a worker thread.
            # UploadFile.file is a sync SpooledTemporaryFile, which belongs on
            # the same thread as the read loop that drains it.
            row = await run_in_threadpool(
                library_service.save_file,
                db,
                folder_id,
                upload.filename,
                upload.file,
                upload.content_type,
            )
        except LookupError:
            raise HTTPException(404, "no such folder")
        except ValueError as exc:
            logger.info("rejected %s: %s", upload.filename, exc)
            failed.append({"name": upload.filename, "error": str(exc)})
            continue

        # Back on the event loop after the await, which is the only place the
        # asyncio queue may be touched.
        extraction_worker.enqueue(row["id"])
        saved.append(row)

    return {"saved": saved, "failed": failed, "queued": extraction_worker.pending()}


@file_router.post("/extract")
async def post_extract(db: sqlite3.Connection = Depends(get_db)):
    """Retry: re-queue every file without text, previous failures included."""
    ids = library_service.files_missing_text(db)
    for file_id in ids:
        extraction_worker.enqueue(file_id)
    return {"requeued": len(ids), "queued": extraction_worker.pending()}


@file_router.get("/files/{file_id}/raw")
async def get_file_raw(
    file_id: int,
    download: bool = False,
    db: sqlite3.Connection = Depends(get_db),
):
    """The original bytes.

    Two callers, two dispositions: the viewer's iframe wants `inline` so the
    browser renders it in place, the Download button wants `attachment` so it
    saves regardless of type. It has to be decided here -- an <a download> is
    ignored cross-origin, and the API is on a different port to the page.
    """
    found = library_service.file_on_disk(db, file_id)
    if not found:
        raise HTTPException(404, "no such file")
    path, name, media_type = found

    return RawFile(
        path,
        media_type=media_type,
        filename=name,
        content_disposition_type="attachment" if download else "inline",
        # nosniff: the browser must not second-guess the type we just decided on.
        headers={"X-Content-Type-Options": "nosniff"},
    )


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
