"""Folder endpoints. Routing and status codes only -- the rules live in services."""

# Custom libraries
from db_pool import get_db
from logger import configure_logging
from schemas.folder_schema import FolderCreate
from services import library_service

# Default libraries
import sqlite3

# Installed libraries
from fastapi import APIRouter, Depends, HTTPException

logger = configure_logging(__name__)

folder_router = APIRouter(tags=["Folders"])


@folder_router.get("/folders")
async def get_folders(db: sqlite3.Connection = Depends(get_db)):
    return library_service.list_folders(db)


@folder_router.post("/folders", status_code=201)
async def post_folder(req: FolderCreate, db: sqlite3.Connection = Depends(get_db)):
    try:
        return library_service.create_folder(db, req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@folder_router.delete("/folders/{folder_id}", status_code=204)
async def remove_folder(folder_id: int, db: sqlite3.Connection = Depends(get_db)):
    if not library_service.delete_folder(db, folder_id):
        raise HTTPException(404, "no such folder")
