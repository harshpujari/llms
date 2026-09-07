"""Database models. Table definitions and the queries against them, nothing else.

Importing this package registers every table with db_pool.Model, which is how
init_db() knows what to create -- the same job models/__init__.py does in
assistcx-platform for SQLAlchemy's Base.
"""

# Database modules
from models.file import File
from models.folder import Folder

__all__ = ["File", "Folder"]
