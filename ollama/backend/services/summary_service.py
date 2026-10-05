"""Summaries: a short one per chunk, and one per document built from those.

How a reader keeps notes -- a line in the margin for each page, and the book's
summary written from the margin notes, not by rereading every page. The chunk
summaries become a second way to find a chunk; the document summary is what
"what's in this file?" is answered from.

No database here, so any chunk can be summarized by hand to judge quality.

Errors come in two kinds, and the worker treats them differently:
  SummaryUnavailable  Ollama is down, slow, or missing the model. Nothing is
                      wrong with the text, so nothing gets marked failed --
                      otherwise a stopped container would fail every chunk in
                      seconds and none would ever be retried.
  ValueError          The model answered but produced nothing usable. Recorded
                      against the chunk, the way extraction errors are.
"""

# Custom libraries
from logger import configure_logging
from services.chunk_service import estimate_tokens
from services.ollama_service import MODEL, OLLAMA_HOST

# Default libraries
import os
import re

# Installed libraries
import httpx
from ollama import Client, ResponseError

logger = configure_logging(__name__)

# Follows the chat model unless set, so switching MODEL switches both.
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", MODEL)

# Sync on purpose: the indexing worker runs this on a thread, as extraction
# does. Generous timeout -- a cold model on CPU takes a while to load.
client = Client(host=OLLAMA_HOST, timeout=300)

# temperature 0: these are index entries, not prose -- the same chunk should
# summarize the same way twice. num_predict stops a small model rambling.
CHUNK_OPTIONS = {"temperature": 0, "num_ctx": 2048, "num_predict": 160}
DOCUMENT_OPTIONS = {"temperature": 0, "num_ctx": 4096, "num_predict": 400}

# How much summary text one document call may read. num_ctx 4096 minus the
# instructions and room for the answer.
DOCUMENT_INPUT_TOKENS = 2500


class SummaryUnavailable(Exception):
    """Ollama couldn't be used. Retry later; the text is fine."""


# The example does more than the rules: a 1B model ignores "copy the values"
# until it has seen what that looks like, and opens every summary with "This
# document appears to be..." -- which makes them all embed alike.
CHUNK_PROMPT = """You write short summaries of document excerpts for a search index.

Rules:
- 1 to 3 sentences of plain text. No bullet points, no preamble.
- Start with the subject itself. Never start with "This document" or "This excerpt".
- Include the specific values: names, numbers, units, dates, model names and
  codes, copied exactly as written.
- If it is a table, say what it lists and give its key values.
- Use only what is in the excerpt. Do not guess anything from the file name.

Example excerpt:
| Plan  | Monthly price | Storage |
| ----- | ------------- | ------- |
| Basic | $9            | 100 GB  |
| Pro   | $29           | 2 TB    |

Example summary:
Pricing table for two plans: Basic at $9 a month with 100 GB of storage, and Pro at $29 a month with 2 TB."""

PART_PROMPT = """Below are summaries of consecutive parts of one document, in order.

Write a summary of this stretch of the document in 3 to 5 sentences of plain
text. No preamble, no headings, no bullet points. Keep the most important
names, figures and dates exactly as written. Use only what is in the summaries;
do not guess anything from the file name."""

DOCUMENT_PROMPT = """Below are summaries of consecutive parts of one document, in order.

Write a summary of the whole document as one paragraph of 4 to 8 sentences. No
preamble, no headings, no bullet points, no bold. Say what kind of document it
is, what it is about, the main topics it covers, and the most important names,
figures and dates, exactly as written. Use only what is in the summaries; do
not guess anything from the file name."""

# Small models open with these whatever the prompt says.
PREAMBLE = re.compile(
    r"^\s*(?:(?:sure|okay|certainly)\b[^\n]*\n+)?"
    r"(?:here(?:'s| is| are)\b[^\n]*?:|summary\s*:)\s*",
    re.IGNORECASE,
)


