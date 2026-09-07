"""Inference: everything that talks to the Ollama server.

Ollama runs in its own container, so the Docker VM has no Metal access and this
is CPU-only -- the trade for a stack with nothing installed on the host.
"""

# Custom libraries
from logger import configure_logging
from schemas.chat_schema import ChatRequest

# Default libraries
import json
import os
from typing import AsyncIterator

# Installed libraries
from ollama import AsyncClient

logger = configure_logging(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL", "llama3.2:1b")

OPTIONS = {"temperature": 0.7, "num_ctx": 4096}

client = AsyncClient(host=OLLAMA_HOST)


async def health() -> dict:
    """Reports the upstream too -- ok:true means Ollama is genuinely reachable."""
    try:
        listing = await client.list()
        return {
            "ok": True,
            "ollama": OLLAMA_HOST,
            "model": MODEL,
            "available": [m["model"] for m in listing["models"]],
        }
    except Exception as exc:
        logger.warning("ollama unreachable at %s: %s", OLLAMA_HOST, exc)
        return {"ok": False, "ollama": OLLAMA_HOST, "error": str(exc)}


async def stream(req: ChatRequest) -> AsyncIterator[str]:
    """Yields NDJSON: one {"token": ...} per chunk, then {"done": true}."""
    try:
        if req.mode == "generate":
            # Only the latest user turn: a raw continuation has no notion of
            # conversation, and num_predict stops it rambling to num_ctx.
            prompt = next(
                (m.content for m in reversed(req.messages) if m.role == "user"), ""
            )
            async for chunk in await client.generate(
                model=MODEL,
                prompt=prompt,
                stream=True,
                options={**OPTIONS, "num_predict": 256},
            ):
                yield json.dumps({"token": chunk["response"]}) + "\n"
        else:
            async for chunk in await client.chat(
                model=MODEL,
                messages=[m.model_dump() for m in req.messages],
                stream=True,
                options=OPTIONS,
            ):
                yield json.dumps({"token": chunk["message"]["content"]}) + "\n"
        yield json.dumps({"done": True}) + "\n"
    except Exception as exc:
        # The response has already started, so errors ride the stream itself
        # rather than surfacing as an HTTP status the browser can act on.
        logger.exception("chat stream failed")
        yield json.dumps({"error": str(exc)}) + "\n"
