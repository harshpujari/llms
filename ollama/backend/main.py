"""Thin streaming proxy in front of a local Ollama server.

The container holds no weights. It forwards to whatever OLLAMA_HOST points at --
by default the Ollama running natively on the Mac, so inference keeps Metal
acceleration instead of falling back to CPU inside the Docker VM.
"""

import json
import os

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from ollama import AsyncClient

import models
from models import file, folder
from models.schemas import ChatRequest, FolderRequest

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL", "llama3.2:1b")

app = FastAPI(title="Local Llama API")

models.init()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncClient(host=OLLAMA_HOST)


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


# --- library --------------------------------------------------------------


@app.get("/folders")
async def get_folders():
    return folder.list_all()


@app.post("/folders", status_code=201)
async def post_folder(req: FolderRequest):
    try:
        return folder.create(req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/folders/{folder_id}", status_code=204)
async def remove_folder(folder_id: int):
    if not folder.delete(folder_id):
        raise HTTPException(404, "no such folder")


@app.get("/folders/{folder_id}/files")
async def get_files(folder_id: int):
    if not folder.get(folder_id):
        raise HTTPException(404, "no such folder")
    return file.list_for(folder_id)


@app.post("/folders/{folder_id}/files", status_code=201)
async def post_files(folder_id: int, files: list[UploadFile]):
    """Multi-upload. A rejected file doesn't abort the ones beside it."""
    saved, failed = [], []
    for upload in files:
        try:
            # UploadFile.file is a sync SpooledTemporaryFile, which is what
            # file.save streams from -- nothing large is held in memory.
            saved.append(
                file.save(folder_id, upload.filename, upload.file, upload.content_type)
            )
        except LookupError:
            raise HTTPException(404, "no such folder")
        except ValueError as exc:
            failed.append({"name": upload.filename, "error": str(exc)})
    return {"saved": saved, "failed": failed}


@app.delete("/files/{file_id}", status_code=204)
async def remove_file(file_id: int):
    if not file.delete(file_id):
        raise HTTPException(404, "no such file")


# --- chat -----------------------------------------------------------------


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
