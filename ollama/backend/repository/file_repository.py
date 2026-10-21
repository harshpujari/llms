# Custom libraries
from logger import configure_logging

# Database modules
from db_pool import now
from models.file import File

# Default libraries
import sqlite3
from typing import Optional

# Installed libraries
from fastapi import HTTPException

logger = configure_logging(__name__)


def _scope(folder_ids: list[int], file_ids: list[int]) -> tuple[str, list[int]]:
    """SQL matching files in any of folder_ids plus any of file_ids -- the
    chat's attachments -- with its parameters. Empty when both are."""
    clauses, params = [], []
    if folder_ids:
        clauses.append(f"fi.folder_id IN ({', '.join('?' * len(folder_ids))})")
        params += folder_ids
    if file_ids:
        clauses.append(f"fi.id IN ({', '.join('?' * len(file_ids))})")
        params += file_ids
    return " OR ".join(clauses), params


class FileRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get_files_by_folder(self, folder_id: int) -> list[dict]:
        rows = self.db.execute(
            f"""SELECT {File.LIST_COLUMNS}
                  FROM {File.__tablename__}
                 WHERE folder_id = ?
                 ORDER BY name COLLATE NOCASE""",
            (folder_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_file(self, file_id: int) -> Optional[dict]:
        """Joined to folders, because every filesystem path needs the slug."""
        row = self.db.execute(
            """SELECT fi.id, fi.folder_id, fi.name, fi.stored_name, fi.bytes,
                      fi.sha256, fi.mime, fi.created_at, fi.extracted_at,
                      fi.extract_error, fi.indexed_at, fi.chunk_count,
                      fi.summarized_at, fi.embed_model,
                      LENGTH(fi.text_content) AS text_chars,
                      f.slug
                 FROM files fi JOIN folders f ON f.id = fi.folder_id
                WHERE fi.id = ?""",
            (file_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_file_text(self, file_id: int) -> Optional[dict]:
        """The extracted markdown, for inspecting what retrieval will see."""
        row = self.db.execute(
            f"SELECT id, name, text_content FROM {File.__tablename__} WHERE id = ?",
            (file_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_queued_files(self) -> list[dict]:
        """Never attempted -- what the worker picks up, including after a restart.

        Excludes failures on purpose: a scanned PDF would otherwise be retried
        forever on every boot.
        """
        rows = self.db.execute(
            """SELECT id FROM files
                WHERE text_content IS NULL AND extract_error IS NULL
                ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_file_ids(self) -> list[dict]:
        """Every file, for a forced re-extraction after the parser changes."""
        rows = self.db.execute("SELECT id FROM files ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_files_missing_text(self) -> list[dict]:
        """Everything without text, failures included -- the manual retry set."""
        rows = self.db.execute(
            """SELECT id FROM files WHERE text_content IS NULL ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_files_to_index(self) -> list[dict]:
        """Extracted but never chunked: files from before indexing existed, or a
        crash between extraction and chunking. Extracted text always chunks to
        at least one chunk, so chunk_count = 0 can only mean "not yet"."""
        rows = self.db.execute(
            """SELECT id FROM files
                WHERE text_content IS NOT NULL AND chunk_count = 0
                ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_files_missing_summary(self) -> list[dict]:
        """Chunked but no document summary yet -- the indexing worker's resume set.

        Covers chunks still pending as well as a crash after the last chunk
        summary but before the document one: re-chunking clears summarized_at,
        so a file with pending chunks is always in here too.
        """
        rows = self.db.execute(
            """SELECT id FROM files
                WHERE chunk_count > 0 AND summarized_at IS NULL
                ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_indexing_rows(
        self, folder_ids: list[int], file_ids: list[int]
    ) -> list[dict]:
        """Progress through extraction and indexing for files in any of
        folder_ids plus any of file_ids -- every file when both are empty.
        Flags and counts only, never the text itself."""
        clause, params = _scope(folder_ids, file_ids)
        where = f"WHERE {clause}" if clause else ""

        rows = self.db.execute(
            f"""SELECT fi.id, fi.folder_id, fi.name, fi.chunk_count, fi.extract_error,
                       fi.text_content IS NOT NULL  AS extracted,
                       fi.extract_error IS NOT NULL AS failed,
                       fi.summarized_at IS NOT NULL AS summarized,
                       (SELECT COUNT(*) FROM chunks c
                         WHERE c.file_id = fi.id
                           AND c.summarized_at IS NULL
                           AND c.summary_error IS NULL) AS pending
                  FROM files fi
                 {where}
                 ORDER BY fi.id""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def create_file(self, file_data: dict) -> Optional[dict]:
        """The row lands with no text: extraction happens after, on the worker."""
        created = now()
        try:
            cur = self.db.execute(
                f"""INSERT INTO {File.__tablename__}
                    (folder_id, name, stored_name, bytes, sha256, mime, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    file_data["folder_id"],
                    file_data["name"],
                    file_data["stored_name"],
                    file_data["bytes"],
                    file_data["sha256"],
                    file_data.get("mime"),
                    created,
                ),
            )
            self.db.commit()
        except sqlite3.IntegrityError as e:
            self.db.rollback()
            logger.error(f"SQLite IntegrityError: {e}")
            raise HTTPException(
                status_code=409,
                detail="File with the same name already exists in this folder.",
            )
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return None

        return {
            "id": cur.lastrowid,
            "folder_id": file_data["folder_id"],
            "name": file_data["name"],
            "stored_name": file_data["stored_name"],
            "bytes": file_data["bytes"],
            "sha256": file_data["sha256"],
            "mime": file_data.get("mime"),
            "created_at": created,
            "extracted_at": None,
            "extract_error": None,
            "text_chars": None,
            "chunk_count": 0,
            "indexed_at": None,
            "summarized_at": None,
        }

    def update_file_text(self, file_id: int, text: str) -> bool:
        """Success clears any error from an earlier attempt."""
        try:
            self.db.execute(
                f"""UPDATE {File.__tablename__}
                       SET text_content = ?, extracted_at = ?, extract_error = NULL
                     WHERE id = ?""",
                (text, now(), file_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def update_file_error(self, file_id: int, error: str) -> bool:
        """The bytes stay on disk -- the user decides whether to delete or retry."""
        try:
            self.db.execute(
                f"UPDATE {File.__tablename__} SET extract_error = ? WHERE id = ?",
                (error, file_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def update_file_chunked(self, file_id: int, chunk_count: int) -> bool:
        """New chunks make the old document summary stale: it described chunks
        that no longer exist, so it is cleared until the worker rebuilds it.

        indexed_at too: it means chunked *and embedded*, and the old chunks'
        vectors went with them. mark_indexed sets it again once the new chunks
        are embedded.
        """
        try:
            self.db.execute(
                f"""UPDATE {File.__tablename__}
                       SET chunk_count = ?, summary = NULL, summary_model = NULL,
                           summarized_at = NULL, indexed_at = NULL, embed_model = NULL
                     WHERE id = ?""",
                (chunk_count, file_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def update_file_summary(self, file_id: int, summary: str, model: str) -> bool:
        try:
            self.db.execute(
                f"""UPDATE {File.__tablename__}
                       SET summary = ?, summary_model = ?, summarized_at = ?
                     WHERE id = ?""",
                (summary, model, now(), file_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    # --- vectors and search ------------------------------------------------
    # vectors_file_summary is a vec0 table (see models/file.py).

    def get_files_needing_vectors(self, model: str) -> list[dict]:
        """Chunked files with something left to embed: chunk text (not yet
        indexed), chunk summaries, or the document summary -- or vectors from
        an embedding model other than `model`. The indexing worker's backfill
        set, including files from before embeddings existed."""
        rows = self.db.execute(
            """SELECT fi.id FROM files fi
                WHERE fi.chunk_count > 0
                  AND (fi.indexed_at IS NULL
                       OR fi.embed_model IS NOT ?
                       OR EXISTS (SELECT 1 FROM chunks c
                                   WHERE c.file_id = fi.id AND c.summary IS NOT NULL
                                     AND NOT EXISTS (SELECT 1 FROM vectors_chunk_summary v
                                                      WHERE v.chunk_id = c.id))
                       OR (fi.summary IS NOT NULL
                           AND NOT EXISTS (SELECT 1 FROM vectors_file_summary v
                                            WHERE v.file_id = fi.id)))
                ORDER BY fi.id""",
            (model,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_summary_to_embed(self, file_id: int) -> Optional[str]:
        """The document summary, if it has no vector yet."""
        row = self.db.execute(
            """SELECT summary FROM files fi
                WHERE fi.id = ? AND fi.summary IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM vectors_file_summary v WHERE v.file_id = fi.id)""",
            (file_id,),
        ).fetchone()
        return row["summary"] if row else None

    def save_summary_vector(self, file_id: int, vector: bytes) -> bool:
        """Delete-then-insert, as vec0 has no INSERT OR REPLACE; skipped if the
        file was deleted meanwhile, since vec0 has no foreign keys either."""
        try:
            self.db.execute("DELETE FROM vectors_file_summary WHERE file_id = ?", (file_id,))
            if self.db.execute(
                f"SELECT 1 FROM {File.__tablename__} WHERE id = ?", (file_id,)
            ).fetchone():
                self.db.execute(
                    "INSERT INTO vectors_file_summary (file_id, embedding) VALUES (?, ?)",
                    (file_id, vector),
                )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def mark_indexed(self, file_id: int, model: str) -> bool:
        """Every chunk's text is embedded, by `model`: searchable from now."""
        try:
            self.db.execute(
                f"UPDATE {File.__tablename__} SET indexed_at = ?, embed_model = ? WHERE id = ?",
                (now(), model, file_id),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def clear_vectors(self, file_id: int) -> bool:
        """Drops every vector of a file -- its chunks' and its summary's -- and
        un-indexes it, so it is embedded again from scratch. For a change of
        embedding model; the text, chunks and summaries all stay."""
        try:
            for table in ("vectors_chunk_text", "vectors_chunk_summary"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id = ?)",
                    (file_id,),
                )
            self.db.execute("DELETE FROM vectors_file_summary WHERE file_id = ?", (file_id,))
            self.db.execute(
                f"UPDATE {File.__tablename__} SET indexed_at = NULL, embed_model = NULL WHERE id = ?",
                (file_id,),
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return True

    def search_summaries(self, vector: bytes, file_ids: list[int], k: int) -> list[dict]:
        """Documents nearest in meaning by their summary, among file_ids."""
        if not file_ids:
            return []
        marks = ", ".join("?" * len(file_ids))
        rows = self.db.execute(
            f"""SELECT file_id, distance
                  FROM vectors_file_summary
                 WHERE embedding MATCH ? AND k = ? AND file_id IN ({marks})
                 ORDER BY distance""",
            (vector, k, *file_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_searchable_files(self, folder_ids: list[int], file_ids: list[int]) -> list[dict]:
        """The attached files, each saying whether it can be searched yet
        (indexed_at set) and, if not, why -- for the "answered from 6 of 8"
        note. Nothing attached means nothing here, unlike get_indexing_rows."""
        clause, params = _scope(folder_ids, file_ids)
        if not clause:
            return []
        rows = self.db.execute(
            f"""SELECT fi.id, fi.folder_id, fi.name, fi.summary,
                       fi.indexed_at IS NOT NULL    AS searchable,
                       fi.text_content IS NOT NULL  AS extracted,
                       fi.extract_error IS NOT NULL AS failed
                  FROM files fi
                 WHERE {clause}
                 ORDER BY fi.id""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_file(self, file_id: int) -> bool:
        try:
            cur = self.db.execute(
                f"DELETE FROM {File.__tablename__} WHERE id = ?", (file_id,)
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return cur.rowcount > 0
