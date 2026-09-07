"""The folders table: the top level of the Library, one directory each."""

# Custom libraries
from db_pool import Model


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
