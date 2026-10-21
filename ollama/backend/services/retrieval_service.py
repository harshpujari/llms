"""Retrieval: from a question and the chat's attachments to the few passages
an answer should be written from.

Three searches over the attached files, each finding chunks a different way:

  chunk text by meaning      the passages themselves
  chunk summaries by meaning a second way in, for questions worded unlike
                             the text they're answered by
  keywords (FTS5, BM25)      exact names, codes and figures -- "1.0 TSI",
                             "205/55R17" -- which vectors are bad at

merged by Reciprocal Rank Fusion: a chunk found by several ranks above one
found by one. What reaches the prompt is always the chunk's own text; a summary
only ever helps find it, so a small model's summarizing slips never reach the
answer.

Files still extracting can't be searched, and files that are still being read
can -- by their text, from seconds after upload. Either way the result says
what was and wasn't searched, so the answer can say so too.
"""

# Custom libraries
from logger import configure_logging
from services import embed_service
from services.chunk_service import estimate_tokens
from services.embed_service import EmbedUnavailable

# Database modules
from db_pool import connect
from repository.chunk_repository import ChunkRepository
from repository.file_repository import FileRepository

# Default libraries
import re
import sqlite3
import time
from typing import Optional

# Installed libraries
from starlette.concurrency import run_in_threadpool

logger = configure_logging(__name__)

# Candidates per search, before fusing.
CANDIDATES = 20
# The usual RRF constant: damps the gap between first and second place, so
# agreement across searches outweighs a single top rank.
RRF_K = 60

# Cosine distance at which a chunk counts as relevant. Measured on the Amaze
# brochure: questions it answers had their best match at 0.23-0.44, unrelated
# ones ("Who wrote Hamlet?", "hi", the weather) at 0.46 and above. A thin
# margin on a small sample -- an eval set is where to tune it.
FLOOR = 0.45
# A keyword match earns a chunk a little more room: an exact "205/55R17" says
# more than its vector does. But only a little -- a stray word shared with an
# off-topic question shouldn't drag in a passage that's far off in meaning.
KEYWORD_FLOOR = 0.55

# Room for passages in the prompt. On CPU the model reads its whole prompt
# before the first token, about 11 ms a token here -- measured cold on the
# Amaze brochure: ~1,460 tokens of excerpts, 6/6 correct, first token ~18 s;
# ~700 tokens, 5/6 correct, ~6 s. Six questions can't tell 5/6 from 6/6; the
# twelve seconds are certain. Raise it for accuracy over speed.
CONTEXT_TOKENS = 900
MAX_SOURCES = 5

# Words that say how a question is asked, not what it's about. Matching them
# would rank passages by how often they use "the".
STOPWORDS = set("""
a an the and or but if then so of to in on at by for from with without about
into over under is are was were be been being am do does did done have has had
having can could will would shall should may might must i me my we our you
your it its they them their he she his her this that these those there here
what which who whom whose when where why how much many any some all each every
not no yes please tell show give explain describe say list find know get got
like just also than too very really document documents file files
""".split())

TOKEN = re.compile(r"\w[\w./-]*\w|\w")


def keyword_query(question: str) -> str:
    """The question as a safe FTS5 expression: its content words, each quoted
    and OR'd. Quoting keeps "205/55R17" together as written, and makes any FTS
    syntax the question happens to contain inert. Empty if nothing's left."""
    words: list[str] = []
    for word in TOKEN.findall(question.lower()):
        if len(word) > 1 and word not in STOPWORDS and word not in words:
            words.append(word)
    return " OR ".join(f'"{w}"' for w in words[:16])


def _coverage(files: list[dict]) -> dict:
    """What was searched, and why the rest wasn't -- for "answered from 6 of 8"."""
    unsearched = []
    for f in files:
        if f["searchable"]:
            continue
        if f["failed"] and not f["extracted"]:
            reason = "couldn't be read"
        elif not f["extracted"]:
            reason = "still extracting"
        else:
            reason = "still being indexed"
        unsearched.append({"id": f["id"], "name": f["name"], "reason": reason})
    return {
        "total": len(files),
        "searched": len(files) - len(unsearched),
        "unsearched": unsearched,
    }


