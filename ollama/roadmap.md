# RAG Roadmap

Adding retrieval to the local Llama stack. Storage is **SQLite + `sqlite-vec`** — one
pip install, one file in a volume, no new service. Embeddings come from the Ollama
container that's already running.

Everything stays local and CPU-only. That constraint shapes most decisions below.

---

## Architecture

```
frontend/  ── POST /chat ──▶  api  ──embed query──▶  ollama  (nomic-embed-text)
                              │
                              ├──KNN──▶  rag.db  (sqlite-vec + FTS5, in a volume)
                              │
                              └──chat w/ context──▶  ollama  (llama3.2:1b)
```

No new containers. `api` gains a dependency and a volume; `ollama` gains a second model.

---

## Phase 0 — Verify the assumptions

Two things can invalidate the whole plan, and both are ten-minute checks. Do these first.

- [ ] **Loadable extensions in the container's Python.** `sqlite-vec` is a SQLite
      extension, so the stdlib `sqlite3` module must be built with
      `--enable-loadable-sqlite-extensions`. The official `python:*-slim` images should
      have it, but confirm rather than assume:
      ```bash
      docker compose -f DockerCompse.yml exec api python -c \
        "import sqlite3; print(hasattr(sqlite3.Connection, 'enable_load_extension'))"
      ```
      If `False`, fall back to `pysqlite3-binary`, which ships its own SQLite build.
- [ ] **Embedding model works.** `ollama pull nomic-embed-text`, then check that
      `client.embed()` returns 768 floats and time it — CPU embedding throughput sets
      the ingestion budget.
- [ ] **Prefix behaviour.** `nomic-embed-text` was trained with asymmetric task prefixes
      (`search_document:` / `search_query:`). Whether Ollama applies them automatically
      is worth testing directly: embed the same string both ways and compare cosine
      similarity against a known-relevant pair. Getting this wrong quietly degrades
      every retrieval, and it won't look like a bug.

---

## Phase 1 — Storage layer

New file: `backend/store.py`. Everything SQLite lives here and nowhere else, so the
later swap to pgvector (if it ever happens) touches one module.

**Schema** — three tables sharing a rowid space, which is what makes hybrid search cheap:

```sql
CREATE TABLE documents (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  source     TEXT NOT NULL,          -- filename or URL
  sha256     TEXT NOT NULL UNIQUE,   -- re-ingest guard
  created_at TEXT NOT NULL
);

CREATE TABLE chunks (
  id      INTEGER PRIMARY KEY,
  doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord     INTEGER NOT NULL,          -- position in the document
  heading TEXT,                      -- nearest enclosing heading, for citations
  text    TEXT NOT NULL
);

CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id');
```

`chunks_vec.rowid` and `chunks_fts.rowid` both equal `chunks.id`. Insert into all three
in one transaction and the joins stay trivial.

- [ ] `connect()` — open, `enable_load_extension`, `sqlite_vec.load()`, `PRAGMA journal_mode=WAL`
- [ ] `init_schema()` — idempotent `CREATE TABLE IF NOT EXISTS`
- [ ] `add_document()` / `add_chunks()` — one transaction, all three tables
- [ ] `search_vec(embedding, k)` — `WHERE embedding MATCH ? AND k = ?`
- [ ] `delete_document(id)` — cascade must also clear the two virtual tables by rowid

**Persistence.** The DB goes at `/app/data/rag.db`, backed by a named volume so it
survives `--stop` and doesn't land in the repo (`./backend` is bind-mounted, so
anything written under `/app` directly would appear in the source tree):

```yaml
  api:
    volumes:
      - ./backend:/app
      - rag-data:/app/data     # nested mount over the bind

volumes:
  ollama-data:
  rag-data:
```

Sizing is a non-issue: 50k chunks × 768 dims × 4 bytes ≈ 150 MB, and brute-force KNN
over that is single-digit milliseconds.

---

## Phase 2 — Ingestion

New file: `backend/ingest.py`.

- [ ] **Chunking.** Split on markdown headings first, then paragraphs, packing to a
      ~400-token target with ~50 tokens of overlap. Carry the enclosing heading onto
      every chunk — it's both a retrieval signal and the citation label. Roughly 40
      lines; skip LangChain, it's a large dependency for a splitter.
- [ ] **Token counting.** No tokenizer in the container. Use `len(text) / 4` as the
      estimate and keep a safety margin; exactness isn't worth a dependency here.
- [ ] **Parsers.** `.txt` and `.md` need nothing. PDF needs `pypdf`. Start with plain
      text — it isolates retrieval quality from parsing quality while you're tuning.
- [ ] **`POST /ingest`.** Multipart upload → parse → chunk → batch-embed → store.
      Needs `python-multipart` in `requirements.txt` (FastAPI won't accept file uploads
      without it) and therefore a `--build`.
