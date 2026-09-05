"""Folders: the top level of the Library, one directory each under ./storage."""

import shutil

from . import db, paths


def list_all() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT f.*,
                   COUNT(fi.id)               AS file_count,
                   COALESCE(SUM(fi.bytes), 0) AS total_bytes
              FROM folders f
              LEFT JOIN files fi ON fi.folder_id = f.id
             GROUP BY f.id
             ORDER BY f.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get(folder_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def create(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("folder name is required")

    base = paths.slugify(name)
    if not base:
        raise ValueError("folder name must contain a letter or number")

    with db.connect() as conn:
        # Two folders may legitimately share a display name; the slug is what
        # has to be unique, since it's a directory.
        slug, n = base, 1
        while conn.execute("SELECT 1 FROM folders WHERE slug = ?", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"

        cur = conn.execute(
            "INSERT INTO folders (name, slug, created_at) VALUES (?, ?, ?)",
            (name, slug, db.now()),
        )
        folder_id = cur.lastrowid

    paths.folder_path(slug).mkdir(parents=True, exist_ok=True)
    return {"id": folder_id, "name": name, "slug": slug, "file_count": 0, "total_bytes": 0}


def delete(folder_id: int) -> bool:
    row = get(folder_id)
    if not row:
        return False

    # Rows first: if the rmtree fails the folder is still listed, which is
    # recoverable. The reverse leaves rows pointing at files that don't exist.
    with db.connect() as conn:
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    shutil.rmtree(paths.folder_path(row["slug"]), ignore_errors=True)
    return True