def _search(question: str, vector: Optional[bytes], files: list[dict]) -> list[dict]:
    """Runs on a worker thread, with its own connection. Returns the sources,
    best first, numbered for citing."""
    ids = [f["id"] for f in files]
    by_id = {f["id"]: f for f in files}
    db = connect()
    try:
        chunks, docs = ChunkRepository(db), FileRepository(db)
        text = chunks.search_text_vectors(vector, ids, CANDIDATES) if vector else []
        summary = chunks.search_summary_vectors(vector, ids, CANDIDATES) if vector else []
        try:
            keyword = chunks.search_keywords(keyword_query(question), ids, CANDIDATES)
        except sqlite3.OperationalError as exc:
            # keyword_query quotes everything, so this shouldn't happen -- but
            # a bad keyword search must cost the keywords, not the answer.
            logger.warning("keyword search failed for %r: %s", question, exc)
            keyword = []
        best_doc = docs.search_summaries(vector, ids, 1) if vector else []

        # Fuse: each list adds 1 / (RRF_K + rank) for each chunk it found.
        score: dict[int, float] = {}
        for hits in (text, summary, keyword):
            for rank, hit in enumerate(hits, 1):
                score[hit["chunk_id"]] = score.get(hit["chunk_id"], 0) + 1 / (RRF_K + rank)

        # The closest either vector came. Keyword hits the vector searches
        # didn't reach get their distance looked up, so they can be judged too.
        distance: dict[int, float] = {}
        for hit in text + summary:
            distance[hit["chunk_id"]] = min(distance.get(hit["chunk_id"], 2.0), hit["distance"])
        keyword_ids = {hit["chunk_id"] for hit in keyword}
        if vector:
            distance.update(chunks.text_distances(vector, [c for c in keyword_ids if c not in distance]))

        def relevant(chunk_id: int) -> bool:
            if not vector:
                return chunk_id in keyword_ids  # embedding model down: keywords only
            d = distance.get(chunk_id, 2.0)
            return d <= FLOOR or (chunk_id in keyword_ids and d <= KEYWORD_FLOOR)

        ranked = sorted((c for c in score if relevant(c)), key=lambda c: -score[c])
        rows = {r["id"]: r for r in chunks.get_chunks(ranked[: MAX_SOURCES * 2])}
    finally:
        db.close()

    sources: list[dict] = []
    used = 0

    # "What is this file about?" is answered by the document summary, not by
    # any one passage -- so when it beats every chunk, it leads.
    best_chunk = min((distance[c] for c in ranked if c in distance), default=2.0)
    if best_doc and best_doc[0]["distance"] <= FLOOR and best_doc[0]["distance"] < best_chunk:
        f = by_id[best_doc[0]["file_id"]]
        tokens = estimate_tokens(f["summary"])
        sources.append({
            "kind": "document", "file_id": f["id"], "file_name": f["name"], "heading": "Summary",
            "text": f["summary"], "tokens": tokens, "distance": best_doc[0]["distance"],
        })
        used += tokens

    for chunk_id in ranked:
        row = rows.get(chunk_id)
        if not row:
            continue  # deleted since the search
        if len(sources) >= MAX_SOURCES or (sources and used + row["tokens"] > CONTEXT_TOKENS):
            break
        sources.append({
            "kind": "chunk", "chunk_id": row["id"], "file_id": row["file_id"],
            "file_name": row["file_name"], "heading": row["heading"], "text": row["text"],
            "tokens": row["tokens"], "distance": distance.get(chunk_id),
        })
        used += row["tokens"]

    for n, source in enumerate(sources, 1):
        source["n"] = n
    return sources


async def retrieve(question: str, folder_ids: list[int], file_ids: list[int]) -> dict:
    """What to answer `question` from, among the attached folders and files.

    Returns sources (best first, numbered [1]..[n]), `coverage` of what was
    and wasn't searched, and `keywords_only` when the embedding model was
    unavailable.
    """
    started = time.time()

    def attached() -> list[dict]:
        db = connect()
        try:
            return FileRepository(db).get_searchable_files(folder_ids, file_ids)
        finally:
            db.close()

    files = await run_in_threadpool(attached)
    coverage = _coverage(files)
    searchable = [f for f in files if f["searchable"]]
    result = {"sources": [], "coverage": coverage, "keywords_only": False}
    if not searchable:
        return result

    try:
        vector = await embed_service.embed_query(question)
    except EmbedUnavailable as exc:
        # Keywords still find exact names and codes; better than no search.
        logger.warning("searching by keywords only: %s", exc)
        vector, result["keywords_only"] = None, True

    sources = await run_in_threadpool(_search, question, vector, searchable)
    result["sources"] = sources
    logger.info(
        "retrieved %d source(s) from %d/%d file(s) in %.0f ms%s",
        len(sources), coverage["searched"], coverage["total"],
        (time.time() - started) * 1000, " (keywords only)" if result["keywords_only"] else "",
    )
    return result
