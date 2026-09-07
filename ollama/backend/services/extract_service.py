"""Document -> markdown extraction, via Microsoft's MarkItDown.

Everything the Library accepts has to end up as markdown, because that is what
the retrieval pipeline chunks and embeds. A file we can't convert is a file that
would sit in the corpus contributing nothing, so the allowlist below is also the
upload policy: if it isn't here, it doesn't come in.

Deliberately absent: OCR and audio transcription. MarkItDown reads a PDF's text
layer, so a scanned or photographed PDF converts to nothing at all -- which is
why empty extractions are rejected loudly rather than stored as an empty file.
"""

# Default libraries
import re
from pathlib import Path

# Installed libraries
import pdfplumber
from markitdown import MarkItDown

# MarkItDown's own per-page routine: it finds column-aligned text and emits the
# pipe tables we render. Private, so guarded -- markitdown is pinned, and if a
# future version moves it we lose tables but still get text and page markers.
try:
    from markitdown.converters._pdf_converter import _extract_form_content_from_words
except ImportError:  # pragma: no cover
    _extract_form_content_from_words = None

# Anything shorter than this is treated as "nothing came out". A real document
# clears it trivially; a scanned PDF yields whitespace or a stray page number.
MIN_CHARS = 16

# Grouped for the UI, which renders these as the "supported formats" notice.
FORMAT_GROUPS = [
    {
        "label": "Documents",
        "extensions": [".pdf", ".docx", ".pptx", ".epub", ".msg"],
        "note": "PDFs must be native (text-layer). Scanned or photographed pages need OCR, which isn't enabled.",
    },
    {
        "label": "Spreadsheets & data",
        "extensions": [".xlsx", ".xls", ".csv", ".json", ".xml"],
    },
    {
        "label": "Text & web",
        "extensions": [".txt", ".md", ".markdown", ".html", ".htm", ".rst"],
    },
]

SUPPORTED = {ext for group in FORMAT_GROUPS for ext in group["extensions"]}

# Built once: MarkItDown loads and registers its converters on construction.
_converter = MarkItDown(enable_plugins=False)


def suffix(filename: str) -> str:
    return Path((filename or "").replace("\\", "/")).suffix.lower()


def is_supported(filename: str) -> bool:
    return suffix(filename) in SUPPORTED


def supported_list() -> str:
    return ", ".join(sorted(SUPPORTED))


SCANNED_PDF = (
    "no text found -- this looks like a scanned PDF. "
    "Only native PDFs with a text layer are supported."
)


def _page_markdown(page) -> str:
    """One page, using the same logic MarkItDown applies to it."""
    if _extract_form_content_from_words is not None:
        form = _extract_form_content_from_words(page)
        if form is not None:
            return form
    return page.extract_text() or ""


def _pdf_to_markdown(path: Path) -> str:
    """PDFs page by page, with a `## Page N` marker between them.

    MarkItDown loops the pages internally and joins them with a blank line, so
    the boundaries are gone by the time it returns -- there is nothing to
    post-process. Doing the loop here keeps its per-page output (verified
    identical on real brochures: same table count, same text) and keeps the page
    numbers, which retrieval will want for citations.
    """
    chunks: list[str] = []
    content_chars = 0

    try:
        with pdfplumber.open(path) as pdf:
            multi_page = len(pdf.pages) > 1
            for number, page in enumerate(pdf.pages, 1):
                text = _page_markdown(page).strip()
                # The marker goes in even for an empty page, so page numbers
                # stay true to the document rather than to what extracted.
                if multi_page:
                    chunks.append(f"## Page {number}")
                if text:
                    chunks.append(text)
                    content_chars += len(text)
                page.close()  # frees pdfplumber's per-page cache as we go
    except Exception as exc:
        raise ValueError(f"couldn't read this PDF ({type(exc).__name__})") from exc

    # Measured on the content, never the markers: a 30-page scan would otherwise
    # clear the threshold on its "## Page N" lines alone and look extracted.
    if content_chars < MIN_CHARS:
        raise ValueError(SCANNED_PDF)

    return "\n\n".join(chunks).strip()


# MarkItDown already marks slide boundaries, but as an HTML comment. Our
# renderer escapes HTML, so it would otherwise show up as literal text in the
# viewer -- and it wouldn't match the "## Page N" form the PDF path uses.
SLIDE_COMMENT = re.compile(r"^\s*<!--\s*Slide number:\s*(\d+)\s*-->\s*$", re.MULTILINE)


def _normalise_markers(text: str, ext: str) -> str:
    """Bring each format's own boundary marker to the same `## <unit> N` shape.

    Not every format has one to normalise:
      .pptx  an HTML comment per slide -> converted here
      .xlsx  already "## <sheet name>", which beats a synthetic number
      .docx  no page concept at all -- Word paginates when it renders, so the
             file simply doesn't record where pages fall
      .epub  chapters, which usually carry their own headings already
    """
    if ext == ".pptx":
        return SLIDE_COMMENT.sub(r"## Slide \1", text)
    return text


def to_markdown(path: Path) -> str:
    """Convert a file on disk. Raises ValueError with a message fit for the UI."""
    ext = suffix(path.name)
    if ext == ".pdf":
        return _pdf_to_markdown(path)

    try:
        result = _converter.convert(str(path))
    except Exception as exc:
        # MarkItDown raises a family of conversion errors, and the underlying
        # parsers raise their own. The user only needs to know it failed.
        raise ValueError(f"couldn't read this file ({type(exc).__name__})") from exc

    text = (result.text_content or "").strip()
    if len(text) < MIN_CHARS:
        raise ValueError("no text could be extracted from this file")
    return _normalise_markers(text, ext)
