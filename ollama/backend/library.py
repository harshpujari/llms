"""The Library: folders of files that RAG will later index and chat over.

Two stores, deliberately:

  * SQLite (in a volume) holds the metadata -- names, sizes, hashes, and the
    indexing state retrieval will need later.
  * STORAGE_ROOT (bind-mounted to ./storage) holds the bytes, in a directory
    tree that mirrors the folder list, so files stay inspectable from the host.

The database is the source of truth for what exists. Anything dropped into
./storage by hand is invisible until it's uploaded through the API -- there's no
row for it, so no hash, no size, and nothing for the indexer to key off.
"""

import hashlib
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("LIBRARY_DB", "/app/data/library.db"))
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/app/storage"))

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CHUNK = 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,          -- as typed, shown in the UI
  slug       TEXT NOT NULL UNIQUE,   -- the directory name under STORAGE_ROOT
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id          INTEGER PRIMARY KEY,
  folder_id   INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,         -- original filename, shown in the UI
  stored_name TEXT NOT NULL,         -- sanitised name on disk
  bytes       INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  mime        TEXT,
  created_at  TEXT NOT NULL,
  -- Reserved for retrieval: null until the file has been chunked and embedded.
  indexed_at  TEXT,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (folder_id, stored_name)
);

CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    # Off by default in SQLite: without this, ON DELETE CASCADE silently
    # does nothing and deleting a folder orphans all its file rows.
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init() -> None:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)


# --- path safety ----------------------------------------------------------
# Folder and file names arrive from the browser, so they are attacker-controlled
# input that gets turned into filesystem paths. Everything below assumes the
# worst: "../../etc/passwd", NUL bytes, names that are just dots.


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:64]


def safe_filename(name: str) -> str:
    # Path().name drops any directory component, including Windows-style ones.
    base = Path((name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")
    return base[:120] or "file"


def _within(root: Path, *parts: str) -> Path:
    """Join and prove the result stayed inside root, whatever the parts were."""
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes storage root")
    return path


def folder_path(slug: str) -> Path:
    return _within(STORAGE_ROOT, slug)


# --- folders --------------------------------------------------------------


def list_folders() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT f.*,
                   COUNT(fi.id)            AS file_count,
                   COALESCE(SUM(fi.bytes), 0) AS total_bytes
              FROM folders f
              LEFT JOIN files fi ON fi.folder_id = f.id
             GROUP BY f.id
             ORDER BY f.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_folder(folder_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def create_folder(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("folder name is required")

    base = slugify(name)
    if not base:
        raise ValueError("folder name must contain a letter or number")

    with connect() as db:
        # Two folders may legitimately share a display name; the slug is what
        # has to be unique, since it's a directory.
        slug, n = base, 1
        while db.execute("SELECT 1 FROM folders WHERE slug = ?", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"

        cur = db.execute(
            "INSERT INTO folders (name, slug, created_at) VALUES (?, ?, ?)",
            (name, slug, _now()),
        )
        folder_id = cur.lastrowid

    folder_path(slug).mkdir(parents=True, exist_ok=True)
    return {"id": folder_id, "name": name, "slug": slug, "file_count": 0, "total_bytes": 0}


def delete_folder(folder_id: int) -> bool:
    folder = get_folder(folder_id)
    if not folder:
        return False

    # Rows first: if the rmtree fails the folder is still listed, which is
    # recoverable. The reverse leaves rows pointing at files that don't exist.
    with connect() as db:
        db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    shutil.rmtree(folder_path(folder["slug"]), ignore_errors=True)
    return True


# --- files ----------------------------------------------------------------


def list_files(folder_id: int) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM files WHERE folder_id = ? ORDER BY name COLLATE NOCASE",
            (folder_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_file(folder_id: int, filename: str, source, mime: str | None = None) -> dict:
    """Streams `source` (a file-like with .read) to disk, hashing as it goes."""
    folder = get_folder(folder_id)
    if not folder:
        raise LookupError("no such folder")

    directory = folder_path(folder["slug"])
    directory.mkdir(parents=True, exist_ok=True)

    display = Path((filename or "").replace("\\", "/")).name or "file"
    stored = safe_filename(filename)

    # Same name twice is a re-upload, not an error -- keep both, numbered.
    stem, dot, ext = stored.rpartition(".")
    stem, ext = (stem, dot + ext) if dot else (stored, "")
    candidate, n = stored, 1
    while (directory / candidate).exists():
        n += 1
        candidate = f"{stem}-{n}{ext}"
    stored = candidate

    target = _within(directory, stored)
    digest = hashlib.sha256()
    size = 0

    try:
        with open(target, "wb") as out:
            while True:
                block = source.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(
                        f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
                    )
                digest.update(block)
                out.write(block)
    except Exception:
        target.unlink(missing_ok=True)  # no half-written file left behind
        raise

    with connect() as db:
        cur = db.execute(
            """INSERT INTO files (folder_id, name, stored_name, bytes, sha256, mime, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (folder_id, display, stored, size, digest.hexdigest(), mime, _now()),
        )
        file_id = cur.lastrowid

    return {
        "id": file_id,
        "folder_id": folder_id,
        "name": display,
        "stored_name": stored,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "mime": mime,
        "chunk_count": 0,
        "indexed_at": None,
    }


def delete_file(file_id: int) -> bool:
    with connect() as db:
        row = db.execute(
            """SELECT fi.stored_name, f.slug
                 FROM files fi JOIN folders f ON f.id = fi.folder_id
                WHERE fi.id = ?""",
            (file_id,),
        ).fetchone()
        if not row:
            return False
        db.execute("DELETE FROM files WHERE id = ?", (file_id,))

    _within(STORAGE_ROOT, row["slug"], row["stored_name"]).unlink(missing_ok=True)
    return True
