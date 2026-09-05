"""Data models for the Library.

Two stores, deliberately:

  * SQLite (in a volume) holds the metadata -- names, sizes, hashes, and the
    indexing state retrieval will need later.
  * ./storage (bind-mounted) holds the bytes, in a directory tree that mirrors
    the folder list, so files stay inspectable from the host.

    db       connection, schema, timestamps
    paths    user input -> filesystem paths, safely
    folder   folders
    file     files within a folder
    schemas  Pydantic request bodies
"""

from . import db, file, folder, paths, schemas

__all__ = ["db", "file", "folder", "paths", "schemas", "init"]


def init() -> None:
    """Create the storage root and the tables. Safe to call on every boot."""
    paths.ensure_root()
    db.init_schema()
