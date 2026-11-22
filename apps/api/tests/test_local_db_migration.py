from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.local_db as local_db
from app.models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStep, AutoDirectorStepStatus


HEAD_REVISION = "0045_chapter_world_branches"


def test_sqlite_migrations_reach_head_with_auto_director_usage(tmp_path: Path):
    database = tmp_path / "migration.db"
    config = Config("apps/api/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    inspector = inspect(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == HEAD_REVISION
    assert {column["name"] for column in inspector.get_columns("auto_director_runs")} >= {"total_tokens", "estimated_cost", "cost_status"}
    assert {column["name"] for column in inspector.get_columns("auto_director_steps")} >= {"provider", "model", "total_tokens", "estimated_cost"}
    assert {"book_contracts", "volume_contracts", "chapter_planning_windows", "volume_continuity_snapshots", "foreshadowing_ledger", "author_guidance", "book_completion_proposals", "chapter_world_branches"} <= set(inspector.get_table_names())
    assert any(item["name"] == "uq_volume_continuity_snapshot_volume_version" and item["unique"] and item["column_names"] == ["volume_id", "snapshot_version"] for item in inspector.get_indexes("volume_continuity_snapshots"))


def test_legacy_sqlite_schema_is_upgraded_before_stamp(tmp_path: Path, monkeypatch):
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("create table projects (id varchar(36) primary key, name varchar(200) not null)"))
    monkeypatch.setattr(local_db, "engine", engine)

    local_db.prepare_sqlite_database()

    inspector = inspect(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == HEAD_REVISION
    assert "story_seed" in {column["name"] for column in inspector.get_columns("projects")}
    assert "auto_director_runs" in inspector.get_table_names()
    assert "book_contracts" in inspector.get_table_names()
    assert list(tmp_path.glob("legacy.legacy-backup-*.db"))


@pytest.mark.parametrize("legacy_layout", ["alembic_index", "orm_constraint", "unversioned_orm", "early_0043"])
def test_sqlite_upgrade_0042_to_0043_preserves_snapshot_rows_and_allows_versions(
    tmp_path: Path, monkeypatch, legacy_layout,
):
    database = tmp_path / "volume-snapshot-upgrade.db"
    config = Config("apps/api/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "0042_volume_snapshot_uniqueness")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        if legacy_layout != "alembic_index":
            definition = connection.scalar(text("""
                SELECT sql FROM sqlite_master WHERE name = 'volume_continuity_snapshots'
            """))
            connection.execute(text("DROP TABLE volume_continuity_snapshots"))
            connection.execute(text(
                definition.rstrip()[:-1]
                + ", CONSTRAINT uq_volume_continuity_snapshot_volume UNIQUE (volume_id))"
            ))
        if legacy_layout == "unversioned_orm":
            connection.execute(text("DROP TABLE alembic_version"))
        if legacy_layout == "early_0043":
            connection.execute(text("UPDATE alembic_version SET version_num = '0043_versioned_volume_snapshots'"))
            connection.execute(text("""
                CREATE UNIQUE INDEX uq_volume_continuity_snapshot_volume_version
                ON volume_continuity_snapshots (volume_id, snapshot_version)
            """))
        project_id, contract_id, volume_id = "project-1", "contract-1", "volume-1"
        connection.execute(text("insert into projects (id, name, autonomy_settings, research_settings, created_at, updated_at) values (:id, :name, '{}', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"id": project_id, "name": "卷迁移"})
        connection.execute(text("insert into book_contracts (id, project_id, title, version, fingerprint, status, created_at, updated_at) values (:id, :project_id, 'book', 1, 'fp', 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"id": contract_id, "project_id": project_id})
        connection.execute(text("insert into volume_contracts (id, project_id, book_contract_id, volume_number, title, status, created_at, updated_at) values (:id, :project_id, :contract_id, 1, 'vol', 'SEALED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"id": volume_id, "project_id": project_id, "contract_id": contract_id})
        connection.execute(text("insert into volume_continuity_snapshots (id, project_id, book_contract_id, volume_id, snapshot_version, source_fingerprint) values ('snapshot-old', :project_id, :contract_id, :volume_id, 1, 'old-fp')"), {"project_id": project_id, "contract_id": contract_id, "volume_id": volume_id})
        connection.execute(text("UPDATE volume_contracts SET sealed_snapshot_id = 'snapshot-old' WHERE id = :id"), {"id": volume_id})
    if legacy_layout in {"unversioned_orm", "early_0043"}:
        monkeypatch.setattr(local_db, "engine", engine)
        local_db.prepare_sqlite_database()
        backup_kind = "legacy" if legacy_layout == "unversioned_orm" else "migration"
        assert list(tmp_path.glob(f"volume-snapshot-upgrade.{backup_kind}-backup-*.db"))
    else:
        command.upgrade(config, "head")
    inspector = inspect(engine)
    indexes = {item["name"]: item for item in inspector.get_indexes("volume_continuity_snapshots")}
    assert "uq_volume_continuity_snapshot_volume" not in indexes
    assert indexes["uq_volume_continuity_snapshot_volume_version"]["column_names"] == ["volume_id", "snapshot_version"]
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert connection.scalar(text("SELECT sealed_snapshot_id FROM volume_contracts WHERE id = 'volume-1'")) == "snapshot-old"
        assert connection.scalar(text("select id from volume_continuity_snapshots where volume_id = 'volume-1' and snapshot_version = 1")) == "snapshot-old"
        connection.execute(text("insert into volume_continuity_snapshots (id, project_id, book_contract_id, volume_id, snapshot_version, source_fingerprint) values ('snapshot-new', 'project-1', 'contract-1', 'volume-1', 2, 'new-fp')"))
        with pytest.raises(IntegrityError):
            connection.execute(text("insert into volume_continuity_snapshots (id, project_id, book_contract_id, volume_id, snapshot_version, source_fingerprint) values ('snapshot-duplicate', 'project-1', 'contract-1', 'volume-1', 2, 'duplicate-fp')"))


@pytest.mark.parametrize("legacy_without_revision", [False, True], ids=["0043", "unstamped"])
def test_existing_auto_director_run_and_step_survive_recovery_migration(
    tmp_path: Path, monkeypatch, legacy_without_revision: bool,
):
    database = tmp_path / "run-recovery.db"
    url = f"sqlite:///{database.as_posix()}"
    config = Config("apps/api/alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0043_versioned_volume_snapshots")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO projects (id, name, autonomy_settings, research_settings, created_at, updated_at)
            VALUES ('project-1', '恢复迁移', '{}', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """))
        connection.execute(text("""
            INSERT INTO auto_director_runs (id, project_id, idempotency_key, status, total_tokens)
            VALUES ('run-1', 'project-1', 'original-request', 'PAUSED', 325)
        """))
        connection.execute(text("""
            INSERT INTO auto_director_steps (id, run_id, stage, input_fingerprint, status)
            VALUES ('step-1', 'run-1', 'IDEA', 'original-input', 'COMMITTED')
        """))
        if legacy_without_revision:
            connection.execute(text("DROP TABLE alembic_version"))

    monkeypatch.setattr(local_db, "engine", engine)
    local_db.prepare_sqlite_database()

    saved_at = datetime(2026, 11, 21, 8, 30)
    with Session(engine) as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        run = db.get(AutoDirectorRun, "run-1")
        step = db.get(AutoDirectorStep, "step-1")
        assert run.status == AutoDirectorRunStatus.PAUSED
        assert run.idempotency_key == "original-request"
        assert run.total_tokens == 325
        assert run.recovery_attempts == 0 and run.recovery_usage_uncertain is False
        assert run.worker_claim_token is None and run.worker_lease_until is None
        assert run.next_retry_at is None and run.checkpoint_saved_at is None
        assert run.recovery_key is None and run.recovery_error_code is None and run.checkpoint_stage is None
        assert step.run_id == run.id and step.status == AutoDirectorStepStatus.COMMITTED
        assert step.input_fingerprint == "original-input"
        run.checkpoint_stage = step.stage.value
        run.checkpoint_saved_at = saved_at
        db.commit()

    with Session(engine) as db:
        run = db.get(AutoDirectorRun, "run-1")
        assert run.checkpoint_stage == "IDEA" and run.checkpoint_saved_at == saved_at
    engine.dispose()


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
