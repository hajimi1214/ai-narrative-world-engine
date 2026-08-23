from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
from app.auto_director_worker import AutoDirectorWorker
from app.db import Base
from app.main import app
from app.models import AutoDirectorRun, BookContract, Chapter, ChapterPlanningWindow, ForeshadowingStatus, Project, StoryPlanChapter, VolumeContinuitySnapshot, VolumeContract, VolumeContractStatus


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


def test_existing_auto_director_endpoint_accepts_author_guided_mode(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    response = client.post(f"/projects/{project_id}/auto-director/runs", json={"run_mode": "AUTHOR_GUIDED_VOLUME", "premise": "作者主线", "estimated_chapters": 2000, "idempotency_key": "compat-author"})
    assert response.status_code == 201, response.text
    assert response.json()["run_mode"] == "AUTHOR_GUIDED_VOLUME"


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


def test_author_guidance_returns_unwritten_impact_and_protects_adopted_chapters(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "impact"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        db.add(Chapter(project_id=project_id, number=1, content="已采用", status="QUALITY_APPROVED", active=True))
        db.commit(); volume_id = volume.id
    response = client.post(f"/projects/{project_id}/volumes/{volume_id}/guidance", json={"author_note": "改变主线目标", "affected_scope": "MAINLINE"})
    assert response.status_code == 200, response.text
    analysis = response.json()["analysis"]
    assert any(item["chapter_number"] == 2 for item in analysis["affected_chapters"])
    assert {item["chapter_number"] for item in analysis["protected_chapters"]} == {1}
    assert response.json()["requires_replan"] is True


def test_author_can_add_character_and_change_plot_direction_without_touching_sealed_volume(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "author-input"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.target_closing_state = {"done": True}; volume.actual_chapter_start = 1; volume.actual_chapter_end = 1
        db.add(Chapter(project_id=project_id, number=1, content="收束", status="QUALITY_APPROVED", active=True))
        db.commit(); volume_id = volume.id
    character = client.post(f"/projects/{project_id}/volumes/{volume_id}/characters", json={"name": "沈砚", "goals": {"current": "追查真相"}})
    assert character.status_code == 201, character.text
    assert character.json()["guidance"]["analysis"]["affected_character_ids"]
    direction = client.post(f"/projects/{project_id}/volumes/{volume_id}/plot-direction", json={"global_plot_direction": "主角必须先保护证人"})
    assert direction.status_code == 200, direction.text
    assert direction.json()["requires_replan"] is True


def test_foreshadowing_context_respects_payoff_window_and_status_transitions(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "foreshadow-context"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume_number = volume.volume_number
        volume_id = volume.id
    seeded = client.post(f"/projects/{project_id}/volumes/{volume_id}/foreshadowings", json={"foreshadow_ref": "secret-door", "title": "密门", "earliest_payoff_volume": 2, "target_payoff_volume": 3}).json()
    with Session() as db:
        from app.author_guided_volume import AuthorGuidedVolumeService
        volume = db.get(VolumeContract, volume_id)
        context = AuthorGuidedVolumeService().continuity_context(db, volume)
        assert seeded["id"] in {item["id"] for item in context["forbidden_to_reveal"]}
    touched = client.post(f"/projects/{project_id}/foreshadowings/{seeded['id']}/status", json={"status": "TOUCHED", "volume_id": volume_id})
    assert touched.status_code == 200
    paid = client.post(f"/projects/{project_id}/foreshadowings/{seeded['id']}/status", json={"status": "PAID_OFF", "volume_id": volume_id})
    assert paid.status_code == 200
    assert paid.json()["status"] == ForeshadowingStatus.PAID_OFF.value


def test_author_run_takeover_and_retry_are_durable(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "controls"}).json()
    run_id = created["id"]
    takeover = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/takeover")
    assert takeover.status_code == 200 and takeover.json()["pause_reason"] == "AUTHOR_TAKEOVER"
    retry = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/retry")
    assert retry.status_code == 200 and retry.json()["status"] == "RUNNING"
    with Session() as db:
        assert db.get(AutoDirectorRun, run_id).context["execute_window"] is True


def test_next_volume_window_inherits_only_sealed_snapshot_reference(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "next-volume"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        current = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        current.status = VolumeContractStatus.READY_TO_SEAL
        current.actual_chapter_start = 1; current.actual_chapter_end = 1
        db.add(Chapter(project_id=project_id, number=1, content="完成", status="QUALITY_APPROVED", active=True))
        from app.author_guided_volume import AuthorGuidedVolumeService
        snapshot = AuthorGuidedVolumeService().create_snapshot(db, current)
        current.status = VolumeContractStatus.SEALED; current.sealed_snapshot_id = snapshot.id
        db.commit(); current_id = current.id
    response = client.post(f"/projects/{project_id}/volumes/{current_id}/next")
    assert response.status_code == 200, response.text
    next_window_id = response.json()["window"]["id"]
    with Session() as db:
        window = db.get(ChapterPlanningWindow, next_window_id)
        assert window.source_volume_snapshot_id == response.json()["source_snapshot_id"]
