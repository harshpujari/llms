"""SQLite connection and schema for the Library.

Metadata only. The file bytes live under STORAGE_ROOT (see paths.py) in a
directory tree mirroring the folder list; this database is the source of truth
for what exists. Anything dropped into ./storage by hand has no row, so no
hash, no size, and nothing for the indexer to key off -- it stays invisible.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("LIBRARY_DB", "/app/data/library.db"))

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


def now() -> str:
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


def init_schema() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
