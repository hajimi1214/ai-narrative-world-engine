from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
from app.auto_director_worker import AutoDirectorWorker
from app.db import Base
from app.main import app
from app.models import AutoDirectorRun, BookContract, ChapterPlanningWindow, Project, StoryPlanChapter, VolumeContract, VolumeContractStatus


def _setup(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="作者卷级测试", story_seed="主角寻找失踪的记忆")
        db.add(project); db.commit(); project_id = project.id
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    return TestClient(app), Session, project_id


def test_author_guided_run_creates_contract_and_bounded_window(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    response = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"title": "长夜记", "theme": "记忆与选择", "premise": "追查失踪记忆", "estimated_chapters": 600, "estimated_volumes": 12, "window_size": 5, "idempotency_key": "book-1"})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    assert response.json()["run_mode"] == "AUTHOR_GUIDED_VOLUME"
    assert AutoDirectorWorker(Session, poll_seconds=0).run_once() is True
    with Session() as db:
        contract = db.scalar(select(BookContract).where(BookContract.project_id == project_id))
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        window = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.project_id == project_id))
        assert contract and contract.length_policy["estimated_chapters"] == 600
        assert volume and volume.status == VolumeContractStatus.ACTIVE
        assert window and window.end_chapter_number - window.start_chapter_number + 1 == 5
        tasks = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.id.in_(db.get(AutoDirectorRun, run_id).context["window_task_ids"])).order_by(StoryPlanChapter.number)).all()
        assert len(tasks) == 5 and [item.number for item in tasks] == [1, 2, 3, 4, 5]
        assert db.get(AutoDirectorRun, run_id).status.value == "PAUSED"


def test_idempotent_author_guided_run_does_not_duplicate_contract(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    first = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"estimated_chapters": None, "idempotency_key": "same"}).json()
    second = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"estimated_chapters": 2000, "idempotency_key": "same"}).json()
    assert first["id"] == second["id"]
    with Session() as db:
        assert len(db.scalars(select(BookContract).where(BookContract.project_id == project_id)).all()) == 1


def test_sealed_volume_requires_author_confirmation_and_snapshot(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"volume": {"target_closing_state": {"case": "closed"}}, "idempotency_key": "seal"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.target_closing_state = {"case": "closed"}
        volume.completion_conditions = []
        volume.actual_chapter_start = 1
        volume.actual_chapter_end = 1
        from app.models import Chapter
        db.add(Chapter(project_id=project_id, number=1, content="本卷完成", status="QUALITY_APPROVED", active=True))
        db.commit(); volume_id = volume.id
    rejected = client.post(f"/projects/{project_id}/volumes/{volume_id}/seal", json={"author_confirmed": False})
    assert rejected.status_code == 409
    sealed = client.post(f"/projects/{project_id}/volumes/{volume_id}/seal", json={"author_confirmed": True})
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["snapshot_id"]
    assert client.get(f"/projects/{project_id}/volumes/{volume_id}/snapshot").status_code == 200