# Markdown the prompt asked not to get. Summaries are embedded and shown
# inline, where "**Type:**" is noise.
MARKDOWN = re.compile(r"\*\*|__|^#+\s*|^\s*[-*]\s+", re.MULTILINE)


# A first line with no sentence end, before the real answer, is the model
# echoing the "Document: / Section:" header back as a title.
TITLE_LINE = re.compile(r"^[^\n.!?]{1,100}\n+(?=\S)")
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _clean(text: str, truncated: bool = False) -> str:
    text = PREAMBLE.sub("", text or "")
    text = MARKDOWN.sub("", text).strip()
    text = TITLE_LINE.sub("", text, count=1).strip().strip('"').strip()
    if truncated:
        # Cut off by num_predict: drop the unfinished sentence. A number cut
        # mid-way ("6.5J x 17") is worse in an index than no number at all.
        ends = list(SENTENCE_END.finditer(text))
        if ends:
            text = text[: ends[-1].end()]
    if not text:
        raise ValueError("the model returned an empty summary")
    return text


def _ask(system: str, user: str, options: dict) -> str:
    try:
        response = client.chat(
            model=SUMMARY_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options=options,
        )
    except ResponseError as exc:
        # A missing model is a 404 here. Like an outage, it is fixed by
        # pulling or restarting, not by giving up on the text.
        raise SummaryUnavailable(f"ollama error ({exc.status_code}): {exc.error}") from exc
    except (ConnectionError, httpx.HTTPError) as exc:
        raise SummaryUnavailable(f"ollama unreachable at {OLLAMA_HOST}: {exc}") from exc
    return _clean(response.message.content, truncated=response.done_reason == "length")


def summarize_chunk(text: str, heading: str | None, file_name: str) -> str:
    """1-3 sentences on one chunk. Raises SummaryUnavailable or ValueError."""
    user = f"Document: {file_name}\n"
    if heading:
        user += f"Section: {heading}\n"
    user += f"\nExcerpt:\n{text}"
    return _ask(CHUNK_PROMPT, user, CHUNK_OPTIONS)


def _label(first: str | None, last: str | None) -> str | None:
    if first and last and first != last:
        return f"{first} – {last}"
    return first or last


def _render(parts: list[dict]) -> str:
    return "\n".join(
        f"[{p['heading']}] {p['summary']}" if p["heading"] else p["summary"]
        for p in parts
    )


def _group(parts: list[dict]) -> list[list[dict]]:
    """Consecutive parts, packed up to what one call can read."""
    groups, current = [], []
    for part in parts:
        if current and estimate_tokens(_render(current + [part])) > DOCUMENT_INPUT_TOKENS:
            groups.append(current)
            current = []
        current.append(part)
    if current:
        groups.append(current)
    return groups


def summarize_document(file_name: str, chunk_summaries: list[dict]) -> str:
    """The whole document, from its chunks' summaries, in document order.

    Takes the rows ChunkRepository.get_chunk_summaries returns. When they
    don't fit one call, consecutive runs are summarized first and those are
    summarized in turn -- pages into chapters into the book.
    """
    parts = [
        {"heading": c["heading"], "summary": c["summary"]}
        for c in chunk_summaries
        if c.get("summary")
    ]
    if not parts:
        raise ValueError("no chunk summaries to build a document summary from")

    header = f"Document: {file_name}\n\nSummaries:\n"
    while estimate_tokens(_render(parts)) > DOCUMENT_INPUT_TOKENS:
        groups = _group(parts)
        logger.info("summarizing %s: %d parts -> %d", file_name, len(parts), len(groups))
        # One part per group. A group that is a single oversized part can't
        # shrink by regrouping, but summarizing it still does.
        parts = [
            {
                "heading": _label(group[0]["heading"], group[-1]["heading"]),
                "summary": _ask(PART_PROMPT, header + _render(group), DOCUMENT_OPTIONS),
            }
            for group in groups
        ]

    return _ask(DOCUMENT_PROMPT, header + _render(parts), DOCUMENT_OPTIONS)