- [ ] **Idempotency.** Hash the file; if `sha256` already exists, replace that
      document's chunks rather than duplicating them.
- [ ] **`GET /documents` / `DELETE /documents/{id}`.** Needed almost immediately —
      without them a bad chunking run means deleting the volume.

Batch the embedding calls. `client.embed()` takes a list, and per-chunk round trips
dominate ingestion time on CPU.

---

## Phase 3 — Retrieval in the chat path

Changes to `backend/main.py`.

- [ ] Add `use_rag: bool = True` to `ChatRequest`.
- [ ] Embed the latest user turn, `search_vec(k=3)`.
- [ ] **Distance floor.** Below a similarity threshold, retrieve nothing and answer
      normally. Without this, every off-topic question drags in three irrelevant chunks
      and a 1B model will dutifully try to use them.
- [ ] Inject as a system message, not as a prefix on the user turn:
      ```
      Answer using only the context below. If the context doesn't
      contain the answer, say so.

      [1] {heading}: {text}
      [2] ...
      ```
- [ ] Return the sources on the stream — one `{"sources": [...]}` frame before the
      tokens start. The NDJSON protocol already tolerates new frame types; the frontend
      ignores what it doesn't recognise.

**Context budget.** `num_ctx` is currently 4096 and that's the binding constraint:

| | tokens |
|---|---|
| 3 chunks × 400 | ~1200 |
| system + instructions | ~150 |
| question + short history | ~250 |
| **left for the answer** | **~2500** |

Workable, but tight once history accumulates. Plan to raise `num_ctx` to 8192 and
re-measure throughput — prompt processing on CPU scales with context length, so this
costs latency on every turn, not just long ones.

**Mode interaction.** RAG belongs to `chat` only. `generate` has no system-message slot,
so context would have to be concatenated raw into the prompt. Simplest is to force
`use_rag=False` in generate mode and grey the toggle out in the UI.

---

## Phase 4 — UI

- [ ] Upload control in the footer (drag-and-drop onto the log is nicer, but a file
      input is a tenth of the code — start there).
- [ ] RAG on/off toggle beside the existing mode dropdown.
- [ ] Render sources under the assistant bubble as small numbered chips: `[1] heading`.
      Being able to see *which* chunk it used is what makes the next phase possible.
- [ ] Document list with delete.

---

## Phase 5 — Make retrieval good

This is where the actual work is. Phases 1–4 produce something that runs; this phase
decides whether it's useful. Same measure-change-remeasure loop as the eval work:

- [ ] **Build a small eval set.** 20–30 questions over your own documents, each labelled
      with the chunk that should answer it. Tedious, and it's the only thing that turns
      retrieval tuning from vibes into numbers.
- [ ] **Measure recall@k** — how often the right chunk is in the top-k. Retrieval failure
      and generation failure look identical in the final answer, and this separates them.
- [ ] **Hybrid search.** Add BM25 from the FTS5 table already in the schema and fuse with
      the vector ranking via Reciprocal Rank Fusion. Usually the single biggest quality
      jump, and it costs one extra query. Vector search alone is bad at exact
      identifiers, error codes, and proper nouns — precisely what you'd search a
      codebase or a manual for.
- [ ] **Tune chunk size** against the eval set — 200 / 400 / 800 tokens behave very
      differently and the answer is corpus-dependent.
- [ ] Try `mxbai-embed-large` (1024 dims) and compare recall@k against nomic on the same
      eval set. Bigger embeddings aren't automatically better for a given corpus.

---

## Later / probably not

- **Reranking.** A cross-encoder over the top 20 → 3 is the standard next lift, but
  there's no good small reranker in Ollama and running one on CPU alongside everything
  else will hurt. Revisit if recall@10 is high but recall@3 isn't — that's the exact
  signature of a reranking problem.
- **pgvector migration.** Only if this stops being single-user, or chunks pass ~1M.
  Keeping all SQL inside `store.py` is what keeps this a one-file change.
- **Query rewriting.** Using the 1B model to expand the query before retrieval. Cheap to
  try; on a model this small it may well add noise. Needs the eval set to judge.

---

## Open questions

1. **What's the corpus?** Code, docs, papers, and notes want different chunking. This
   changes Phase 2 more than anything else on this page.
2. **How big?** Under a few thousand chunks, ingestion is a non-issue. Tens of thousands
   on CPU means ingestion is a background job with progress reporting.
3. **Is `llama3.2:1b` the answer model?** Retrieval will expose its synthesis limits
   quickly. Worth keeping `MODEL` swappable so a 3B/8B comparison is one env var — the
   RAG plumbing is model-independent.

---

## Dependency delta

```
sqlite-vec          # vector search extension
python-multipart    # file uploads
pypdf               # only when PDFs are needed
```

Three lines, one `--build`. That's the whole infrastructure cost of this plan.
