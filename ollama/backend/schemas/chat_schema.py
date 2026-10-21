"""Request bodies for the chat endpoint."""

# Installed libraries
from pydantic import BaseModel, Field, model_validator

# Folders and files together. Retrieval picks its best chunks from all of them,
# so more wouldn't overflow the context -- the cap keeps the chips readable and
# keeps unrelated documents away from a small model. The frontend mirrors it in
# js/state.js.
MAX_ATTACHMENTS = 5


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    # "chat"     -> wrapped in Llama 3.2's instruction template, history included
    # "generate" -> raw completion, no template, no history: the model just
    #               continues the text, the way a base model would.
    mode: str = "chat"
    # What the conversation answers from: whole folders and single files, as
    # attached in the UI. Accepted and ignored until retrieval lands.
    folder_ids: list[int] = Field(default_factory=list)
    file_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cap_attachments(self) -> "ChatRequest":
        if len(self.folder_ids) + len(self.file_ids) > MAX_ATTACHMENTS:
            raise ValueError(f"at most {MAX_ATTACHMENTS} folders and files can be attached")
        return self
