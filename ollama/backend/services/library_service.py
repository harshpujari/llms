"""Library operations: the layer that owns the rules.

A folder is a row *and* a directory; a file is a row, some bytes, and the
markdown extracted from them. Keeping those in step -- and deciding what happens
when half of it fails -- is this module's job. Repositories below it only run
SQL, and storage/extract only touch the filesystem and MarkItDown respectively.
"""

# Custom libraries
from logger import configure_logging
from services import extract_service, extraction_worker, indexing_worker, storage_service

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


MISC_NAME = "Miscellaneous"
MISC_SLUG = storage_service.slugify(MISC_NAME)


def misc_folder(db: sqlite3.Connection) -> dict:
    """Where uploads from the chat bar land, created on first use.

    Found by slug, not name: a "Miscellaneous" made by hand is the same folder,
    and deleting it just means the next upload brings it back. Called on the
    event loop, never a worker thread, so two uploads can't both create it.
    """
    row = FolderRepository(db).get_folder_by_slug(MISC_SLUG)
    if row:
        return row
    created = create_folder(db, MISC_NAME)
    logger.info("created %s for chat uploads", MISC_NAME)
    return FolderRepository(db).get_folder(created["id"])


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


def files_to_extract(db: sqlite3.Connection, force: bool = False) -> list[int]:
    """Ids to hand the worker.

    Without force: everything lacking text, previous failures included -- the
    manual "try again", unlike the worker's startup queue which skips failures.
    With force: everything, for re-running after the parser itself changes.
    """
    repo = FileRepository(db)
    rows = repo.get_all_file_ids() if force else repo.get_files_missing_text()
    return [row["id"] for row in rows]


# --- indexing progress ----------------------------------------------------


def indexing_status(
    db: sqlite3.Connection,
    folder_ids: Optional[list[int]] = None,
    file_ids: Optional[list[int]] = None,
) -> dict:
    """Where each file is, so the UI can say what isn't ready yet. Covers the
    given folders and files -- what the chat is attached to -- or the whole
    library when neither is given.

    Stages, in the order a file moves through them:
      extracting  no text yet -- not searchable at all
      reading     text and chunks exist, summaries still being written; already
                  searchable by its text, so answers can use it
      ready       document summary written
      failed      extraction failed, already shown on the file's row

    A file with every chunk attempted but no document summary (all of them
    failed) counts as ready: it is as searchable as it will get, and calling it
    "reading" would leave a banner up forever.
    """
    live = indexing_worker.status()
    active = live["current"]["file_id"] if live["current"] else None
    # Waiting to retry after an error that wasn't the file's own -- in either
    # worker. Shown, so a file that's stuck for a moment doesn't look stuck.
    retrying = {
        r["id"]: {"error": r["error"], "retry_in": r["retry_in"], "attempts": r["attempts"]}
        for r in extraction_worker.status()["retrying"] + live["retrying"]
    }

    folder_ids, file_ids = folder_ids or [], file_ids or []
    rows = FileRepository(db).get_indexing_rows(folder_ids, file_ids)

    counts = {"extracting": 0, "reading": 0, "ready": 0, "failed": 0}
    unfinished, failed = [], []
    for row in rows:
        if not row["extracted"]:
            stage = "failed" if row["failed"] else "extracting"
        elif row["summarized"]:
            stage = "ready"
        elif row["chunk_count"] == 0 or row["pending"] or row["id"] == active:
            # The active check covers the document-summary call itself, when
            # no chunks are left pending but the file isn't finished.
            stage = "reading"
        else:
            stage = "ready"

        counts[stage] += 1
        if stage in ("extracting", "reading"):
            unfinished.append(
                {
                    "id": row["id"],
                    "folder_id": row["folder_id"],
                    "name": row["name"],
                    "stage": stage,
                    # Clamped: right after new text, chunk_count is 0 while
                    # the old chunks are still there to be replaced.
                    "done": max(0, row["chunk_count"] - row["pending"]),
                    "total": row["chunk_count"],
                    "active": row["id"] == active,
                    "retrying": retrying.get(row["id"]),
                }
            )
        elif stage == "failed":
            failed.append(
                {
                    "id": row["id"],
                    "folder_id": row["folder_id"],
                    "name": row["name"],
                    "error": row["extract_error"],
                }
            )

    # Attached, but deleted since. Reported rather than raised, so one stale
    # attachment can't break the status of the rest -- and dropped by the UI
    # straight away, before SQLite hands its id to something new.
    found = {row["id"] for row in rows}
    folders = FolderRepository(db)
    missing = {
        "folder_ids": [f for f in folder_ids if not folders.get_folder(f)],
        "file_ids": [f for f in file_ids if f not in found],
    }

    # paused: Ollama unreachable, with seconds until the next try -- the one
    # thing worth telling the user that the tables can't.
    return {
        "counts": counts,
        "files": unfinished,
        "failed": failed,
        "missing": missing,
        "paused": live["paused"],
    }
