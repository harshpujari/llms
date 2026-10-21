"""Background indexing: chunk each extracted file, embed every chunk's text,
summarize every chunk, then summarize the document from those -- embedding
each summary too.

Embedding the text comes first, because it's what makes a file searchable:
seconds after upload, against minutes for its summaries. Those only add a
second way to find a chunk once they arrive.

Its own queue rather than a step on the extraction one: extraction takes
seconds, summarizing takes minutes per document on CPU, and an upload shouldn't
wait behind someone's 300-page PDF to get its text.

One worker, one file at a time, for the same reasons as extraction -- the model
can only usefully do one thing at once on CPU, and ordering stays predictable.

State lives in the tables, not in this queue. Each chunk summary is saved as it
is written, and vectors a batch at a time, so a restart (including every
uvicorn --reload) loses at most the work in flight: start() requeues whatever
is unfinished -- or missing vectors -- and it resumes there.

When Ollama can't be reached, the file goes back on the queue and the worker
waits, backing off. Nothing is marked failed -- the text is fine, the model is
just not there yet.
"""

# Custom libraries
from logger import configure_logging
from services import chunk_service, embed_service, summary_service
from services.embed_service import EmbedUnavailable
from services.summary_service import SummaryUnavailable

# Database modules
from db_pool import connect
from repository.chunk_repository import ChunkRepository
from repository.file_repository import FileRepository

# Default libraries
import asyncio
import time
from typing import Optional

# Installed libraries
from starlette.concurrency import run_in_threadpool

logger = configure_logging(__name__)

# Waits after Ollama is unreachable: 30s, doubling, up to 5 minutes.
BACKOFF_START = 30
BACKOFF_MAX = 300

_queue: Optional[asyncio.Queue] = None
_task: Optional[asyncio.Task] = None
# file_id -> rechunk, for everything waiting. Keeps a file from being queued
# twice, and lets a later "rechunk" request upgrade one already waiting.
_queued: dict[int, bool] = {}

# What status() reports. Written by the worker thread, read by requests --
# plain assignments of small values, and only ever advisory.
_current: Optional[dict] = None
_paused: Optional[dict] = None


def enqueue(file_id: int, rechunk: bool = False) -> None:
    """Called from the event loop only -- asyncio.Queue isn't thread-safe.

    rechunk=True when the text changed (a fresh extraction): the old chunks,
    and every summary of them, describe text that is gone.
    """
    if _queue is None:
        logger.warning("indexing worker not running, file %s not queued", file_id)
        return
    if file_id in _queued:
        _queued[file_id] = _queued[file_id] or rechunk
        return
    _queued[file_id] = rechunk
    _queue.put_nowait(file_id)


def pending() -> int:
    return _queue.qsize() if _queue else 0


def status() -> dict:
    """Live progress, for telling the user what isn't ready yet.

    Which files are unfinished is better read from the files table; this adds
    what only the worker knows -- how far through the current file it is, and
    whether it is waiting on Ollama.
    """
    paused = None
    if _paused:
        paused = {
            "reason": _paused["reason"],
            "retry_in": max(0, round(_paused["retry_at"] - time.time())),
        }
    return {"queued": pending(), "current": _current, "paused": paused}


def _embed_text(file_id: int, files: FileRepository, chunks: ChunkRepository) -> bool:
    """Every chunk's text, a batch at a time so a restart keeps what's done;
    then the file is marked indexed -- searchable. False if it stopped short."""
    while todo := chunks.get_chunks_missing_text_vectors(file_id)[:embed_service.BATCH]:
        if not files.get_file(file_id):
            return False  # deleted mid-way: its chunks went with it
        vectors = embed_service.embed_documents([c["text"] for c in todo])
        if not chunks.save_text_vectors(file_id, [(c["id"], v) for c, v in zip(todo, vectors)]):
            return False  # logged by the repository; the next boot retries
    return files.mark_indexed(file_id, embed_service.EMBED_MODEL)


def _embed_summaries(file_id: int, files: FileRepository, chunks: ChunkRepository) -> None:
    """Chunk summaries not embedded yet, and the document summary if it isn't."""
    while todo := chunks.get_chunks_missing_summary_vectors(file_id)[:embed_service.BATCH]:
        vectors = embed_service.embed_documents([c["summary"] for c in todo])
        if not chunks.save_summary_vectors(file_id, [(c["id"], v) for c, v in zip(todo, vectors)]):
            return
    summary = files.get_summary_to_embed(file_id)
    if summary:
        files.save_summary_vector(file_id, embed_service.embed_documents([summary])[0])


