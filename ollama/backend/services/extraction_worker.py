"""Background extraction: the queue that converts uploaded files to markdown.

Uploads used to extract inside the request, which made a 30-file batch a
22-second HTTP call with no progress and no way to cancel. Now the row lands
immediately with no text and the file is queued here.

One worker, one file at a time, on purpose. Extraction is CPU-bound, and the
whole reason for moving it off the request path was to stop it monopolising the
machine -- running N of them in parallel would recreate the problem it fixed.
Ordering also stays predictable, which makes the UI's polling easy to reason
about.

State lives in the files table, not in this queue, so a restart loses nothing:
start() re-reads whatever is still unextracted and queues it again.
"""

# Custom libraries
from logger import configure_logging
from services import extract_service, storage_service

# Database modules
from db_pool import connect
from repository.file_repository import FileRepository

# Default libraries
import asyncio
from typing import Optional

# Installed libraries
from starlette.concurrency import run_in_threadpool

logger = configure_logging(__name__)

_queue: Optional[asyncio.Queue] = None
_task: Optional[asyncio.Task] = None


def enqueue(file_id: int) -> None:
    """Called from the event loop only -- asyncio.Queue isn't thread-safe."""
    if _queue is None:
        logger.warning("extraction worker not running, file %s not queued", file_id)
        return
    _queue.put_nowait(file_id)


def pending() -> int:
    return _queue.qsize() if _queue else 0


def _extract_one(file_id: int) -> None:
    """Runs on a worker thread. Opens its own connection: the request-scoped one
    from Depends(get_db) is long gone by the time this runs."""
    db = connect()
    try:
        repo = FileRepository(db)
        row = repo.get_file(file_id)
        if not row:
            return  # deleted while it sat in the queue

        path = storage_service.file_path(row["slug"], row["stored_name"])
        if not path.exists():
            repo.update_file_error(file_id, "file missing from storage")
            return

        try:
            text = extract_service.to_markdown(path)
        except ValueError as exc:
            logger.info("extraction failed for %s: %s", row["name"], exc)
            repo.update_file_error(file_id, str(exc))
            return

        repo.update_file_text(file_id, text)
        logger.info("extracted %s (%d chars)", row["name"], len(text))
    finally:
        db.close()


async def _run() -> None:
    while True:
        file_id = await _queue.get()
        try:
            await run_in_threadpool(_extract_one, file_id)
        except Exception:
            # A crash here must not kill the worker, or every later upload
            # silently stays queued forever.
            logger.exception("extraction worker failed on file %s", file_id)
        finally:
            _queue.task_done()


async def start() -> None:
    global _queue, _task
    _queue = asyncio.Queue()
    _task = asyncio.create_task(_run())

    # Anything left unextracted by a previous run gets picked up again.
    db = connect()
    try:
        queued = FileRepository(db).get_queued_files()
    finally:
        db.close()

    for row in queued:
        _queue.put_nowait(row["id"])
    if queued:
        logger.info("requeued %d file(s) from a previous run", len(queued))


async def stop() -> None:
    global _queue, _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    _queue = None
