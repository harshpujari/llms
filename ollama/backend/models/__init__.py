"""Database models: table definitions only. Every query lives in `repository`.

Importing this package registers each table with db_pool.Model, which is how
init_db() knows what to create -- the same job models/__init__.py does in
assistcx-platform for SQLAlchemy's Base.
"""

# Database modules
from models.file import File
from models.folder import Folder

__all__ = ["File", "Folder"]
