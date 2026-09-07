# Custom libraries
from logger import configure_logging

# Database modules
from db_pool import now
from models.folder import Folder

# Default libraries
import sqlite3
from typing import Optional

# Installed libraries
from fastapi import HTTPException

logger = configure_logging(__name__)


class FolderRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get_all_folders(self) -> list[dict]:
        rows = self.db.execute(
            f"""
            SELECT f.*,
                   COUNT(fi.id)               AS file_count,
                   COALESCE(SUM(fi.bytes), 0) AS total_bytes
              FROM {Folder.__tablename__} f
              LEFT JOIN files fi ON fi.folder_id = f.id
             GROUP BY f.id
             ORDER BY f.name COLLATE NOCASE
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_folder(self, folder_id: int) -> Optional[dict]:
        row = self.db.execute(
            f"SELECT * FROM {Folder.__tablename__} WHERE id = ?", (folder_id,)
        ).fetchone()
        return dict(row) if row else None

    def slug_exists(self, slug: str) -> bool:
        return (
            self.db.execute(
                f"SELECT 1 FROM {Folder.__tablename__} WHERE slug = ?", (slug,)
            ).fetchone()
            is not None
        )

    def create_folder(self, folder_data: dict) -> Optional[dict]:
        try:
            cur = self.db.execute(
                f"INSERT INTO {Folder.__tablename__} (name, slug, created_at) VALUES (?, ?, ?)",
                (folder_data["name"], folder_data["slug"], now()),
            )
            self.db.commit()
        except sqlite3.IntegrityError as e:
            self.db.rollback()
            logger.error(f"SQLite IntegrityError: {e}")
            raise HTTPException(
                status_code=409,
                detail="Folder with the same name already exists. Please check and retry.",
            )
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return None

        return {
            "id": cur.lastrowid,
            "name": folder_data["name"],
            "slug": folder_data["slug"],
            "file_count": 0,
            "total_bytes": 0,
        }

    def delete_folder(self, folder_id: int) -> bool:
        """File rows go with it -- ON DELETE CASCADE, enabled per connection."""
        try:
            cur = self.db.execute(
                f"DELETE FROM {Folder.__tablename__} WHERE id = ?", (folder_id,)
            )
            self.db.commit()
        except sqlite3.Error as e:
            self.db.rollback()
            logger.error(f"SQLite Error: {e}")
            return False
        return cur.rowcount > 0
