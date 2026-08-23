"""Prepare the single-user SQLite database used by the author launcher."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import json
import enum

from sqlalchemy import inspect, text

from .db import Base, engine
from . import models  # noqa: F401 - register every model with Base.metadata


def _sqlite_default(column) -> str | None:
    """Render only deterministic declarative defaults for additive columns."""
    default = column.server_default.arg if column.server_default is not None else None
    if default is None and column.default is not None:
        default = column.default.arg
    if callable(default):
        if default is dict:
            return "'{}'"
        if default is list:
            return "'[]'"
        default = None
    if default is None:
        type_name = column.type.__class__.__name__.lower()
        if "json" in type_name:
            return "'{}'"
        if "bool" in type_name or "int" in type_name or "float" in type_name:
            return "0"
        if "string" in type_name or "text" in type_name:
            return "''"
        return None
    if isinstance(default, enum.Enum):
        default = default.value
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, (dict, list)):
        default = json.dumps(default, ensure_ascii=False)
    if isinstance(default, (int, float)):
        return str(default)
    value = str(default)
    if value.startswith("'") and value.endswith("'"):
        return value
    return "'" + value.replace("'", "''") + "'"


def prepare_sqlite_database() -> str | None:
    """Prepare SQLite without replacing an existing author database.

    SQLite installations created by earlier local launchers may have no
    ``alembic_version`` row. They are brought to the current declarative
    schema additively, then stamped so subsequent Alembic upgrades are real
    upgrades. Databases that already have migration metadata are upgraded in
    place after a timestamped copy is retained.
    """
    if engine.dialect.name != "sqlite":
        return None

    database_file = Path(engine.url.database or "narrative.db").resolve()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1")) if "alembic_version" in tables else None
    from alembic import command
    from alembic.config import Config
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", str(engine.url))

    if version:
        backup = database_file.with_name(f"{database_file.stem}.migration-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{database_file.suffix}")
        if database_file.exists():
            shutil.copy2(database_file, backup)
        command.upgrade(config, "head")
        return None

    # Legacy/no-version databases are never dropped or replaced. Back them up
    # first, then apply an additive compatibility upgrade and verify the
    # resulting shape before stamping. Do not use ``create_all + stamp``:
    # that would make an incomplete or unknown schema appear fully migrated.
    backup = database_file.with_name(f"{database_file.stem}.legacy-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{database_file.suffix}")
    if database_file.exists(): shutil.copy2(database_file, backup)
    if not tables:
        # A genuinely empty database can be created through the migration
        # chain, which also creates alembic_version in the correct state.
        command.upgrade(config, "head")
        return None

    with engine.begin() as connection:
        # Create only missing tables through the migration-compatible SQLAlchemy
        # DDL path, then add missing columns one by one. Existing rows remain
        # untouched and the final shape is checked before stamping.
        for table in Base.metadata.sorted_tables:
            if table.name not in inspect(connection).get_table_names():
                table.create(connection, checkfirst=True)
        for table_name, table in Base.metadata.tables.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table_name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                type_sql = column.type.compile(dialect=engine.dialect)
                default = _sqlite_default(column)
                if column.nullable:
                    nullable = ""
                elif default is not None:
                    nullable = f" NOT NULL DEFAULT {default}"
                else:
                    raise RuntimeError(f"LOCAL_SCHEMA_INCOMPLETE: cannot add required column {table_name}.{column.name} without a default")
                connection.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {type_sql}{nullable}'))
            for index in table.indexes:
                index.create(connection, checkfirst=True)
        # The migration chain cannot run against a legacy database without an
        # Alembic revision row. Reproduce the non-destructive legacy mapping
        # here before stamping the verified additive schema.
        if "book_contracts" in inspect(connection).get_table_names() and "projects" in inspect(connection).get_table_names():
            project_columns = {column["name"] for column in inspect(connection).get_columns("projects")}
            if {"id", "name"}.issubset(project_columns):
                rows = connection.execute(text("SELECT id, name, story_seed, autonomy_settings FROM projects")) if {"story_seed", "autonomy_settings"}.issubset(project_columns) else connection.execute(text("SELECT id, name FROM projects"))
                for row in rows.mappings():
                    exists = connection.scalar(text("SELECT 1 FROM book_contracts WHERE project_id = :project_id LIMIT 1"), {"project_id": row["id"]})
                    if exists:
                        continue
                    settings = row.get("autonomy_settings") or {}
                    if isinstance(settings, str):
                        try: settings = json.loads(settings)
                        except Exception: settings = {}
                    settings = settings if isinstance(settings, dict) else {}
                    policy = {"mode": "ESTIMATE_ONLY", "estimated_chapters": settings.get("target_chapters") or settings.get("estimated_chapters"), "estimated_volumes": None, "completion_strategy": "AUTHOR_CONFIRMATION", "operational_run_chapter_budget": settings.get("max_chapters") or settings.get("operational_run_chapter_budget"), "operational_token_budget": None}
                    connection.execute(text("INSERT INTO book_contracts (id, project_id, title, premise, protagonist_contract, global_required_events, global_forbidden_events, style_contract, author_locked_constraints, length_policy, version, fingerprint, status) VALUES (:id, :project_id, :title, :premise, :protagonist_contract, :global_required_events, :global_forbidden_events, :style_contract, :author_locked_constraints, :length_policy, 1, :fingerprint, :status)"), {"id": models.new_id(), "project_id": row["id"], "title": row["name"], "premise": row.get("story_seed"), "protagonist_contract": "{}", "global_required_events": "[]", "global_forbidden_events": "[]", "style_contract": "{}", "author_locked_constraints": "[]", "length_policy": json.dumps(policy, ensure_ascii=False), "fingerprint": "legacy-import", "status": "ACTIVE"})
    inspector = inspect(engine)
    missing = {table.name: [column.name for column in table.columns if column.name not in {item["name"] for item in inspector.get_columns(table.name)}] for table in Base.metadata.sorted_tables if table.name in inspector.get_table_names()}
    missing = {table: columns for table, columns in missing.items() if columns}
    if missing: raise RuntimeError(f"LOCAL_SCHEMA_INCOMPLETE: {missing}")
    command.stamp(config, "head")
    return None


if __name__ == "__main__":
    prepare_sqlite_database()
