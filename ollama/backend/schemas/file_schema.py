"""Request and response bodies for file endpoints."""

# Default libraries
from typing import Optional

# Installed libraries
from pydantic import BaseModel


class FileResponse(BaseModel):
    id: int
    folder_id: int
    name: str
    stored_name: str
    bytes: int
    sha256: str
    mime: Optional[str] = None
    created_at: str
    extracted_at: Optional[str] = None
    indexed_at: Optional[str] = None
    chunk_count: int = 0
    # None means the file predates extraction, or wouldn't convert.
    text_chars: Optional[int] = None


class FileRejection(BaseModel):
    name: Optional[str] = None
    error: str


class UploadResponse(BaseModel):
    """A rejected file doesn't abort the ones beside it, so both lists come back."""

    saved: list[FileResponse]
    failed: list[FileRejection]


class FileTextResponse(BaseModel):
    id: int
    name: str
    text_content: Optional[str] = None


class FormatGroup(BaseModel):
    label: str
    extensions: list[str]
    note: Optional[str] = None


class FormatsResponse(BaseModel):
    groups: list[FormatGroup]
    max_mb: int


class BackfillResult(BaseModel):
    name: str
    text_chars: int


class BackfillResponse(BaseModel):
    extracted: list[BackfillResult]
    failed: list[FileRejection]
