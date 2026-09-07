"""Request and response bodies for folder endpoints."""

# Installed libraries
from pydantic import BaseModel


class FolderCreate(BaseModel):
    name: str


class FolderResponse(BaseModel):
    id: int
    name: str
    slug: str
    file_count: int
    total_bytes: int
