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
        assert connection.scalar(text("select version_num from alembic_version")) == "0042_volume_snapshot_uniqueness"
    assert {column["name"] for column in inspector.get_columns("auto_director_runs")} >= {"total_tokens", "estimated_cost", "cost_status"}
    assert {column["name"] for column in inspector.get_columns("auto_director_steps")} >= {"provider", "model", "total_tokens", "estimated_cost"}
    assert {"book_contracts", "volume_contracts", "chapter_planning_windows", "volume_continuity_snapshots", "foreshadowing_ledger", "author_guidance", "book_completion_proposals"} <= set(inspector.get_table_names())
    assert any(item["name"] == "uq_volume_continuity_snapshot_volume" and item["unique"] for item in inspector.get_indexes("volume_continuity_snapshots"))


def test_legacy_sqlite_schema_is_upgraded_before_stamp(tmp_path: Path, monkeypatch):
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("create table projects (id varchar(36) primary key, name varchar(200) not null)"))
    monkeypatch.setattr(local_db, "engine", engine)

    local_db.prepare_sqlite_database()

    inspector = inspect(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == "0042_volume_snapshot_uniqueness"
    assert "story_seed" in {column["name"] for column in inspector.get_columns("projects")}
    assert "auto_director_runs" in inspector.get_table_names()
    assert "book_contracts" in inspector.get_table_names()
    assert list(tmp_path.glob("legacy.legacy-backup-*.db"))


def test_legacy_autonomy_length_settings_are_mapped_to_book_contract(tmp_path: Path, monkeypatch):
    database = tmp_path / "legacy-settings.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("create table projects (id varchar(36) primary key, name varchar(200) not null, story_seed text, autonomy_settings json)"))
        connection.execute(text("insert into projects (id, name, story_seed, autonomy_settings) values ('legacy-project', '旧书', '旧种子', :settings)"), {"settings": '{"target_chapters":600,"max_chapters":8}'})
    monkeypatch.setattr(local_db, "engine", engine)
    local_db.prepare_sqlite_database()
    with engine.connect() as connection:
        row = connection.execute(text("select length_policy from book_contracts where project_id = 'legacy-project'")).mappings().one()
    import json
    policy = json.loads(row["length_policy"])
    assert policy["estimated_chapters"] == 600
    assert policy["operational_run_chapter_budget"] == 8
