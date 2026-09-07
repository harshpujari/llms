"""The files table: a folder's contents, and the corpus RAG will index."""

# Custom libraries
from db_pool import Model


class File(Model):
    __tablename__ = "files"
    __schema__ = """
    CREATE TABLE IF NOT EXISTS files (
      id          INTEGER PRIMARY KEY,
      folder_id   INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
      name        TEXT NOT NULL,         -- original filename, shown in the UI
      stored_name TEXT NOT NULL,         -- sanitised name on disk
      bytes       INTEGER NOT NULL,
      sha256      TEXT NOT NULL,
      mime        TEXT,
      created_at  TEXT NOT NULL,
      -- The markdown the parser produced. This, not the original, is what
      -- retrieval chunks and embeds.
      text_content TEXT,
      extracted_at TEXT,
      -- Reserved for retrieval: null until chunked and embedded.
      indexed_at  TEXT,
      chunk_count INTEGER NOT NULL DEFAULT 0,
      UNIQUE (folder_id, stored_name)
    );

    CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id);
    """
    # Columns added after the table shipped. CREATE TABLE IF NOT EXISTS won't
    # alter a table that already exists, so these are applied by hand.
    __migrations__ = {
        "text_content": "TEXT",
        "extracted_at": "TEXT",
    }

    # Every column except text_content, which can be megabytes and has no
    # business in a directory listing. Its length comes back instead.
    LIST_COLUMNS = """id, folder_id, name, stored_name, bytes, sha256, mime,
                      created_at, extracted_at, indexed_at, chunk_count,
                      LENGTH(text_content) AS text_chars"""
