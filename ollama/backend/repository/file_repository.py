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