def _index_one(file_id: int, rechunk: bool) -> None:
    """Runs on a worker thread, with its own connection, as extraction does.

    Raises SummaryUnavailable or EmbedUnavailable when Ollama can't be used;
    everything already saved stays saved, and the next attempt picks up there.
    """
    global _current
    db = connect()
    try:
        files, chunks = FileRepository(db), ChunkRepository(db)
        row = files.get_file(file_id)
        if not row:
            return  # deleted while it sat in the queue

        if rechunk or row["chunk_count"] == 0:
            text = (files.get_file_text(file_id) or {}).get("text_content")
            if not text:
                return  # not extracted yet; extraction queues it when it is
            pieces = chunk_service.to_chunks(text)
            if not chunks.replace_chunks(file_id, pieces):
                return  # logged by the repository; the next boot retries
            files.update_file_chunked(file_id, len(pieces))
            logger.info("chunked %s into %d chunks", row["name"], len(pieces))
            row = files.get_file(file_id)
        elif row["embed_model"] and row["embed_model"] != embed_service.EMBED_MODEL:
            # Vectors from two models can't be compared: start this file's over
            # rather than mix them. The chunks and summaries all stay.
            files.clear_vectors(file_id)
            logger.info("re-embedding %s: made by %s, now using %s",
                        row["name"], row["embed_model"], embed_service.EMBED_MODEL)

        if not row["indexed_at"] or row["embed_model"] != embed_service.EMBED_MODEL:
            if not _embed_text(file_id, files, chunks):
                return
            logger.info("indexed %s: searchable", row["name"])

        todo = chunks.get_pending_chunks(file_id)
        total = row["chunk_count"]
        _current = {
            "file_id": file_id,
            "name": row["name"],
            "done": total - len(todo),
            "total": total,
        }

        for chunk in todo:
            if not files.get_file(file_id):
                return  # deleted mid-way: its chunks went with it

            try:
                summary = summary_service.summarize_chunk(
                    chunk["text"], chunk["heading"], row["name"]
                )
            except ValueError as exc:
                # The model answered with nothing usable. Record it and move
                # on: the document summary is built from the chunks that did.
                logger.info("chunk %s of %s not summarized: %s", chunk["ord"], row["name"], exc)
                chunks.update_chunk_error(chunk["id"], str(exc))
            else:
                chunks.update_chunk_summary(chunk["id"], summary, summary_service.SUMMARY_MODEL)
            _current = {**_current, "done": _current["done"] + 1}

        # Already summarized -- a file requeued only for its vectors, say. A
        # rewrite would be a minute of model time to replace a summary that's
        # fine, and would throw away its vector with it.
        latest = files.get_file(file_id)
        if not latest:
            return  # deleted mid-way
        if not latest["summarized_at"]:
            summaries = chunks.get_chunk_summaries(file_id)
            if not summaries:
                # Every chunk failed. Nothing to summarize from; startup will
                # look at it again, cheaply, since there is no model call to make.
                logger.warning("no chunk summaries for %s, skipping document summary", row["name"])
                return

            try:
                document = summary_service.summarize_document(row["name"], summaries)
            except ValueError as exc:
                logger.warning("document summary failed for %s: %s", row["name"], exc)
                return

            files.update_file_summary(file_id, document, summary_service.SUMMARY_MODEL)
            logger.info("summarized %s (%d chunks)", row["name"], len(summaries))

        _embed_summaries(file_id, files, chunks)
    finally:
        _current = None
        db.close()


async def _run() -> None:
    global _paused
    backoff = BACKOFF_START
    while True:
        file_id = await _queue.get()
        rechunk = _queued.pop(file_id, False)
        try:
            await run_in_threadpool(_index_one, file_id, rechunk)
            backoff = BACKOFF_START
        except (SummaryUnavailable, EmbedUnavailable) as exc:
            # Back on the queue without rechunk: chunking happens before the
            # first model call, so whatever this attempt chunked is kept.
            enqueue(file_id)
            logger.warning("ollama unavailable, retrying in %ds: %s", backoff, exc)
            _paused = {"reason": str(exc), "retry_at": time.time() + backoff}
            await asyncio.sleep(backoff)
            _paused = None
            backoff = min(backoff * 2, BACKOFF_MAX)
        except Exception:
            # A crash here must not kill the worker, or every later file
            # silently stays unindexed forever.
            logger.exception("indexing worker failed on file %s", file_id)
        finally:
            _queue.task_done()


async def start() -> None:
    global _queue, _task, _queued
    _queue = asyncio.Queue()
    _queued = {}
    _task = asyncio.create_task(_run())

    # Extracted but never chunked, chunked but never finished, and finished
    # but missing vectors (from before embeddings, or another model). All
    # resume from what the tables already hold; enqueue drops duplicates.
    db = connect()
    try:
        files = FileRepository(db)
        unfinished = (
            files.get_files_to_index()
            + files.get_files_missing_summary()
            + files.get_files_needing_vectors(embed_service.EMBED_MODEL)
        )
    finally:
        db.close()

    for row in unfinished:
        enqueue(row["id"])
    if unfinished:
        logger.info("requeued %d file(s) for indexing from a previous run", pending())


async def stop() -> None:
    global _queue, _task, _paused
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    _queue = None
    _paused = None
