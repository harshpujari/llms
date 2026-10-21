"""Chat: a conversation, answered from the attached documents when there are any.

With attachments, each message is answered in three moves:

  retrieve   the latest message is searched for in the attached folders and
             files (retrieval_service)
  ground     what was found replaces that message, for this request only: the
             excerpts, then the question, then a cue to answer. The history
             the UI keeps still holds what was typed
  stream     the model answers, after a {"retrieval": ...} frame telling the UI
             what was searched and what the answer can cite

The prompt's shape was measured, not guessed. With the excerpts in a system
message, labelled "[1] file · page", llama3.2:1b began its answers by copying
them -- citing [1] looked like the start of an excerpt. Tagged excerpts in the
user message, ending "Question: ... Answer in one or two sentences:", gave
5/6 and 6/6 correct with nothing copied. And when nothing matched, *any*
instruction about documents confused it ("hi" -> "there are no attached
documents. Is everything okay?"), so then there is none: the {"retrieval"}
frame tells the UI, which says so beside the answer.

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


def _attr(value: str) -> str:
    """Safe inside a double-quoted tag attribute."""
    return value.replace('"', "'")


def _grounded(found: dict, question: str) -> str:
    """The latest message as the model sees it: the excerpts, then the question."""
    excerpts = "\n".join(
        f'<excerpt n="{s["n"]}" source="{_attr(_source_label(s))}">\n{s["text"]}\n</excerpt>'
        for s in found["sources"]
    )
    instruction = (
        "Use only the excerpts to answer. Cite them by number, like [1]. "
        "If the answer isn't in them, say so."
    )
    unsearched = found["coverage"]["unsearched"]
    if unsearched:
        names = ", ".join(u["name"] for u in unsearched[:5])
        instruction += (
            f" Some attached documents couldn't be searched yet ({names}), "
            "so if the answer isn't here it may be in those."
        )
    return f"{instruction}\n\n{excerpts}\n\nQuestion: {question}\nAnswer in one or two sentences:"


def _source_label(s: dict) -> str:
    # The file's name without its extension, and where in it.
    name = s["file_name"].rsplit(".", 1)[0]
    return f"{name}, {s['heading']}" if s["heading"] else name


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


def _fit(history: list[Message], latest: Optional[str] = None) -> list[dict]:
    """As much recent history as the context holds, oldest turns dropped
    first. The latest message always stays -- as `latest` when given, the
    grounded version of it."""
    messages = [m.model_dump() for m in history]
    if latest is not None and messages:
        messages[-1] = {**messages[-1], "content": latest}

    budget = ollama_service.OPTIONS["num_ctx"] - ANSWER_TOKENS
    kept: list[dict] = []
    for message in reversed(messages):
        cost = estimate_tokens(message["content"]) + MESSAGE_OVERHEAD
        if kept and cost > budget:
            break
        kept.append(message)
        budget -= cost
    kept.reverse()

    if len(kept) < len(messages):
        logger.info("trimmed %d old message(s) to fit the context", len(messages) - len(kept))
    return kept


async def stream(req: ChatRequest) -> AsyncIterator[str]:
    """Yields NDJSON: a {"retrieval": ...} frame when documents are attached,
    then {"token": ...} frames, then {"done": true}."""
    attached = req.folder_ids or req.file_ids
    question = req.messages[-1].content if req.messages and req.messages[-1].role == "user" else ""
    if req.mode == "generate" or not attached or not question.strip():
        if req.mode == "generate":
            async for line in ollama_service.stream(req):
                yield line
        else:
            async for line in ollama_service.stream_messages(_fit(req.messages)):
                yield line
        return

    latest = None
    try:
        found = await retrieval_service.retrieve(question, req.folder_ids, req.file_ids)
        if found["sources"]:
            latest = _grounded(found, question)
        yield _frame(found)
    except Exception:
        # The answer still comes, just not from the documents -- and the frame
        # says so, rather than the reply quietly pretending to know.
        logger.exception("retrieval failed")
        yield _frame(None, "Couldn't search the attached documents.")

    async for line in ollama_service.stream_messages(_fit(req.messages, latest)):
        yield line
