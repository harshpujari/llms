"""Turning user-supplied names into filesystem paths, safely.

Folder and file names arrive from the browser, so they are attacker-controlled
input that ends up as a path. Everything here assumes the worst:
"../../etc/passwd", backslash separators, names that are nothing but dots.

The display name and the on-disk name are kept as separate columns precisely so
this module can be ruthless -- "../../etc" stays intact in the UI while the
directory it creates is just "etc".
"""

import os
import re
from pathlib import Path

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/app/storage"))

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CHUNK = 1024 * 1024


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


def within(root: Path, *parts: str) -> Path:
    """Join, then prove the result stayed inside root whatever the parts were."""
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes storage root")
    return path


def folder_path(slug: str) -> Path:
    return within(STORAGE_ROOT, slug)


def ensure_root() -> None:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
