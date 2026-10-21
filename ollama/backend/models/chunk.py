"""The chunks table: each file's markdown split for retrieval, with a summary.

Like a reader's margin notes: every chunk gets a short summary of its own, and
a file's chunk summaries are what its document summary is built from.

Alongside it, what retrieval searches: a keyword index over each chunk's text
and summary, and a vector for each of the two.
"""

# Custom libraries
from db_pool import Model

# Default libraries
import sqlite3

# nomic-embed-text's vector size. The vec0 tables are fixed to it: switching
# EMBED_MODEL to one of another size means dropping and re-embedding them.
EMBED_DIM = 768


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

    # Created after every table exists (see Model.after_init). Neither kind of
    # virtual table follows ON DELETE CASCADE, so triggers do the tidying.
    __search__ = f"""
    -- Keywords: exact names, codes and figures ("1.0 TSI", "205/55R17") that
    -- vectors are bad at. Reads its text from chunks rather than keeping a copy.
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
      text, summary, content='chunks', content_rowid='id'
    );

    CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
      INSERT INTO chunks_fts(rowid, text, summary) VALUES (new.id, new.text, new.summary);
    END;

    CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
      INSERT INTO chunks_fts(chunks_fts, rowid, text, summary)
        VALUES ('delete', old.id, old.text, old.summary);
    END;

    CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE OF text, summary ON chunks BEGIN
      INSERT INTO chunks_fts(chunks_fts, rowid, text, summary)
        VALUES ('delete', old.id, old.text, old.summary);
      INSERT INTO chunks_fts(rowid, text, summary) VALUES (new.id, new.text, new.summary);
    END;

    -- Meaning: one vector per chunk for its text, one for its summary. file_id
    -- rides along so a search can be limited to what the chat has attached.
    CREATE VIRTUAL TABLE IF NOT EXISTS vectors_chunk_text USING vec0(
      chunk_id  INTEGER PRIMARY KEY,
      embedding float[{EMBED_DIM}] distance_metric=cosine,
      file_id   INTEGER
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS vectors_chunk_summary USING vec0(
      chunk_id  INTEGER PRIMARY KEY,
      embedding float[{EMBED_DIM}] distance_metric=cosine,
      file_id   INTEGER
    );

    CREATE TRIGGER IF NOT EXISTS chunks_vectors_delete AFTER DELETE ON chunks BEGIN
      DELETE FROM vectors_chunk_text WHERE chunk_id = old.id;
      DELETE FROM vectors_chunk_summary WHERE chunk_id = old.id;
    END;

    -- A new summary makes the old one's vector wrong; the worker embeds again.
    CREATE TRIGGER IF NOT EXISTS chunks_summary_vector_stale AFTER UPDATE OF summary ON chunks
      WHEN new.summary IS NOT old.summary BEGIN
      DELETE FROM vectors_chunk_summary WHERE chunk_id = old.id;
    END;
    """

    @classmethod
    def after_init(cls, db: sqlite3.Connection) -> None:
        db.executescript(cls.__search__)
        # The triggers keep the keyword index current from here on. Chunks from
        # before it existed -- or any drift -- get one full rebuild.
        indexed = db.execute("SELECT COUNT(*) FROM chunks_fts_docsize").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if indexed != total:
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            db.commit()
