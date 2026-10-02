"""Markdown -> chunks: the units retrieval summarizes, embeds, and cites.

Pure text in, dicts out -- no database, no model -- so it can be run against any
file's text_content to see exactly what retrieval will get.

Splitting follows the structure extract_service leaves behind. Its `## Page N`,
`## Slide N` and `## <sheet>` markers are real boundaries, so sections come
first and each heading becomes the chunk's citation label. Within a section,
paragraphs are packed to a token target, with a little overlap so a sentence
cut at a boundary survives whole on one side of it.

Token counts are len/4 estimates. There is no tokenizer in the container, and
the sizes below are tuning knobs, not limits anything breaks on.
"""

# Default libraries
import math
import re

TARGET_TOKENS = 400
# Carried from the end of one chunk to the start of the next, within a section
# only -- a new page or slide is a real break, not a cut mid-thought.
OVERLAP_TOKENS = 50
# Sections smaller than this join the next one. Sparse PDF pages would
# otherwise each become a 30-token chunk costing a full summarization call.
MIN_SECTION_TOKENS = 100

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
TABLE_SEPARATOR = re.compile(r"^\|?(\s*:?-{3,}:?\s*\|)*\s*:?-{3,}:?\s*\|?$")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


# --- structure -------------------------------------------------------------


def _sections(markdown: str) -> list[dict]:
    """Split on headings, ignoring `#` lines inside code fences."""
    sections = [{"heading": None, "lines": []}]
    in_fence = False

    for line in markdown.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match:
            sections.append({"heading": match.group(2), "lines": []})
        else:
            sections[-1]["lines"].append(line)

    return [
        {"heading": s["heading"], "blocks": _blocks(s["lines"])}
        for s in sections
    ]


def _blocks(lines: list[str]) -> list[str]:
    """Paragraphs, split on blank lines -- except inside a code fence."""
    blocks, current, in_fence = [], [], False

    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())

    return [b for b in blocks if b]


def _label(first: str | None, last: str | None) -> str | None:
    if first and last and first != last:
        return f"{first} – {last}"
    return first or last


def _merge_small(sections: list[dict]) -> list[dict]:
    """Join undersized sections onto the next, dropping empty ones.

    The joined sections keep their own heading line in the text, so a chunk
    labelled "Page 3 – Page 5" still shows where page 4 begins.
    """
    merged: list[dict] = []
    pending = None

    for section in sections:
        if not section["blocks"]:
            continue  # a blank page's marker, a heading with nothing under it
        if pending is None:
            pending = {
                "first": section["heading"],
                "last": section["heading"],
                "blocks": list(section["blocks"]),
            }
        else:
            if section["heading"]:
                pending["blocks"].append(f"## {section['heading']}")
            pending["blocks"].extend(section["blocks"])
            pending["last"] = section["heading"] or pending["last"]

        if estimate_tokens("\n\n".join(pending["blocks"])) >= MIN_SECTION_TOKENS:
            merged.append(pending)
            pending = None

    # A small trailing section has nothing after it to join, so it goes back
    # onto the previous one instead of standing alone.
    if pending is not None:
        if merged:
            last = merged[-1]
            if pending["first"]:
                last["blocks"].append(f"## {pending['first']}")
            last["blocks"].extend(pending["blocks"])
            last["last"] = pending["last"] or last["last"]
        else:
            merged.append(pending)

    return [
        {"heading": _label(s["first"], s["last"]), "blocks": s["blocks"]}
        for s in merged
    ]


# --- oversized blocks --------------------------------------------------------


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_table(block: str) -> bool:
    return all(_is_table_line(line) for line in block.splitlines())


def _runs(block: str) -> list[str]:
    """Consecutive table rows vs everything else.

    PDF extraction puts no blank line between a table and the text beside it,
    so a spec sheet arrives as one block of both. Each kind splits differently.
    """
    runs: list[list[str]] = []
    for line in block.splitlines():
        if runs and _is_table_line(runs[-1][-1]) == _is_table_line(line):
            runs[-1].append(line)
        else:
            runs.append([line])
    return ["\n".join(run) for run in runs]


