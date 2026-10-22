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
missing the model -- retry later, nothing is wrong with the text. A text it
refuses while otherwise working comes back as None instead, so one bad chunk
can't stall a file, or the queue behind it.
"""

# Custom libraries
from logger import configure_logging
from models.chunk import EMBED_DIM
from services.ollama_service import OLLAMA_HOST

# Default libraries
import os
import time
from typing import Optional

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


def _request(texts: list[str]) -> list[bytes]:
    response = client.embed(model=EMBED_MODEL, input=[DOCUMENT_PREFIX + t for t in texts])
    return _to_bytes(response.embeddings)


def _healthy() -> bool:
    try:
        _request(["ok"])
        return True
    except Exception:
        return False


def _one(text: str) -> Optional[bytes]:
    """A text the batch it was in failed on: whole, then its first half --
    whatever in it the model chokes on may be in the rest, and half a chunk
    still says most of what it's about. None if Ollama is up but refuses it
    both ways."""
    for attempt in (text, text[: max(200, len(text) // 2)]):
        try:
            return _request([attempt])[0]
        except ResponseError as exc:
            if exc.status_code == 404:
                raise _unavailable(exc) from exc
            failure = exc
        except (ConnectionError, httpx.HTTPError) as exc:
            raise _unavailable(exc) from exc
    if _healthy():
        logger.warning("%s refuses a text (%s): %r", EMBED_MODEL, failure.status_code, text[:60])
        return None
    raise _unavailable(failure) from failure


def embed_documents(texts: list[str]) -> list[Optional[bytes]]:
    """Chunks and summaries, one vector each, in order -- None for a text the
    model refuses while otherwise working. Raises EmbedUnavailable when it's
    down or missing."""
    vectors: list[Optional[bytes]] = []
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        try:
            vectors += _request(batch)
        except ResponseError as exc:
            if exc.status_code == 404:
                raise _unavailable(exc) from exc
            # One text can sink a whole batch: find which, one at a time.
            time.sleep(1)
            vectors += [_one(t) for t in batch]
        except (ConnectionError, httpx.HTTPError) as exc:
            raise _unavailable(exc) from exc
    return vectors


async def embed_query(text: str) -> bytes:
    """A question, at chat time."""
    try:
        response = await aclient.embed(model=EMBED_MODEL, input=[QUERY_PREFIX + text])
    except (ResponseError, ConnectionError, httpx.HTTPError) as exc:
        raise _unavailable(exc) from exc
    return _to_bytes(response.embeddings)[0]
