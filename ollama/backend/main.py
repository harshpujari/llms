"""Application entry point: configuration, middleware, and router mounting.

Layering, top to bottom:

    routes    -> HTTP surface, no logic
    services  -> the business logic
    models    -> tables and the SQL against them
    schemas   -> what crosses the HTTP boundary
"""

# Custom libraries
from db_pool import init_db
from logger import configure_logging
from routes.chat_routes import chat_router
from routes.file_routes import file_router
from routes.folder_routes import folder_router
from services import storage_service

# Installed libraries
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = configure_logging(__name__)

app = FastAPI(title="Local Llama API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

storage_service.ensure_root()
init_db()

app.include_router(chat_router)
app.include_router(folder_router)
app.include_router(file_router)
