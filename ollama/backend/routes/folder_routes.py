"""Folder endpoints. Routing and status codes only -- the rules live in services."""

# Custom libraries
from logger import configure_logging
from schemas.folder_schema import FolderCreate
from services import library_service

# Installed libraries
from fastapi import APIRouter, HTTPException

logger = configure_logging(__name__)

folder_router = APIRouter(tags=["Folders"])


@folder_router.get("/folders")
async def get_folders():
    return library_service.list_folders()


@folder_router.post("/folders", status_code=201)
async def post_folder(req: FolderCreate):
    try:
        return library_service.create_folder(req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@folder_router.delete("/folders/{folder_id}", status_code=204)
async def remove_folder(folder_id: int):
    if not library_service.delete_folder(folder_id):
        raise HTTPException(404, "no such folder")
