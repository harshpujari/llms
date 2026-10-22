"""The files table: a folder's contents, and the corpus RAG will index."""

# Custom libraries
from db_pool import Model
from models.chunk import EMBED_DIM

# Default libraries
import sqlite3


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
      -- Why extraction failed, if it did. The three states are derived:
      --   text_content NULL + extract_error NULL -> queued
      --   text_content NOT NULL                  -> extracted
      --   extract_error NOT NULL                 -> failed
      extract_error TEXT,
      -- Reserved for retrieval: null until chunked and embedded.
      indexed_at  TEXT,
      chunk_count INTEGER NOT NULL DEFAULT 0,
      -- The document summary, built from its chunks' summaries rather than
      -- its opening pages. NULL until every chunk has been summarized.
      summary       TEXT,
      summary_model TEXT,               -- which model wrote it, so it can be redone
      summarized_at TEXT,
      -- The embedding model this file's vectors came from. Vectors from two
      -- models can't be compared, so a change of EMBED_MODEL re-embeds.
      embed_model   TEXT,
      -- Why the document summary has no vector, when Ollama was up but
      -- refused it. Cleared with a new summary or a model change.
      vector_error  TEXT,
      UNIQUE (folder_id, stored_name)
    );

    CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id);
    """
    # Columns added after the table shipped. CREATE TABLE IF NOT EXISTS won't
    # alter a table that already exists, so these are applied by hand.
    __migrations__ = {
        "text_content": "TEXT",
        "extracted_at": "TEXT",
        "extract_error": "TEXT",
        "summary": "TEXT",
        "summary_model": "TEXT",
        "summarized_at": "TEXT",
        "embed_model": "TEXT",
        "vector_error": "TEXT",
    }

    # The document summary's vector, so "what is this file about?" can match a
    # whole document rather than one chunk of it. Triggers here, not in
    # __schema__: summary is a migrated column, and on an old database it
    # doesn't exist until the migrations have run.
    __search__ = f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS vectors_file_summary USING vec0(
      file_id   INTEGER PRIMARY KEY,
      embedding float[{EMBED_DIM}] distance_metric=cosine
    );

    CREATE TRIGGER IF NOT EXISTS files_vectors_delete AFTER DELETE ON files BEGIN
      DELETE FROM vectors_file_summary WHERE file_id = old.id;
    END;

    -- Re-chunking clears the summary; a rewrite replaces it. Either way the
    -- old vector describes text that is gone.
    CREATE TRIGGER IF NOT EXISTS files_summary_vector_stale AFTER UPDATE OF summary ON files
      WHEN new.summary IS NOT old.summary BEGIN
      DELETE FROM vectors_file_summary WHERE file_id = old.id;
    END;
    """

    @classmethod
    def after_init(cls, db: sqlite3.Connection) -> None:
        db.executescript(cls.__search__)

    # Every column except text_content, which can be megabytes and has no
    # business in a directory listing. Its length comes back instead. summary
    # stays out for the same reason; summarized_at is enough to show status.
    LIST_COLUMNS = """id, folder_id, name, stored_name, bytes, sha256, mime,
                      created_at, extracted_at, extract_error, indexed_at,
                      chunk_count, summarized_at,
                      LENGTH(text_content) AS text_chars"""
