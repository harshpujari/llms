"""The chunks table: each file's markdown split for retrieval, with a summary.

Like a reader's margin notes: every chunk gets a short summary of its own, and
a file's chunk summaries are what its document summary is built from.
"""

# Custom libraries
from db_pool import Model


class Chunk(Model):
    __tablename__ = "chunks"
    __schema__ = """
    CREATE TABLE IF NOT EXISTS chunks (
      id            INTEGER PRIMARY KEY,
      file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
      ord           INTEGER NOT NULL,   -- position in the document, 0-based
      heading       TEXT,               -- enclosing heading, e.g. "Page 3" -- the citation label
      text          TEXT NOT NULL,      -- the raw chunk: what the answer model reads
      tokens        INTEGER NOT NULL,   -- len(text)/4 estimate, for context budgeting
      -- The summary is a way to find the chunk, never a stand-in for it. The
      -- three states are derived, as on files:
      --   summary NULL + summary_error NULL -> pending
      --   summary NOT NULL                  -> summarized
      --   summary_error NOT NULL            -> failed
      summary       TEXT,               -- 1-3 sentences
      summary_model TEXT,               -- which model wrote it, so it can be redone
      summarized_at TEXT,
      -- Set on failure so one bad chunk isn't retried on every boot.
      summary_error TEXT,
      -- Also serves as the file_id index: it leads with file_id.
      UNIQUE (file_id, ord)
    );

    -- The worker's resume query: what's still left to summarize.
    CREATE INDEX IF NOT EXISTS chunks_pending ON chunks(file_id)
      WHERE summarized_at IS NULL AND summary_error IS NULL;
    """
