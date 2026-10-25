"""Markdown -> chunks: the units retrieval summarizes, embeds, and cites.

Pure text in, dicts out -- no database, no model -- so it can be run against any
file's text_content to see exactly what retrieval will get.

Splitting follows the structure extract_service leaves behind. Its `## Page N`,
`## Slide N` and `## <sheet>` markers are real boundaries, so sections come
first and each heading becomes the chunk's citation label. Within a section,
paragraphs are packed to a token target, with a little overlap so a sentence
cut at a boundary survives whole on one side of it.

Tables are tidied first. PDF extraction lays them out as wide grids of mostly
empty cells -- `| | Fuel Tank Capacity (Litres) | | | | 35 |` -- which is mostly
punctuation to a search index and easy for a small model to misread. Each row
becomes a line saying what it says: `Fuel Tank Capacity (Litres): 35`. Only the
chunks are tidied; the extracted text the viewer shows stays as extracted.

Token counts are len/4 estimates. There is no tokenizer in the container, and
the sizes below are tuning knobs, not limits anything breaks on.
"""

# Default libraries
import math
import re
from typing import Optional

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


# --- tables ----------------------------------------------------------------
# Rules, in the order a row meets them:
#
#   header      a block's first row, when it's one of two strict kinds -- a
#               data row passing for a header would label everything under it
#               wrongly:
#                 columns   single-word labels, no digits (`| | | V | VX | ZX |`):
#                           a feature matrix's variants. Remembered for later
#                           blocks in the section too: PDF extraction often
#                           splits one feature table into a table per row.
#                 full      every cell filled, no digits: a real table's header,
#                           as spreadsheets and documents have.
#   merged      an empty leading cell takes the value above it. A label spanning
#               two rows in the PDF ("Brake System" over Front and Rear) comes
#               out on the first row only.
#   matrix      a label plus availability marks (◼ / - / O). With a header the
#               marks line up with, it says which columns: "available on ZX".
#               Without one it keeps the marks as they are -- never a guess at
#               which variant has what.
#   labelled    a row under a full header with the same number of cells:
#               "Basic — Monthly price: $9, Storage: 100 GB".
#   key: value  anything else: leading labels, then the values from the first
#               cell with a digit in it -- "Kerb weight · [kg]: 1 169 - 1 219".
#
# Whatever a rule can't place is kept, just without the empty cells: tidying
# must never drop a fact, or invent one.

POSITIVE = {"◼", "■", "●", "✓", "✔", "O", "Yes", "yes", "Y"}
NEGATIVE = {"-", "—", "–", "✗", "✘", "No", "no", "N"}
OPTIONAL = {"◻", "□", "(O)", "Optional", "optional"}
MARKS = POSITIVE | NEGATIVE | OPTIONAL


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [c.strip() for c in inner.split("|")]


def _header_kind(cells: list[str]) -> Optional[str]:
    filled = [c for c in cells if c]
    if len(filled) < 2 or any(c in MARKS or re.search(r"\d", c) for c in filled):
        return None
    if len(filled) == len(cells):
        return "full"
    if all(" " not in c for c in filled):
        return "columns"
    return None


def _availability(label: str, header: list[str], cells: list[str]) -> Optional[str]:
    """"Honda SENSING: available on ZX" -- only when every mark sits under a
    named column; None when they don't line up."""
    if len(header) != len(cells):
        return None
    yes, maybe, marked = [], [], 0
    for name, cell in zip(header, cells):
        if cell in MARKS:
            if not name:
                return None
            marked += 1
            if cell in POSITIVE:
                yes.append(name)
            elif cell in OPTIONAL:
                maybe.append(name)
    if not marked:
        return None
    text = f"available on {', '.join(yes)}" if yes else "not available"
    if maybe:
        text += f"; optional on {', '.join(maybe)}"
    return f"{label}: {text}"


def _row_line(
    cells: list[str], header: Optional[list[str]], full: bool, section_header: Optional[list[str]]
) -> Optional[str]:
    filled = [c for c in cells if c]
    if not filled:
        return None
    labels = [c for c in filled if c not in MARKS]
    marks = [c for c in filled if c in MARKS]

    if marks and len(labels) <= 1:
        # A matrix row. Marks with no label are a fragment the extraction cut
        # from the row above: nothing to say what they're for, so dropped.
        if not labels:
            return None
        for h in (header, section_header):
            if h and (line := _availability(labels[0], h, cells)):
                return line
        return f"{labels[0]}: {' '.join(marks)}"

    if header and full and len(header) == len(cells):
        pairs = [(h, c) for h, c in zip(header, cells) if c]
        named = [f"{h}: {c}" for h, c in pairs if h]
        unnamed = [c for h, c in pairs if not h]
        if named:
            return (" · ".join(unnamed) + " — " if unnamed else "") + ", ".join(named)

    if len(filled) == 1:
        return filled[0]
    first_value = next((i for i, c in enumerate(filled) if re.search(r"\d", c)), None)
    if first_value is None or first_value == 0:
        keys, values = filled[:-1], filled[-1:]
    else:
        keys, values = filled[:first_value], filled[first_value:]
    return f"{' · '.join(keys)}: {' | '.join(values)}"


def tidy_tables(markdown: str) -> str:
    """Markdown with every table rewritten as one line per row."""
    out: list[str] = []
    block: list[list[str]] = []
    section_header: Optional[list[str]] = None

    def flush() -> None:
        nonlocal section_header
        if not block:
            return
        rows = list(block)
        block.clear()
        header, kind = None, _header_kind(rows[0])
        if kind:
            header = rows.pop(0)
            if kind == "columns":
                section_header = header
            out.append(" · ".join(c for c in header if c))
        previous: Optional[list[str]] = None
        for cells in rows:
            # Merged cells: empty leading cells inherit from the row above --
            # but not in a row of marks, where a gap means a fragment.
            if previous and not any(c in MARKS for c in cells):
                lead = next((i for i, c in enumerate(cells) if c), len(cells))
                cells = [previous[i] if i < lead and i < len(previous) and previous[i] else c
                         for i, c in enumerate(cells)]
            if (line := _row_line(cells, header, kind == "full", section_header)):
                out.append(line)
            previous = cells
        out.append("")

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            if not TABLE_SEPARATOR.match(stripped):
                block.append(_cells(stripped))
            continue
        flush()
        if HEADING.match(line):
            section_header = None  # a new page or section: the old columns no longer apply
        out.append(line)
    flush()
    return "\n".join(out)


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
    markdown = tidy_tables(markdown or "")
    for section in _merge_small(_sections(markdown)):
        for text in _pack_section(section["blocks"]):
            chunks.append(
                {
                    "ord": len(chunks),
                    "heading": section["heading"],
                    "text": text,
                    "tokens": estimate_tokens(text),
                }
            )

    # Text that's nothing but headings packs into no blocks at all. It's still
    # text -- extraction refuses anything shorter than MIN_CHARS -- so it
    # becomes one chunk, rather than a file that can never be indexed.
    if not chunks and markdown and markdown.strip():
        text = markdown.strip()
        chunks.append({"ord": 0, "heading": None, "text": text, "tokens": estimate_tokens(text)})
    return chunks
