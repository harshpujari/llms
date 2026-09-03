"""Thin streaming proxy in front of a local Ollama server.

The container holds no weights. It forwards to whatever OLLAMA_HOST points at --
by default the Ollama running natively on the Mac, so inference keeps Metal
acceleration instead of falling back to CPU inside the Docker VM.
"""

import json
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from ollama import AsyncClient
from pydantic import BaseModel

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL", "llama3.2:1b")

app = FastAPI(title="Local Llama API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncClient(host=OLLAMA_HOST)


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    # "chat"     -> wrapped in Llama 3.2's instruction template, history included
    # "generate" -> raw completion, no template, no history: the model just
    #               continues the text, the way a base model would.
    mode: str = "chat"


@app.get("/health")
async def health():
    """Reports the upstream too -- a 200 here means Ollama is genuinely reachable."""
    try:
        models = await client.list()
        return {
            "ok": True,
            "ollama": OLLAMA_HOST,
            "model": MODEL,
            "available": [m["model"] for m in models["models"]],
        }
    except Exception as exc:
        return {"ok": False, "ollama": OLLAMA_HOST, "error": str(exc)}


@app.post("/chat")
async def chat(req: ChatRequest):
    """Streams NDJSON: one {"token": ...} per chunk, then {"done": true}."""

    options = {"temperature": 0.7, "num_ctx": 4096}

    async def stream():
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
                    options={**options, "num_predict": 256},
                ):
                    yield json.dumps({"token": chunk["response"]}) + "\n"
            else:
                async for chunk in await client.chat(
                    model=MODEL,
                    messages=[m.model_dump() for m in req.messages],
                    stream=True,
                    options=options,
                ):
                    yield json.dumps({"token": chunk["message"]["content"]}) + "\n"
            yield json.dumps({"done": True}) + "\n"
        except Exception as exc:
            # The response has already started, so errors ride the stream itself
            # rather than surfacing as an HTTP status the browser can act on.
            yield json.dumps({"error": str(exc)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
