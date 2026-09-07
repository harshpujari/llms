"""The files table: a folder's contents, and the corpus RAG will index."""

# Custom libraries
from db_pool import Model, connect, now


class File(Model):
    __tablename__ = "files"
    __schema__ = """
    CREATE TABLE IF NOT EXISTS files (
      id          INTEGER PRIMARY KEY,
      folder_id   INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
      name        TEXT NOT NULL,         -- original filename, shown in the UI
      stored_name TEXT NOT NULL,         -- sanitised name on disk
      bytes       INTEGER NOT NULL,
      sha256      TEXT NOT NULL,
      mime        TEXT,
      created_at  TEXT NOT NULL,
      -- The markdown the parser produced. This, not the original, is what
      -- retrieval chunks and embeds.
      text_content TEXT,
      extracted_at TEXT,
      -- Reserved for retrieval: null until chunked and embedded.
      indexed_at  TEXT,
      chunk_count INTEGER NOT NULL DEFAULT 0,
      UNIQUE (folder_id, stored_name)
    );

    CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id);
    """
    __migrations__ = {
        "text_content": "TEXT",
        "extracted_at": "TEXT",
    }


# Every column except text_content, which can be megabytes and has no business
# in a directory listing. Its length comes back instead.
COLUMNS = """id, folder_id, name, stored_name, bytes, sha256, mime, created_at,
             extracted_at, indexed_at, chunk_count,
             LENGTH(text_content) AS text_chars"""


def list_for(folder_id: int) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            f"SELECT {COLUMNS} FROM files WHERE folder_id = ? ORDER BY name COLLATE NOCASE",
            (folder_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get(file_id: int) -> dict | None:
    """Joined to folders, because every filesystem path needs the slug."""
    with connect() as db:
        row = db.execute(
            """SELECT fi.id, fi.folder_id, fi.name, fi.stored_name, fi.bytes,
                      fi.sha256, fi.mime, fi.created_at, fi.extracted_at,
                      fi.indexed_at, fi.chunk_count,
                      LENGTH(fi.text_content) AS text_chars,
                      f.slug
                 FROM files fi JOIN folders f ON f.id = fi.folder_id
                WHERE fi.id = ?""",
            (file_id,),
        ).fetchone()
    return dict(row) if row else None


def get_text(file_id: int) -> dict | None:
    """The extracted markdown, for inspecting what retrieval will actually see."""
    with connect() as db:
        row = db.execute(
            "SELECT id, name, text_content FROM files WHERE id = ?", (file_id,)
        ).fetchone()
    return dict(row) if row else None


def without_text() -> list[dict]:
    """Files that have no extracted markdown -- the backfill queue."""
    with connect() as db:
        rows = db.execute(
            """SELECT fi.id, fi.name, fi.stored_name, f.slug
                 FROM files fi JOIN folders f ON f.id = fi.folder_id
                WHERE fi.text_content IS NULL"""
        ).fetchall()
    return [dict(r) for r in rows]


def insert(
    folder_id: int,
    name: str,
    stored_name: str,
    size: int,
    sha256: str,
    mime: str | None,
    text: str,
) -> dict:
    created = now()
    with connect() as db:
        cur = db.execute(
            """INSERT INTO files (folder_id, name, stored_name, bytes, sha256, mime,
                                  created_at, text_content, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (folder_id, name, stored_name, size, sha256, mime, created, text, created),
        )
        file_id = cur.lastrowid

    return {
        "id": file_id,
        "folder_id": folder_id,
        "name": name,
        "stored_name": stored_name,
        "bytes": size,
        "sha256": sha256,
        "mime": mime,
        "created_at": created,
        "extracted_at": created,
        "text_chars": len(text),
        "chunk_count": 0,
        "indexed_at": None,
    }


def set_text(file_id: int, text: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE files SET text_content = ?, extracted_at = ? WHERE id = ?",
            (text, now(), file_id),
        )


def delete(file_id: int) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    return cur.rowcount > 0
