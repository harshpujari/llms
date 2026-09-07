"""The folders table: the top level of the Library, one directory each."""

# Custom libraries
from db_pool import Model, connect, now


class Folder(Model):
    __tablename__ = "folders"
    __schema__ = """
    CREATE TABLE IF NOT EXISTS folders (
      id         INTEGER PRIMARY KEY,
      name       TEXT NOT NULL,          -- as typed, shown in the UI
      slug       TEXT NOT NULL UNIQUE,   -- the directory name under STORAGE_ROOT
      created_at TEXT NOT NULL
    );
    """


def list_all() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT f.*,
                   COUNT(fi.id)               AS file_count,
                   COALESCE(SUM(fi.bytes), 0) AS total_bytes
              FROM folders f
              LEFT JOIN files fi ON fi.folder_id = f.id
             GROUP BY f.id
             ORDER BY f.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get(folder_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def slug_taken(slug: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM folders WHERE slug = ?", (slug,)).fetchone() is not None


def insert(name: str, slug: str) -> dict:
    with connect() as db:
        cur = db.execute(
            "INSERT INTO folders (name, slug, created_at) VALUES (?, ?, ?)",
            (name, slug, now()),
        )
        folder_id = cur.lastrowid
    return {"id": folder_id, "name": name, "slug": slug, "file_count": 0, "total_bytes": 0}


def delete(folder_id: int) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    return cur.rowcount > 0
