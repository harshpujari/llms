"""Library operations: the layer that owns the rules.

A folder is a row *and* a directory; a file is a row, some bytes, and the
markdown extracted from them. Keeping those in step -- and deciding what happens
when half of it fails -- is this module's job. The models below it only run SQL,
and storage/extract only touch the filesystem and MarkItDown respectively.
"""

# Custom libraries
from logger import configure_logging
from services import extract_service, storage_service

# Database modules
from models import file as file_model, folder as folder_model

logger = configure_logging(__name__)


# --- folders --------------------------------------------------------------


def list_folders() -> list[dict]:
    return folder_model.list_all()


def get_folder(folder_id: int) -> dict | None:
    return folder_model.get(folder_id)


def create_folder(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("folder name is required")

    base = storage_service.slugify(name)
    if not base:
        raise ValueError("folder name must contain a letter or number")

    # Two folders may legitimately share a display name; the slug is what has
    # to be unique, since it's a directory.
    slug, n = base, 1
    while folder_model.slug_taken(slug):
        n += 1
        slug = f"{base}-{n}"

    row = folder_model.insert(name, slug)
    storage_service.make_folder(slug)
    return row


def delete_folder(folder_id: int) -> bool:
    row = folder_model.get(folder_id)
    if not row:
        return False

    # Row first: if the rmtree fails the folder is already unlisted, which is
    # recoverable. The reverse leaves rows pointing at files that don't exist.
    folder_model.delete(folder_id)
    storage_service.remove_folder(row["slug"])
    return True


# --- files ----------------------------------------------------------------


def list_files(folder_id: int) -> list[dict]:
    return file_model.list_for(folder_id)


def save_file(folder_id: int, filename: str, source, mime: str | None = None) -> dict:
    parent = folder_model.get(folder_id)
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

    target, size, sha = storage_service.write_stream(slug, stored, source)

    # Extract before inserting the row. A file we can't convert never becomes
    # part of the library at all -- no orphan on disk, no row, no silent
    # zero-chunk document turning up later when retrieval finds nothing.
    try:
        text = extract_service.to_markdown(target)
    except ValueError:
        storage_service.remove_file(slug, stored)
        raise

    return file_model.insert(folder_id, display, stored, size, sha, mime, text)


def delete_file(file_id: int) -> bool:
    row = file_model.get(file_id)
    if not row:
        return False
    file_model.delete(file_id)
    storage_service.remove_file(row["slug"], row["stored_name"])
    return True


def file_text(file_id: int) -> dict | None:
    return file_model.get_text(file_id)


def backfill() -> dict:
    """Extract any file stored before extraction existed, or whose extraction
    failed. Non-destructive: a file that still won't convert keeps its bytes and
    simply stays without text, visible in the UI as un-extracted."""
    done, failed = [], []

    for row in file_model.without_text():
        path = storage_service.file_path(row["slug"], row["stored_name"])
        if not path.exists():
            failed.append({"name": row["name"], "error": "file missing from storage"})
            continue
        try:
            text = extract_service.to_markdown(path)
        except ValueError as exc:
            failed.append({"name": row["name"], "error": str(exc)})
            continue
        file_model.set_text(row["id"], text)
        done.append({"name": row["name"], "text_chars": len(text)})

    return {"extracted": done, "failed": failed}
