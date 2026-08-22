"""Prepare the single-user SQLite database used by the author launcher."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, text

from .db import Base, engine
from . import models  # noqa: F401 - register every model with Base.metadata


def prepare_sqlite_database() -> str | None:
    """Create the current SQLite schema and preserve an empty failed database.

    The author launcher does not need PostgreSQL's historical migration path.
    An empty database left behind by a failed migration is moved aside so it
    can be recovered, never deleted or overwritten.
    """
    if engine.dialect.name != "sqlite":
        return None

    database_file = Path(engine.url.database or "narrative.db").resolve()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "projects" in tables:
        with engine.connect() as connection:
            project_count = int(connection.scalar(text("SELECT COUNT(*) FROM projects")) or 0)
            version = connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1")) if "alembic_version" in tables else None
        if project_count == 0 and version:
            engine.dispose()
            backup = database_file.with_name(
                f"{database_file.stem}.migration-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{database_file.suffix}"
            )
            database_file.replace(backup)

    Base.metadata.create_all(engine)
    return None


if __name__ == "__main__":
    prepare_sqlite_database()
