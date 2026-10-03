# Custom libraries
from logger import configure_logging

# Database modules
from db_pool import now
from models.chunk import Chunk

# Default libraries
import sqlite3

logger = configure_logging(__name__)


class ChunkRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get_pending_chunks(self, file_id: int) -> list[dict]:
        """Still to summarize, in document order -- the worker's resume point.

        Excludes failures on purpose, as get_queued_files does for extraction.
        """
        rows = self.db.execute(
            f"""SELECT id, ord, heading, text
                  FROM {Chunk.__tablename__}
                 WHERE file_id = ?
                   AND summarized_at IS NULL AND summary_error IS NULL
                 ORDER BY ord""",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_chunk_summaries(self, file_id: int) -> list[dict]:
        """What the document summary is built from: summaries, never raw text."""
        rows = self.db.execute(
            f"""SELECT ord, heading, summary
                  FROM {Chunk.__tablename__}
                 WHERE file_id = ? AND summary IS NOT NULL
                 ORDER BY ord""",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_files_with_pending_chunks(self) -> list[dict]:
        """Files a previous run left half-summarized, for the worker to requeue."""
        rows = self.db.execute(
            f"""SELECT DISTINCT file_id AS id
                  FROM {Chunk.__tablename__}
                 WHERE summarized_at IS NULL AND summary_error IS NULL
                 ORDER BY file_id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def replace_chunks(self, file_id: int, chunks: list[dict]) -> bool:
        """A file's old chunks out and new ones in, as one transaction.

        Re-indexing after a chunker change must never leave a mix of both:
        ord would collide, and the summaries would describe text that is gone.
        """
        try:
            self.db.execute(
                f"DELETE FROM {Chunk.__tablename__} WHERE file_id = ?", (file_id,)
            )
            self.db.executemany(
                f"""INSERT INTO {Chunk.__tablename__}
                    (file_id, ord, heading, text, tokens)
                    VALUES (?, ?, ?, ?, ?)""",
                [
                    (file_id, c["ord"], c["heading"], c["text"], c["tokens"])
                    for c in chunks
                ],
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def update_chunk_summary(self, chunk_id: int, summary: str, model: str) -> bool:
        """Saved per chunk, so a restart resumes rather than starts over.
        Success clears any error from an earlier attempt."""
        try:
            self.db.execute(
                f"""UPDATE {Chunk.__tablename__}
                       SET summary = ?, summary_model = ?, summarized_at = ?,
                           summary_error = NULL
                     WHERE id = ?""",
                (summary, model, now(), chunk_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def update_chunk_error(self, chunk_id: int, error: str) -> bool:
        """Takes the chunk out of the pending set, so it isn't retried every boot."""
        try:
            self.db.execute(
                f"UPDATE {Chunk.__tablename__} SET summary_error = ? WHERE id = ?",
                (error, chunk_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True
