"""Files: the contents of a folder, and the corpus RAG will eventually index."""

import hashlib

from . import db, folder, paths


def list_for(folder_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM files WHERE folder_id = ? ORDER BY name COLLATE NOCASE",
            (folder_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save(folder_id: int, filename: str, source, mime: str | None = None) -> dict:
    """Streams `source` (a file-like with .read) to disk, hashing as it goes."""
    parent = folder.get(folder_id)
    if not parent:
        raise LookupError("no such folder")

    directory = paths.folder_path(parent["slug"])
    directory.mkdir(parents=True, exist_ok=True)

    display = paths.display_name(filename)
    stored = paths.safe_filename(filename)

    # The same name twice is a re-upload, not an error -- keep both, numbered.
    stem, dot, ext = stored.rpartition(".")
    stem, ext = (stem, dot + ext) if dot else (stored, "")
    candidate, n = stored, 1
    while (directory / candidate).exists():
        n += 1
        candidate = f"{stem}-{n}{ext}"
    stored = candidate

    target = paths.within(directory, stored)
    digest = hashlib.sha256()
    size = 0

    try:
        with open(target, "wb") as out:
            while True:
                block = source.read(paths.CHUNK)
                if not block:
                    break
                size += len(block)
                if size > paths.MAX_UPLOAD_BYTES:
                    raise ValueError(
                        f"file exceeds the {paths.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
                    )
                digest.update(block)
                out.write(block)
    except Exception:
        target.unlink(missing_ok=True)  # no half-written file left behind
        raise

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO files (folder_id, name, stored_name, bytes, sha256, mime, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (folder_id, display, stored, size, digest.hexdigest(), mime, db.now()),
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


def delete(file_id: int) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            """SELECT fi.stored_name, f.slug
                 FROM files fi JOIN folders f ON f.id = fi.folder_id
                WHERE fi.id = ?""",
            (file_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    paths.within(paths.STORAGE_ROOT, row["slug"], row["stored_name"]).unlink(missing_ok=True)
    return True
