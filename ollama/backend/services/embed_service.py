"""Embeddings: text -> vectors, so retrieval can compare by meaning.

nomic-embed-text was trained with task prefixes -- "search_document: " on what
gets searched, "search_query: " on the question -- and Ollama doesn't add them.
Measured on the Amaze brochure: with them, the fuel-tank chunk ranked first for
"What is the fuel tank capacity?"; without, third. They're added here, so
nothing else has to know.

No database here. Vectors come back as float32 bytes, the form the vec0 tables
store, checked against EMBED_DIM: a model of another size fails loudly instead
of filling the tables with vectors that can't be compared.

Errors follow summary_service: EmbedUnavailable means Ollama is down, slow, or
missing the model -- retry later, nothing is wrong with the text.
"""

# Custom libraries
from logger import configure_logging
from models.chunk import EMBED_DIM
from services.ollama_service import OLLAMA_HOST

# Default libraries
import os

# Installed libraries
import httpx
import numpy as np
from ollama import AsyncClient, Client, ResponseError

logger = configure_logging(__name__)

EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

# Texts per request: one call for a 300-chunk PDF would hold a request open for
# a minute and a half on CPU. 16 keeps each to a few seconds.
BATCH = 16

# Sync for the indexing worker, which runs on a thread; async for chat, where
# a cold model's few seconds of loading mustn't stall the server for everyone.
client = Client(host=OLLAMA_HOST, timeout=300)
aclient = AsyncClient(host=OLLAMA_HOST, timeout=60)


class EmbedUnavailable(Exception):
    """Ollama couldn't embed. Retry later; the text is fine."""


def _unavailable(exc: Exception) -> EmbedUnavailable:
    if isinstance(exc, ResponseError):
        # A missing model is a 404 here: fixed by pulling, not by giving up.
        return EmbedUnavailable(f"ollama error ({exc.status_code}): {exc.error}")
    return EmbedUnavailable(f"ollama unreachable at {OLLAMA_HOST}: {exc}")


def _to_bytes(vectors) -> list[bytes]:
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != EMBED_DIM:
        # A configuration problem, not a text one -- so "unavailable" until
        # it's fixed, never a failure recorded against the chunk.
        raise EmbedUnavailable(
            f"{EMBED_MODEL} returns {arr.shape[-1]}-dimension vectors but the "
            f"vector tables hold {EMBED_DIM}; changing EMBED_MODEL means re-creating them"
        )
    return [row.tobytes() for row in arr]


def embed_documents(texts: list[str]) -> list[bytes]:
    """Chunks and summaries, one vector each, in order."""
    vectors: list[bytes] = []
    for i in range(0, len(texts), BATCH):
        batch = [DOCUMENT_PREFIX + t for t in texts[i:i + BATCH]]
        try:
            response = client.embed(model=EMBED_MODEL, input=batch)
        except (ResponseError, ConnectionError, httpx.HTTPError) as exc:
            raise _unavailable(exc) from exc
        vectors += _to_bytes(response.embeddings)
    return vectors


async def embed_query(text: str) -> bytes:
    """A question, at chat time."""
    try:
        response = await aclient.embed(model=EMBED_MODEL, input=[QUERY_PREFIX + text])
    except (ResponseError, ConnectionError, httpx.HTTPError) as exc:
        raise _unavailable(exc) from exc
    return _to_bytes(response.embeddings)[0]
