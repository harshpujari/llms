"""Chat endpoints: health of the upstream, and the streaming completion."""

# Custom libraries
from logger import configure_logging
from schemas.chat_schema import ChatRequest
from services import ollama_service

# Installed libraries
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = configure_logging(__name__)

chat_router = APIRouter(tags=["Chat"])


@chat_router.get("/health")
async def get_health():
    return await ollama_service.health()


@chat_router.post("/chat")
async def post_chat(req: ChatRequest):
    return StreamingResponse(
        ollama_service.stream(req), media_type="application/x-ndjson"
    )
