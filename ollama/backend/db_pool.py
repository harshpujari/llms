"""SQLite connection and table creation.

Plays the role assistcx-platform's db_pool.py plays for Postgres/SQLAlchemy:
models declare their tables against `Model` here, and init_db() creates every
registered one. SQLite has no server and no pool -- connections are cheap enough
to open per operation -- so this is a much smaller thing than an engine.
"""

# Custom libraries
from logger import configure_logging

# Default libraries
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = configure_logging(__name__)

DB_PATH = Path(os.getenv("LIBRARY_DB", "/app/data/library.db"))


class Model:
    """Base for table definitions -- the role SQLAlchemy's declarative Base plays.

    Subclasses register themselves on definition, so init_db() can create the
    whole schema without importing each model by hand.
    """

    __tablename__: str = ""
    __schema__: str = ""
    # Columns added after a table shipped. CREATE TABLE IF NOT EXISTS won't
    # alter a table that already exists, so these are applied by hand.
    __migrations__: dict[str, str] = {}

    registry: list[type["Model"]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Model.registry.append(cls)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because uploads hand the request's connection to a
    # worker thread (run_in_threadpool) so extraction doesn't block the event
    # loop. SQLite would otherwise refuse to be touched off its creating thread.
    # Safe here: a request uses its connection sequentially, never concurrently.
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    # Off by default in SQLite: without this, ON DELETE CASCADE silently
    # does nothing and deleting a folder orphans all its file rows.
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db():
    """FastAPI dependency, mirroring assistcx-platform's get_schema_db."""
    db = connect()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create every registered table, then apply any pending column additions."""
    # Importing the package registers each model against Model.registry.
    import models  # noqa: F401

    with connect() as db:
        for model in Model.registry:
            db.executescript(model.__schema__)

            if not model.__migrations__:
                continue
            have = {r["name"] for r in db.execute(f"PRAGMA table_info({model.__tablename__})")}
            for column, ddl in model.__migrations__.items():
                if column not in have:
                    logger.info("adding %s.%s", model.__tablename__, column)
                    db.execute(f"ALTER TABLE {model.__tablename__} ADD COLUMN {column} {ddl}")
