"""Library operations: the layer that owns the rules.

A folder is a row *and* a directory; a file is a row, some bytes, and the
markdown extracted from them. Keeping those in step -- and deciding what happens
when half of it fails -- is this module's job. Repositories below it only run
SQL, and storage/extract only touch the filesystem and MarkItDown respectively.
"""

# Custom libraries
from logger import configure_logging
from services import extract_service, storage_service

# Database modules
from repository.file_repository import FileRepository
from repository.folder_repository import FolderRepository

# Default libraries
import sqlite3
from typing import Optional

logger = configure_logging(__name__)


# --- folders --------------------------------------------------------------


def list_folders(db: sqlite3.Connection) -> list[dict]:
    return FolderRepository(db).get_all_folders()


def get_folder(db: sqlite3.Connection, folder_id: int) -> Optional[dict]:
    return FolderRepository(db).get_folder(folder_id)


def create_folder(db: sqlite3.Connection, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("folder name is required")

    base = storage_service.slugify(name)
    if not base:
        raise ValueError("folder name must contain a letter or number")

    repo = FolderRepository(db)

    # Two folders may legitimately share a display name; the slug is what has
    # to be unique, since it's a directory.
    slug, n = base, 1
    while repo.slug_exists(slug):
        n += 1
        slug = f"{base}-{n}"

    row = repo.create_folder({"name": name, "slug": slug})
    storage_service.make_folder(slug)
    return row


def delete_folder(db: sqlite3.Connection, folder_id: int) -> bool:
    repo = FolderRepository(db)
    row = repo.get_folder(folder_id)
    if not row:
        return False

    # Row first: if the rmtree fails the folder is already unlisted, which is
    # recoverable. The reverse leaves rows pointing at files that don't exist.
    repo.delete_folder(folder_id)
    storage_service.remove_folder(row["slug"])
    return True


# --- files ----------------------------------------------------------------


def list_files(db: sqlite3.Connection, folder_id: int) -> list[dict]:
    return FileRepository(db).get_files_by_folder(folder_id)


def get_file(db: sqlite3.Connection, file_id: int) -> Optional[dict]:
    return FileRepository(db).get_file(file_id)


def file_text(db: sqlite3.Connection, file_id: int) -> Optional[dict]:
    return FileRepository(db).get_file_text(file_id)


def file_on_disk(db: sqlite3.Connection, file_id: int) -> Optional[tuple]:
    """(path, display name, media type) for serving the original bytes back."""
    row = FileRepository(db).get_file(file_id)
    if not row:
        return None

    path = storage_service.file_path(row["slug"], row["stored_name"])
    if not path.exists():
        return None

    return path, row["name"], storage_service.safe_media_type(row["mime"], row["name"])


def save_file(
    db: sqlite3.Connection,
    folder_id: int,
    filename: str,
    source,
    mime: Optional[str] = None,
) -> dict:
    parent = FolderRepository(db).get_folder(folder_id)
    if not parent:
        raise LookupError("no such folder")

    # Checked before a single byte is written: an unsupported file can't be
    # converted, so it would only ever be dead weight in the corpus.
    if not extract_service.is_supported(filename):
        got = extract_service.suffix(filename) or "no extension"
        raise ValueError(f"{got} isn't supported. Allowed: {extract_service.supported_list()}")

    slug = parent["slug"]
    display = storage_service.display_name(filename)
    stored = storage_service.unique_name(slug, storage_service.safe_filename(filename))

    _target, size, sha = storage_service.write_stream(slug, stored, source)

    # The row lands with no text. Conversion is queued, not done here: a big
    # batch would otherwise hold the request open for minutes. The caller
    # enqueues the returned id -- this runs on a worker thread, and the queue
    # can only be touched from the event loop.
    return FileRepository(db).create_file(
        {
            "folder_id": folder_id,
            "name": display,
            "stored_name": stored,
            "bytes": size,
            "sha256": sha,
            "mime": mime,
        }
    )


def delete_file(db: sqlite3.Connection, file_id: int) -> bool:
    repo = FileRepository(db)
    row = repo.get_file(file_id)
    if not row:
        return False
    repo.delete_file(file_id)
    storage_service.remove_file(row["slug"], row["stored_name"])
    return True


def files_missing_text(db: sqlite3.Connection) -> list[int]:
    """Ids of every file without extracted text, previous failures included.

    Retrying a failure is the point here -- this is the manual "try again"
    path, unlike the worker's startup queue which skips known failures.
    """
    return [row["id"] for row in FileRepository(db).get_files_missing_text()]
