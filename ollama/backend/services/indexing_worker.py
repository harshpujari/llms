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
uvicorn --reload) loses at most the work in flight: the scan at startup -- and
every sweep after -- finds whatever is unfinished, and it resumes there.

The queue is a WorkQueue. When Ollama can't be reached it pauses, backing off,
and nothing is marked failed -- the text is fine, the model just isn't there
yet. Any other failure -- a write the database refused, a bug -- retries that
file on its own clock while the others carry on.
"""

# Custom libraries
from logger import configure_logging
from services import chunk_service, embed_service, summary_service
from services.embed_service import EmbedUnavailable
from services.summary_service import SummaryUnavailable
from services.work_queue import WorkQueue, must

# Database modules
from db_pool import connect
from repository.chunk_repository import ChunkRepository
from repository.file_repository import FileRepository

# Default libraries
from typing import Optional

logger = configure_logging(__name__)

# How far through the current file the worker is. Written by the worker
# thread, read by requests -- plain assignments of small values, only advisory.
_current: Optional[dict] = None


def _embed_text(file_id: int, files: FileRepository, chunks: ChunkRepository) -> bool:
    """Every chunk's text, a batch at a time so a restart keeps what's done;
    then the file is marked indexed -- searchable. False if it stopped short."""
    while todo := chunks.get_chunks_missing_text_vectors(file_id)[:embed_service.BATCH]:
        if not files.get_file(file_id):
            return False  # deleted mid-way: its chunks went with it
        vectors = embed_service.embed_documents([c["text"] for c in todo])
        must(chunks.save_text_vectors(file_id, [(c["id"], v) for c, v in zip(todo, vectors)]), "chunk vectors")
    must(files.mark_indexed(file_id, embed_service.EMBED_MODEL), "the file as indexed")
    return True


def _embed_summaries(file_id: int, files: FileRepository, chunks: ChunkRepository) -> None:
    """Chunk summaries not embedded yet, and the document summary if it isn't."""
    while todo := chunks.get_chunks_missing_summary_vectors(file_id)[:embed_service.BATCH]:
        vectors = embed_service.embed_documents([c["summary"] for c in todo])
        must(chunks.save_summary_vectors(file_id, [(c["id"], v) for c, v in zip(todo, vectors)]), "summary vectors")
    summary = files.get_summary_to_embed(file_id)
    if summary:
        must(files.save_summary_vector(file_id, embed_service.embed_documents([summary])[0]), "the summary vector")


def _index_one(file_id: int) -> None:
    """Runs on a worker thread, with its own connection, as extraction does.
    Safe to run on any file at any time: each step looks at what's already in
    the tables and does only what's missing.

    Raises SummaryUnavailable or EmbedUnavailable when Ollama can't be used,
    RetryLater when a write didn't happen; everything already saved stays
    saved, and the next attempt picks up there.
    """
    global _current
    db = connect()
    try:
        files, chunks = FileRepository(db), ChunkRepository(db)
        row = files.get_file(file_id)
        if not row:
            return  # deleted while it sat in the queue

        # chunk_count 0: never chunked, or new text since -- saving new text
        # resets it, so this holds across a restart too.
        if row["chunk_count"] == 0:
            text = (files.get_file_text(file_id) or {}).get("text_content")
            if not text:
                return  # not extracted yet; extraction queues it when it is
            pieces = chunk_service.to_chunks(text)
            must(chunks.replace_chunks(file_id, pieces), "the chunks")
            must(files.update_file_chunked(file_id, len(pieces)), "the chunk count")
            logger.info("chunked %s into %d chunks", row["name"], len(pieces))
            row = files.get_file(file_id)
        elif row["embed_model"] and row["embed_model"] != embed_service.EMBED_MODEL:
            # Vectors from two models can't be compared: start this file's over
            # rather than mix them. The chunks and summaries all stay.
            must(files.clear_vectors(file_id), "the cleared vectors")
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
                must(chunks.update_chunk_error(chunk["id"], str(exc)), "a chunk's summary error")
            else:
                must(chunks.update_chunk_summary(chunk["id"], summary, summary_service.SUMMARY_MODEL), "a chunk summary")
            _current = {**_current, "done": _current["done"] + 1}

        # Already summarized -- a file requeued only for its vectors, say. A
        # rewrite would be a minute of model time to replace a summary that's
        # fine, and would throw away its vector with it.
        latest = files.get_file(file_id)
        if not latest:
            return  # deleted mid-way
        if not latest["summarized_at"]:
            summaries = chunks.get_chunk_summaries(file_id)
            document = None
            if not summaries:
                # Every chunk failed: nothing to summarize from.
                logger.warning("no chunk summaries for %s, so no document summary", row["name"])
            else:
                try:
                    document = summary_service.summarize_document(row["name"], summaries)
                except ValueError as exc:
                    logger.warning("document summary failed for %s: %s", row["name"], exc)
            # Done either way -- with no summary, the step is still finished.
            # Left open, every sweep would come back and try again.
            must(files.update_file_summary(file_id, document, summary_service.SUMMARY_MODEL),
                 "the document summary")
            if document:
                logger.info("summarized %s (%d chunks)", row["name"], len(summaries))

        _embed_summaries(file_id, files, chunks)
    finally:
        _current = None
        db.close()


def _unfinished() -> list[int]:
    """Extracted but never chunked (or re-extracted), chunked but not finished,
    finished but missing vectors (from before embeddings, or another model),
    and any with chunks still pending a summary."""
    db = connect()
    try:
        files, chunks = FileRepository(db), ChunkRepository(db)
        rows = (
            files.get_files_to_index()
            + files.get_files_missing_summary()
            + files.get_files_needing_vectors(embed_service.EMBED_MODEL)
            + chunks.get_files_with_pending_chunks()
        )
    finally:
        db.close()
    return sorted({row["id"] for row in rows})


queue = WorkQueue(
    "indexing", _index_one, _unfinished,
    pause_on=(SummaryUnavailable, EmbedUnavailable),
)


def enqueue(file_id: int) -> None:
    """Event loop only."""
    queue.enqueue(file_id)


def pending() -> int:
    return queue.pending()


def status() -> dict:
    """Live progress, for telling the user what isn't ready yet.

    Which files are unfinished is better read from the files table; this adds
    what only the worker knows -- how far through the current file it is,
    whether it's waiting on Ollama, and which files are waiting to retry.
    """
    s = queue.status()
    return {"queued": s["queued"], "current": _current, "paused": s["paused"], "retrying": s["retrying"]}


async def start() -> None:
    await queue.start()


async def stop() -> None:
    await queue.stop()
