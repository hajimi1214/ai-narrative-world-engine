import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backup import ARCHIVE_FORMAT, ProjectBackupService
from app.db import Base
from app.main import app
from app.models import CanonFact, CanonType, Character, CharacterMemory, Project
import app.api as api
from fastapi.testclient import TestClient


def test_author_backup_excludes_credentials_and_restores_as_new_project():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        source = Project(name="我的长篇", story_seed="一封来自未来的信")
        db.add(source); db.flush()
        character = Character(project_id=source.id, name="林舟", goals={"current": "找到信的主人"})
        db.add(character); db.flush()
        db.add_all([
            CharacterMemory(character_id=character.id, content="林舟记得码头的钟声。"),
            CanonFact(project_id=source.id, fact_type=CanonType.CORE_CANON, proposition="信件无法被销毁。"),
        ])
        db.commit()

        archive = ProjectBackupService().export(db, source.id)
        assert archive["format"] == ARCHIVE_FORMAT
        assert "project_provider_credentials" not in archive["tables"]
        assert archive["fingerprint"].startswith("nwe-author-backup-v1:")
        assert json.dumps(archive, ensure_ascii=False)

        restored = ProjectBackupService().restore(db, archive, name="我的长篇（恢复副本）")
        db.commit()
        assert restored.id != source.id and restored.name == "我的长篇（恢复副本）"
        assert db.scalar(select(Character).where(Character.project_id == restored.id)).name == "林舟"
        assert db.scalar(select(CanonFact).where(CanonFact.project_id == restored.id)).proposition == "信件无法被销毁。"
        restored_character = db.scalar(select(Character).where(Character.project_id == restored.id))
        assert db.scalar(select(CharacterMemory).where(CharacterMemory.character_id == restored_character.id)).content == "林舟记得码头的钟声。"


def test_backup_fingerprint_tampering_is_rejected():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        project = Project(name="安全测试")
        db.add(project); db.commit()
        archive = ProjectBackupService().export(db, project.id)
        archive["project_name"] = "被篡改"
        try:
            ProjectBackupService().restore(db, archive)
        except ValueError as exc:
            assert str(exc) == "BACKUP_FINGERPRINT_INVALID"
        else:
            raise AssertionError("tampered backup must be rejected")


def test_backup_api_download_and_import_are_author_facing(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    client = TestClient(app)
    source = client.post("/projects", json={"name": "可迁移的小说"}).json()
    download = client.get(f"/projects/{source['id']}/backup/export")
    assert download.status_code == 200 and download.headers["content-type"].startswith("application/json")
    archive = download.json()
    imported = client.post("/projects/import", json={"archive": archive, "name": "可迁移的小说（副本）"})
    assert imported.status_code == 201 and imported.json()["project"]["name"] == "可迁移的小说（副本）"
    status = client.get(f"/projects/{source['id']}/backup/status")
    assert status.status_code == 200 and status.json()["safe"] is True
