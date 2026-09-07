"""Files: the contents of a folder, and the corpus RAG will eventually index."""

import hashlib

from . import db, extract, folder, paths

# Every column except text_content, which can be megabytes and has no business
# in a directory listing. Its length comes back instead.
COLUMNS = """id, folder_id, name, stored_name, bytes, sha256, mime, created_at,
             extracted_at, indexed_at, chunk_count,
             LENGTH(text_content) AS text_chars"""


def list_for(folder_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM files WHERE folder_id = ? ORDER BY name COLLATE NOCASE",
            (folder_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_text(file_id: int) -> dict | None:
    """The extracted markdown, for inspecting what retrieval will actually see."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, name, text_content FROM files WHERE id = ?", (file_id,)
        ).fetchone()
    return dict(row) if row else None


def save(folder_id: int, filename: str, source, mime: str | None = None) -> dict:
    """Streams `source` (a file-like with .read) to disk, hashing as it goes."""
    parent = folder.get(folder_id)
    if not parent:
        raise LookupError("no such folder")

    # Checked before a single byte is written: an unsupported file can't be
    # converted, so it would only ever be dead weight in the corpus.
    if not extract.is_supported(filename):
        got = extract.suffix(filename) or "no extension"
        raise ValueError(f"{got} isn't supported. Allowed: {extract.supported_list()}")

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

    # Extract before committing the row. A file we can't convert never becomes
    # part of the library at all -- no orphan on disk, no row, no silent
    # zero-chunk document turning up later when retrieval finds nothing.
    try:
        text = extract.to_markdown(target)
    except ValueError:
        target.unlink(missing_ok=True)
        raise

    now = db.now()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO files (folder_id, name, stored_name, bytes, sha256, mime,
                                  created_at, text_content, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (folder_id, display, stored, size, digest.hexdigest(), mime, now, text, now),
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
        "created_at": now,
        "extracted_at": now,
        "text_chars": len(text),
        "chunk_count": 0,
        "indexed_at": None,
    }


def backfill() -> dict:
    """Extract any file stored before extraction existed, or whose extraction
    failed. Non-destructive: a file that still won't convert keeps its bytes and
    simply stays without text, visible in the UI as un-extracted."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT fi.id, fi.name, fi.stored_name, f.slug
                 FROM files fi JOIN folders f ON f.id = fi.folder_id
                WHERE fi.text_content IS NULL"""
        ).fetchall()

    done, failed = [], []
    for row in rows:
        path = paths.within(paths.STORAGE_ROOT, row["slug"], row["stored_name"])
        if not path.exists():
            failed.append({"name": row["name"], "error": "file missing from storage"})
            continue
        try:
            text = extract.to_markdown(path)
        except ValueError as exc:
            failed.append({"name": row["name"], "error": str(exc)})
            continue
        with db.connect() as conn:
            conn.execute(
                "UPDATE files SET text_content = ?, extracted_at = ? WHERE id = ?",
                (text, db.now(), row["id"]),
            )
        done.append({"name": row["name"], "text_chars": len(text)})

    return {"extracted": done, "failed": failed}


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
