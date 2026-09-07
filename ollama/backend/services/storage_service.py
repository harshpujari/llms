"""The filesystem side of the Library: turning user-supplied names into paths,
safely, and moving bytes in and out of ./storage.

Folder and file names arrive from the browser, so they are attacker-controlled
input that ends up as a path. Everything here assumes the worst:
"../../etc/passwd", backslash separators, names that are nothing but dots.

The display name and the on-disk name are kept as separate columns precisely so
this module can be ruthless -- "../../etc" stays intact in the UI while the
directory it creates is just "etc".
"""

# Default libraries
import hashlib
import os
import re
import shutil
from pathlib import Path

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/app/storage"))

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CHUNK = 1024 * 1024


# --- naming ---------------------------------------------------------------


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:64]


def safe_filename(name: str) -> str:
    # Path().name drops any directory component, including Windows-style ones.
    base = Path((name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")
    return base[:120] or "file"


def display_name(name: str) -> str:
    return Path((name or "").replace("\\", "/")).name or "file"


# --- paths ----------------------------------------------------------------


def within(root: Path, *parts: str) -> Path:
    """Join, then prove the result stayed inside root whatever the parts were."""
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes storage root")
    return path


def folder_path(slug: str) -> Path:
    return within(STORAGE_ROOT, slug)


def file_path(slug: str, stored_name: str) -> Path:
    return within(STORAGE_ROOT, slug, stored_name)


def ensure_root() -> None:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


# --- directories ----------------------------------------------------------


def make_folder(slug: str) -> None:
    folder_path(slug).mkdir(parents=True, exist_ok=True)


def remove_folder(slug: str) -> None:
    shutil.rmtree(folder_path(slug), ignore_errors=True)


# --- files ----------------------------------------------------------------


def unique_name(slug: str, stored: str) -> str:
    """The same name twice is a re-upload, not an error -- keep both, numbered."""
    directory = folder_path(slug)
    stem, dot, ext = stored.rpartition(".")
    stem, ext = (stem, dot + ext) if dot else (stored, "")
    candidate, n = stored, 1
    while (directory / candidate).exists():
        n += 1
        candidate = f"{stem}-{n}{ext}"
    return candidate


def write_stream(slug: str, stored: str, source) -> tuple[Path, int, str]:
    """Stream `source` (a file-like with .read) to disk, hashing as it goes.

    Returns (path, size, sha256). Nothing large is held in memory, and a failed
    write leaves no partial file behind.
    """
    make_folder(slug)
    target = file_path(slug, stored)
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
        target.unlink(missing_ok=True)
        raise

    return target, size, digest.hexdigest()


def remove_file(slug: str, stored_name: str) -> None:
    file_path(slug, stored_name).unlink(missing_ok=True)


# Types the browser would execute, or treat as markup, in the API's own origin.
# Uploaded content is not trusted enough for that, so it goes back as plain text.
EXECUTABLE_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "text/xml",
}

# Extensions the browser can render inline; everything else downloads instead.
VIEWABLE_SUFFIXES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".markdown": "text/plain",
    ".rst": "text/plain",
    ".csv": "text/plain",
    ".json": "text/plain",
    ".xml": "text/plain",
    ".html": "text/plain",
    ".htm": "text/plain",
}


def safe_media_type(mime: str | None, name: str) -> str:
    """What to serve a stored file as.

    The extension decides, not the browser-supplied mime: the uploader chose
    that header, and an .html served as text/html would run its scripts on the
    API's origin.
    """
    suffix = Path(name).suffix.lower()
    if suffix in VIEWABLE_SUFFIXES:
        return VIEWABLE_SUFFIXES[suffix]
    if mime and mime.lower() not in EXECUTABLE_TYPES:
        return mime
    return "application/octet-stream"
