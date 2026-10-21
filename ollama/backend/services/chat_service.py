"""Chat: a conversation, answered from the attached documents when there are any.

With attachments, each message is answered in three moves:

  retrieve   the latest message is searched for in the attached folders and
             files (retrieval_service)
  ground     what was found goes into a system message, numbered for citing,
             written fresh for this message and never kept in the history
  stream     the model answers, after a {"retrieval": ...} frame telling the UI
             what was searched and what the answer can cite

Without attachments it's plain chat, as it always was. Either way the history
is trimmed to fit the context window: Ollama silently drops the *start* of an
over-long prompt, which is exactly where the instructions are.

Kept apart from ollama_service on purpose: embed_service and summary_service
import their settings from there, so retrieval can't be imported back into it.
"""

# Custom libraries
from logger import configure_logging
from schemas.chat_schema import ChatRequest, Message
from services import ollama_service, retrieval_service
from services.chunk_service import estimate_tokens

# Default libraries
import json
from typing import AsyncIterator, Optional

logger = configure_logging(__name__)

# Held back from the context window for the answer itself.
ANSWER_TOKENS = 700
# Per message, for the chat template's role markers.
MESSAGE_OVERHEAD = 4


def _system_prompt(found: dict) -> str:
    sources, coverage = found["sources"], found["coverage"]
    lines: list[str] = []

    if sources:
        lines += [
            "Answer using only the excerpts below, from the user's attached documents.",
            "- If they don't contain the answer, say you couldn't find it in the attached documents.",
            "- Cite the excerpts you used by number, like [1] or [2][3].",
            "- Copy names, figures and units exactly as written.",
        ]
    else:
        # "hi" with a folder attached should still get a greeting, not an
        # apology for the folder.
        lines += [
            "The user has attached documents, but nothing in them matched this message.",
            "If it asks about the documents, say you couldn't find that in the attached "
            "documents. Otherwise, reply normally.",
        ]

    if coverage["unsearched"]:
        names = ", ".join(u["name"] for u in coverage["unsearched"][:5])
        lines.append(
            f"Some attached documents couldn't be searched yet ({names}), "
            "so an answer missing here may be in those."
        )

    if sources:
        names = {s["file_id"]: s["file_name"] for s in sources}
        if found["about"]:
            lines.append("\nDocuments:")
            lines += [f"- {names[fid]}: {about}" for fid, about in found["about"].items()]
        lines.append("\nExcerpts:")
        for s in sources:
            label = f"{s['file_name']} · {s['heading']}" if s["heading"] else s["file_name"]
            lines.append(f"[{s['n']}] {label}\n{s['text']}\n")

    return "\n".join(lines)


def _frame(found: Optional[dict], error: Optional[str] = None) -> str:
    """What the UI shows under the answer: sources without their text, and what
    was searched. Sent before the first token."""
    if found is None:
        return json.dumps({"retrieval": {"error": error}}) + "\n"
    sources = [
        {k: s[k] for k in ("n", "kind", "file_id", "file_name", "heading")}
        for s in found["sources"]
    ]
    return json.dumps({"retrieval": {
        "sources": sources,
        "coverage": found["coverage"],
        "keywords_only": found["keywords_only"],
    }}) + "\n"


def _fit(system: Optional[str], history: list[Message]) -> list[dict]:
    """The system message, then as much recent history as the context holds.
    Oldest turns go first; the latest message always stays."""
    budget = ollama_service.OPTIONS["num_ctx"] - ANSWER_TOKENS
    if system:
        budget -= estimate_tokens(system) + MESSAGE_OVERHEAD

    kept: list[dict] = []
    for message in reversed(history):
        cost = estimate_tokens(message.content) + MESSAGE_OVERHEAD
        if kept and cost > budget:
            break
        kept.append(message.model_dump())
        budget -= cost
    kept.reverse()

    if len(kept) < len(history):
        logger.info("trimmed %d old message(s) to fit the context", len(history) - len(kept))
    return ([{"role": "system", "content": system}] if system else []) + kept


async def stream(req: ChatRequest) -> AsyncIterator[str]:
    """Yields NDJSON: a {"retrieval": ...} frame when documents are attached,
    then {"token": ...} frames, then {"done": true}."""
    attached = req.folder_ids or req.file_ids
    if req.mode == "generate" or not attached:
        if req.mode == "generate":
            async for line in ollama_service.stream(req):
                yield line
        else:
            async for line in ollama_service.stream_messages(_fit(None, req.messages)):
                yield line
        return

    question = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    system = None
    try:
        found = await retrieval_service.retrieve(question, req.folder_ids, req.file_ids)
        system = _system_prompt(found)
        yield _frame(found)
    except Exception:
        # The answer still comes, just not from the documents -- and the frame
        # says so, rather than the reply quietly pretending to know.
        logger.exception("retrieval failed")
        yield _frame(None, "Couldn't search the attached documents.")

    async for line in ollama_service.stream_messages(_fit(system, req.messages)):
        yield line
