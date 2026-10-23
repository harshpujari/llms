"""Background extraction: the queue that converts uploaded files to markdown.

Uploads used to extract inside the request, which made a 30-file batch a
22-second HTTP call with no progress and no way to cancel. Now the row lands
immediately with no text and the file is queued here.

One worker, one file at a time, on purpose. Extraction is CPU-bound, and the
whole reason for moving it off the request path was to stop it monopolising the
machine -- running N of them in parallel would recreate the problem it fixed.
Ordering also stays predictable, which makes the UI's polling easy to reason
about.

The queue itself is a WorkQueue: a file that fails for a reason of its own --
a scanned PDF, an unsupported layout -- is marked failed; one that fails for
any other reason is retried until it doesn't. The files table is the record of
what's unextracted, re-read at startup and every sweep.

Each file that extracts is handed on to indexing_worker, which chunks and
summarizes it on its own, slower queue.
"""

# Custom libraries
from logger import configure_logging
from services import extract_service, indexing_worker, storage_service
from services.work_queue import WorkQueue, must

# Database modules
from db_pool import connect
from repository.file_repository import FileRepository

logger = configure_logging(__name__)

# Re-extraction asked for explicitly (/extract?force). Anything else is only
# extracted while it has no text, so a sweep or a repeated request can never
# redo -- and so re-index from scratch -- a file that's already done.
_forced: set[int] = set()


def _extract_one(file_id: int) -> bool:
    """Runs on a worker thread. Opens its own connection: the request-scoped one
    from Depends(get_db) is long gone by the time this runs.

    True when new text was saved, so indexing gets queued.
    """
    db = connect()
    try:
        repo = FileRepository(db)
        row = repo.get_file(file_id)
        if not row:
            _forced.discard(file_id)
            return False  # deleted while it sat in the queue
        if row["text_chars"] is not None and file_id not in _forced:
            return False  # already extracted

        path = storage_service.file_path(row["slug"], row["stored_name"])
        if not path.exists():
            must(repo.update_file_error(file_id, "file missing from storage"), "the extraction error")
            _forced.discard(file_id)
            return False

        try:
            text = extract_service.to_markdown(path)
        except ValueError as exc:
            # The file's own problem: no text layer, an unreadable format.
            logger.info("extraction failed for %s: %s", row["name"], exc)
            must(repo.update_file_error(file_id, str(exc)), "the extraction error")
            _forced.discard(file_id)
            return False

        must(repo.update_file_text(file_id, text), "the extracted text")
        _forced.discard(file_id)
        logger.info("extracted %s (%d chars)", row["name"], len(text))
        return True
    finally:
        db.close()


def _unextracted() -> list[int]:
    """Never attempted -- failures are left for the user to retry."""
    db = connect()
    try:
        return [row["id"] for row in FileRepository(db).get_queued_files()]
    finally:
        db.close()


def _hand_on(file_id: int, extracted: bool) -> None:
    # Runs on the event loop, the only place a queue may be touched.
    if extracted:
        indexing_worker.enqueue(file_id)


queue = WorkQueue("extraction", _extract_one, _unextracted, on_done=_hand_on)


def enqueue(file_id: int, force: bool = False) -> None:
    """Event loop only. force re-extracts a file that already has text -- for
    when the parser itself has changed."""
    if force:
        _forced.add(file_id)
    queue.enqueue(file_id)


def pending() -> int:
    return queue.pending()


def status() -> dict:
    return queue.status()


async def start() -> None:
    await queue.start()


async def stop() -> None:
    await queue.stop()
