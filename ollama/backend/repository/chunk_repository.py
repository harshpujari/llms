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

    # --- vectors and search ------------------------------------------------
    # vectors_chunk_text and vectors_chunk_summary are vec0 tables, chunks_fts
    # an FTS5 one (see models/chunk.py). Whether a chunk is embedded is read
    # from the vector tables themselves -- no flag on the row to drift.

    def get_chunks_missing_text_vectors(self, file_id: int) -> list[dict]:
        """Chunks whose text isn't embedded yet, in document order."""
        rows = self.db.execute(
            f"""SELECT c.id, c.text
                  FROM {Chunk.__tablename__} c
                 WHERE c.file_id = ?
                   AND NOT EXISTS (SELECT 1 FROM vectors_chunk_text v WHERE v.chunk_id = c.id)
                 ORDER BY c.ord""",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_chunks_missing_summary_vectors(self, file_id: int) -> list[dict]:
        """Summarized chunks whose summary isn't embedded yet."""
        rows = self.db.execute(
            f"""SELECT c.id, c.summary
                  FROM {Chunk.__tablename__} c
                 WHERE c.file_id = ? AND c.summary IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM vectors_chunk_summary v WHERE v.chunk_id = c.id)
                 ORDER BY c.ord""",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _save_vectors(self, table: str, file_id: int, vectors: list[tuple[int, bytes]]) -> bool:
        """vec0 has no INSERT OR REPLACE, so an existing vector is deleted
        first, in the same transaction. A chunk that's gone by now -- the file
        re-chunked while this was embedding -- is skipped: vec0 has no foreign
        keys, so nothing else would stop its vector being orphaned. The first
        DELETE takes the write lock, so the chunk can't vanish in between."""
        try:
            for chunk_id, vector in vectors:
                self.db.execute(f"DELETE FROM {table} WHERE chunk_id = ?", (chunk_id,))
                if self.db.execute(
                    f"SELECT 1 FROM {Chunk.__tablename__} WHERE id = ?", (chunk_id,)
                ).fetchone():
                    self.db.execute(
                        f"INSERT INTO {table} (chunk_id, embedding, file_id) VALUES (?, ?, ?)",
                        (chunk_id, vector, file_id),
                    )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def save_text_vectors(self, file_id: int, vectors: list[tuple[int, bytes]]) -> bool:
        return self._save_vectors("vectors_chunk_text", file_id, vectors)

    def save_summary_vectors(self, file_id: int, vectors: list[tuple[int, bytes]]) -> bool:
        return self._save_vectors("vectors_chunk_summary", file_id, vectors)

    def _knn(self, table: str, vector: bytes, file_ids: list[int], k: int) -> list[dict]:
        if not file_ids:
            return []
        marks = ", ".join("?" * len(file_ids))
        rows = self.db.execute(
            f"""SELECT chunk_id, distance
                  FROM {table}
                 WHERE embedding MATCH ? AND k = ? AND file_id IN ({marks})
                 ORDER BY distance""",
            (vector, k, *file_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_text_vectors(self, vector: bytes, file_ids: list[int], k: int) -> list[dict]:
        """Chunks nearest in meaning by their text, among file_ids, closest first."""
        return self._knn("vectors_chunk_text", vector, file_ids, k)

    def search_summary_vectors(self, vector: bytes, file_ids: list[int], k: int) -> list[dict]:
        """Chunks nearest in meaning by their summary, among file_ids."""
        return self._knn("vectors_chunk_summary", vector, file_ids, k)

    def text_distances(self, vector: bytes, chunk_ids: list[int]) -> dict[int, float]:
        """Distance in meaning from `vector` to these chunks' text, looked up
        directly -- for keyword hits the nearest-neighbour search didn't reach."""
        if not chunk_ids:
            return {}
        marks = ", ".join("?" * len(chunk_ids))
        rows = self.db.execute(
            f"""SELECT chunk_id, vec_distance_cosine(embedding, ?) AS distance
                  FROM vectors_chunk_text
                 WHERE chunk_id IN ({marks})""",
            (vector, *chunk_ids),
        ).fetchall()
        return {r["chunk_id"]: r["distance"] for r in rows}

    def search_keywords(self, match: str, file_ids: list[int], k: int) -> list[dict]:
        """Best BM25 matches among file_ids, best first (bm25 is lower-is-better).

        `match` is an FTS5 expression the caller has already built and made
        safe -- turning a question into one is search logic, not SQL.
        """
        if not file_ids or not match:
            return []
        marks = ", ".join("?" * len(file_ids))
        rows = self.db.execute(
            f"""SELECT c.id AS chunk_id, bm25(chunks_fts) AS score
                  FROM chunks_fts JOIN {Chunk.__tablename__} c ON c.id = chunks_fts.rowid
                 WHERE chunks_fts MATCH ? AND c.file_id IN ({marks})
                 ORDER BY score
                 LIMIT ?""",
            (match, *file_ids, k),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_chunks(self, ids: list[int]) -> list[dict]:
        """The chunks a search picked, with their file's name for citing them.
        Ids that no longer exist are simply absent."""
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        rows = self.db.execute(
            f"""SELECT c.id, c.file_id, c.ord, c.heading, c.text, c.tokens,
                       f.name AS file_name
                  FROM {Chunk.__tablename__} c JOIN files f ON f.id = c.file_id
                 WHERE c.id IN ({marks})""",
            ids,
        ).fetchall()
        return [dict(r) for r in rows]
