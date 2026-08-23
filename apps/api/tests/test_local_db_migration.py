from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

import app.local_db as local_db


def test_sqlite_migrations_reach_head_with_auto_director_usage(tmp_path: Path):
    database = tmp_path / "migration.db"
    config = Config("apps/api/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    inspector = inspect(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == "0040_auto_director_usage_metrics"
    assert {column["name"] for column in inspector.get_columns("auto_director_runs")} >= {"total_tokens", "estimated_cost", "cost_status"}
    assert {column["name"] for column in inspector.get_columns("auto_director_steps")} >= {"provider", "model", "total_tokens", "estimated_cost"}


def test_legacy_sqlite_schema_is_upgraded_before_stamp(tmp_path: Path, monkeypatch):
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("create table projects (id varchar(36) primary key, name varchar(200) not null)"))
    monkeypatch.setattr(local_db, "engine", engine)

    local_db.prepare_sqlite_database()

    inspector = inspect(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == "0040_auto_director_usage_metrics"
    assert "story_seed" in {column["name"] for column in inspector.get_columns("projects")}
    assert "auto_director_runs" in inspector.get_table_names()
    assert list(tmp_path.glob("legacy.legacy-backup-*.db"))
