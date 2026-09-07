"""Request bodies for the chat endpoint."""

# Default libraries
from typing import Optional

# Installed libraries
from pydantic import BaseModel


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    # "chat"     -> wrapped in Llama 3.2's instruction template, history included
    # "generate" -> raw completion, no template, no history: the model just
    #               continues the text, the way a base model would.
    mode: str = "chat"
    # Accepted and ignored until retrieval lands -- the UI already tracks which
    # library folder the conversation is scoped to, and this is where it arrives.
    folder_id: Optional[int] = None
