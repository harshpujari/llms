"""Document -> markdown extraction, via Microsoft's MarkItDown.

Everything the Library accepts has to end up as markdown, because that is what
the retrieval pipeline chunks and embeds. A file we can't convert is a file that
would sit in the corpus contributing nothing, so the allowlist below is also the
upload policy: if it isn't here, it doesn't come in.

Deliberately absent: OCR and audio transcription. MarkItDown reads a PDF's text
layer, so a scanned or photographed PDF converts to nothing at all -- which is
why empty extractions are rejected loudly rather than stored as an empty file.
"""

from pathlib import Path

from markitdown import MarkItDown

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


def to_markdown(path: Path) -> str:
    """Convert a file on disk. Raises ValueError with a message fit for the UI."""
    try:
        result = _converter.convert(str(path))
    except Exception as exc:
        # MarkItDown raises a family of conversion errors, and the underlying
        # parsers raise their own. The user only needs to know it failed.
        raise ValueError(f"couldn't read this file ({type(exc).__name__})") from exc

    text = (result.text_content or "").strip()
    if len(text) < MIN_CHARS:
        if suffix(path.name) == ".pdf":
            raise ValueError(
                "no text found -- this looks like a scanned PDF. "
                "Only native PDFs with a text layer are supported."
            )
        raise ValueError("no text could be extracted from this file")
    return text