def _split_table(block: str) -> list[str]:
    """Split by rows, repeating the header on every piece.

    A run of rows without its header is just numbers -- the model can't tell
    what any column means. A single row over the target is kept whole.
    """
    lines = block.splitlines()
    head_size = 2 if len(lines) > 1 and TABLE_SEPARATOR.match(lines[1].strip()) else 1
    head, rows = lines[:head_size], lines[head_size:]

    pieces, current = [], []
    for row in rows:
        if current and estimate_tokens("\n".join(head + current + [row])) > TARGET_TOKENS:
            pieces.append("\n".join(head + current))
            current = []
        current.append(row)
    if current or not pieces:
        pieces.append("\n".join(head + current))
    return pieces


def _pack(units: list[str], sep: str, limit: int) -> list[str]:
    """Greedily join units into pieces at or under the limit."""
    pieces, current = [], ""
    for unit in units:
        candidate = f"{current}{sep}{unit}" if current else unit
        if current and estimate_tokens(candidate) > limit:
            pieces.append(current)
            current = unit
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _split_prose(block: str) -> list[str]:
    """Lines, then sentences, then words -- each only where the last was too big.

    Lines come first because PDF text is line-shaped ("Cylinders 3"), with no
    sentence ends to split on. Pieces leave room for the overlap the next chunk
    opens with, or every chunk after the first would land at target + overlap.
    """
    limit = TARGET_TOKENS - OVERLAP_TOKENS
    lines = []
    for line in block.splitlines():
        if estimate_tokens(line) <= limit:
            lines.append(line)
            continue
        sentences = []
        for sentence in SENTENCE_END.split(line):
            if estimate_tokens(sentence) > limit:
                sentences.extend(_pack(sentence.split(), " ", limit))
            else:
                sentences.append(sentence)
        lines.extend(_pack(sentences, " ", limit))
    return _pack(lines, "\n", limit)


def _fit(block: str) -> list[str]:
    """A block as one or more pieces, each at or under the target."""
    if estimate_tokens(block) <= TARGET_TOKENS:
        return [block]
    pieces = []
    for run in _runs(block):
        pieces.extend(_split_table(run) if _is_table(run) else _split_prose(run))
    return pieces


# --- packing -----------------------------------------------------------------


def _tail(text: str) -> str:
    """The end of a chunk, to open the next one: whole sentences if they fit,
    else the last words."""
    budget = OVERLAP_TOKENS * 4
    tail = ""
    for sentence in reversed(SENTENCE_END.split(text)):
        candidate = f"{sentence} {tail}".strip()
        if len(candidate) > budget:
            break
        tail = candidate
    if tail:
        return tail

    words, kept = text.split(), []
    for word in reversed(words):
        if len(" ".join([word] + kept)) > budget:
            break
        kept.insert(0, word)
    return " ".join(kept)


def _pack_section(blocks: list[str]) -> list[str]:
    pieces = [piece for block in blocks for piece in _fit(block)]
    chunks, current = [], []
    # How many of `current`'s entries are carried overlap rather than new
    # text. A chunk is only emitted once it holds something new, or a piece
    # too big to sit beside the overlap would emit the overlap on its own.
    carried = 0

    for piece in pieces:
        too_big = estimate_tokens("\n\n".join(current + [piece])) > TARGET_TOKENS
        if len(current) > carried and too_big:
            chunks.append("\n\n".join(current))
            # Overlap from prose only: a table's last rows, out of their
            # table, would read as noise in the next chunk.
            overlap = "" if _is_table(current[-1]) else _tail(current[-1])
            # Skipped too when the piece leaves no room for it -- a full
            # table piece opens with its own header anyway.
            if overlap and estimate_tokens(f"{overlap}\n\n{piece}") > TARGET_TOKENS:
                overlap = ""
            current = [overlap] if overlap else []
            carried = len(current)
        current.append(piece)

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def to_chunks(markdown: str) -> list[dict]:
    """Split a file's markdown into rows shaped for the chunks table."""
    chunks = []
    for section in _merge_small(_sections(markdown or "")):
        for text in _pack_section(section["blocks"]):
            chunks.append(
                {
                    "ord": len(chunks),
                    "heading": section["heading"],
                    "text": text,
                    "tokens": estimate_tokens(text),
                }
            )
    return chunks
