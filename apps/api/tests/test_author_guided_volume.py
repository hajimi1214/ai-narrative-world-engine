from __future__ import annotations

from fastapi.testclient import TestClient
import json
import re
from types import SimpleNamespace
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
import app.auto_director as auto_module
import app.runtime as runtime_module
from app.auto_director_worker import AutoDirectorWorker
from app.db import Base
from app.main import app
from app.models import AutoDirectorRun, AutoDirectorStage, AutoDirectorStep, AutoDirectorStepStatus, BookContract, Chapter, ChapterPlanningWindow, ChapterQualityAssessment, ChapterWriterDraft, Character, CharacterKnowledge, ForeshadowingStatus, KnowledgeStatus, Project, StoryPlanChapter, VolumeContinuitySnapshot, VolumeContract, VolumeContractStatus


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
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"case": "closed"}
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


def test_sealed_snapshot_does_not_expose_future_character_secret(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "cognition"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        character = Character(project_id=project_id, name="主角", secrets=["导演内部未来答案"], profile={"public": "调查员"})
        db.add(character); db.flush()
        volume.status = VolumeContractStatus.READY_TO_SEAL; volume.actual_chapter_start = 1; volume.actual_chapter_end = 1
        db.add(Chapter(project_id=project_id, number=1, content="收束", status="QUALITY_APPROVED", active=True))
        from app.author_guided_volume import AuthorGuidedVolumeService
        snapshot = AuthorGuidedVolumeService().create_snapshot(db, volume)
        db.add(CharacterKnowledge(character_id=character.id, proposition="未来才揭示的秘密", status=KnowledgeStatus.KNOWN, source="future-scene"))
        db.commit()
        assert "secrets" not in snapshot.character_states[character.id]
        assert "未来才揭示的秘密" not in snapshot.character_states[character.id]["known_facts_at_seal"]


def test_author_can_update_unsealed_volume_contract_but_not_sealed_volume(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "volume-contract"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume_id = volume.id
        version = volume.version
    updated = client.patch(f"/projects/{project_id}/volumes/{volume_id}/contract", json={"volume_goal": "作者指定的新目标", "author_note": "调整本卷目标"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["volume"]["version"] == version + 1
    with Session() as db:
        volume = db.get(VolumeContract, volume_id)
        volume.status = VolumeContractStatus.SEALED
        db.commit()
    rejected = client.patch(f"/projects/{project_id}/volumes/{volume_id}/contract", json={"volume_goal": "不应修改", "author_note": "尝试修改封存卷"})
    assert rejected.status_code == 409


def test_dynamic_length_and_window_rollover_do_not_use_estimate_as_completion_limit(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"estimated_chapters": 600, "window_size": 5, "idempotency_key": "dynamic-length"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        contract = db.scalar(select(BookContract).where(BookContract.project_id == project_id))
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        window = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id))
        assert contract.length_policy["estimated_chapters"] == 600
        assert db.scalar(select(StoryPlanChapter.number).where(StoryPlanChapter.project_id == project_id).order_by(StoryPlanChapter.number.desc())) == 5
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1
        volume.target_closing_state = {"done": True}
        db.add(Chapter(project_id=project_id, number=1, content="已收束", status="QUALITY_APPROVED", active=True))
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"done": True}
        db.commit()
        report = __import__("app.author_guided_volume", fromlist=["AuthorGuidedVolumeService"]).AuthorGuidedVolumeService().progress(db, volume)
        assert report["should_prepare_seal"] is True
        assert report["reason"]
    with Session() as db:
        other_project = Project(name="另一部小说", story_seed="独立故事")
        db.add(other_project); db.commit(); other_project_id = other_project.id
    other_response = client.post(f"/projects/{other_project_id}/author-guided-volume/runs", json={"estimated_chapters": 300, "idempotency_key": "independent"})
    assert other_response.status_code == 201
    with Session() as db:
        other = db.scalar(select(BookContract).where(BookContract.project_id == other_project_id))
        assert other.length_policy["estimated_chapters"] == 300
        assert other.id != contract.id

    import app.auto_director as volume_module
    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": 3}
        run.status = "COMPLETED"
        return run
    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue")
    with Session() as db:
        db.expire_all()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id).order_by(ChapterPlanningWindow.start_chapter_number)).all()
        assert len(windows) == 2 and windows[1].start_chapter_number == 6


def test_open_window_can_continue_after_estimated_chapter_600(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"estimated_chapters": 600, "window_size": 5, "idempotency_key": "beyond-estimate"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        from app.author_guided_volume import AuthorGuidedVolumeService
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        previous = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id))
        db.add(Chapter(project_id=project_id, number=600, content="实际写到预估之外", status="QUALITY_APPROVED", active=True))
        db.commit()
        followup = AuthorGuidedVolumeService().ensure_followup_window(db, db.get(Project, project_id), volume, previous)
        assert followup.start_chapter_number == 601


def test_usage_summary_projects_chapter_window_volume_and_book_scopes(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 5, "idempotency_key": "usage-scopes"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        window = db.get(ChapterPlanningWindow, run.context["window_id"])
        volume_id = window.volume_id
        for stage, payload, tokens in [
            (AutoDirectorStage.CHAPTER_EXECUTION, {"chapter_number": 1, "window_id": window.id, "volume_id": volume_id}, 5),
            (AutoDirectorStage.CHAPTER_DETAIL, {"chapter_number": 2, "window_id": window.id, "volume_id": volume_id}, 7),
            (AutoDirectorStage.VOLUME_PLANNING, {"window_id": window.id, "volume_id": volume_id}, 3),
        ]:
            db.add(AutoDirectorStep(run_id=run.id, stage=stage, status=AutoDirectorStepStatus.COMMITTED, input_fingerprint=f"scope-{tokens}", output_payload=payload, calls=1, total_tokens=tokens, prompt_tokens=tokens, completed_at=None))
        run.context = {**run.context, "current_chapter_number": 1}
        db.commit()
    summary = client.get(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}").json()["usage_summary"]
    assert summary["chapter"]["total_tokens"] == 5
    assert summary["window"]["total_tokens"] == 15
    assert summary["volume"]["total_tokens"] == 15
    assert summary["book"]["total_tokens"] == 15


def test_operational_token_budget_pauses_author_run_at_checkpoint(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"operational_token_budget": 1, "idempotency_key": "token-budget"}).json()
    worker = AutoDirectorWorker(Session, poll_seconds=0)
    assert worker.run_once() is True
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        assert run.settings.get("max_tokens") == 1
    import app.auto_director as auto_module
    from app.auto_director import AutoDirectorError
    def budget_error(self, db, run):
        raise AutoDirectorError("TOKEN_BUDGET_EXCEEDED")
    monkeypatch.setattr(auto_module.AutoDirectorOrchestrator, "advance_to_pause", budget_error)
    client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue")
    assert worker.run_once() is True
    state = client.get(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}").json()
    assert state["status"] == "PAUSED" and state["pause_reason"] == "TOKEN_BUDGET_EXCEEDED"


def test_new_worker_instance_resumes_persisted_author_checkpoint_without_duplicate_window(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 5, "idempotency_key": "restart"}).json()
    first_worker = AutoDirectorWorker(Session, poll_seconds=0)
    assert first_worker.run_once() is True
    with Session() as db:
        before = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.project_id == project_id)).all()
        assert len(before) == 1
        before_ids = [item.id for item in before]
    client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue")
    second_worker = AutoDirectorWorker(Session, poll_seconds=0)
    monkeypatch.setattr("app.auto_director.AutoDirectorOrchestrator.advance_to_pause", lambda self, db, run: run)
    assert second_worker.run_once() is True
    with Session() as db:
        after = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.project_id == project_id)).all()
        assert [item.id for item in after] == before_ids
        assert db.get(AutoDirectorRun, created["id"]).context["window_id"] == before_ids[0]


def test_quality_failed_chapter_blocks_volume_seal(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "quality-seal"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.target_closing_state = {"done": True}; volume.actual_chapter_start = 1; volume.actual_chapter_end = 1
        db.add(Chapter(project_id=project_id, number=1, content="有草稿但质量失败", status="DRAFT", active=True))
        db.commit(); volume_id = volume.id
    response = client.post(f"/projects/{project_id}/volumes/{volume_id}/seal", json={"author_confirmed": True})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "VOLUME_COMPLETION_CONDITIONS_UNMET"


def test_completion_proposal_contains_adopted_evidence_and_honors_policy(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"length_policy": {"allow_completion_proposal": True}, "idempotency_key": "completion-evidence"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1; volume.target_closing_state = {"done": True}
        chapter = Chapter(project_id=project_id, number=1, content="已采用正文", status="QUALITY_APPROVED", active=True)
        db.add(chapter)
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"done": True}
        db.commit(); volume_id = volume.id; chapter_id = chapter.id
    proposal = client.get(f"/projects/{project_id}/volumes/{volume_id}/completion-proposal")
    assert proposal.status_code == 200
    assert proposal.json()["status"] == "PROPOSED"
    assert proposal.json()["evidence_chapter_ids"] == [chapter_id]


def test_completion_proposal_waits_for_global_contract_events(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"global_required_events": ["终局证据"], "idempotency_key": "global-ending"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1; volume.target_closing_state = {"done": True}
        db.add(Chapter(project_id=project_id, number=1, content="已采用正文", status="QUALITY_APPROVED", active=True))
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"done": True}
        db.commit(); volume_id = volume.id
    proposal = client.get(f"/projects/{project_id}/volumes/{volume_id}/completion-proposal")
    assert proposal.status_code == 200
    assert proposal.json()["status"] == "NOT_READY"


def test_completion_confirmation_only_auto_completes_when_enabled(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"length_policy": {"allow_completion_proposal": True, "allow_auto_complete": True}, "idempotency_key": "auto-complete"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1; volume.target_closing_state = {"done": True}
        db.add(Chapter(project_id=project_id, number=1, content="终局证据", status="QUALITY_APPROVED", active=True))
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"done": True}
        db.commit(); volume_id = volume.id
    proposal = client.get(f"/projects/{project_id}/volumes/{volume_id}/completion-proposal").json()
    confirmed = client.post(f"/projects/{project_id}/completion-proposals/{proposal['id']}/confirm", json={"author_confirmed": True})
    assert confirmed.status_code == 200 and confirmed.json()["book_completed"] is True
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        assert db.get(BookContract, run.context["book_contract_id"]).status == "COMPLETED"
        assert run.current_stage.value == "BOOK_COMPLETED"


def test_volume_progress_requires_structured_closing_state(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "closing-state"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1; volume.target_closing_state = {"case": "closed"}
        db.add(Chapter(project_id=project_id, number=1, content="质量通过但未收束", status="QUALITY_APPROVED", active=True))
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"case": "open"}
        db.commit(); volume_id = volume.id
    blocked = client.post(f"/projects/{project_id}/volumes/{volume_id}/seal", json={"author_confirmed": True})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "VOLUME_COMPLETION_CONDITIONS_UNMET"


def test_author_guided_fake_provider_executes_writer_quality_and_adoption(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    with Session() as db:
        project = db.get(Project, project_id)
        project.autonomy_settings = {"quality_gate": {"require_critic": False}}
        db.commit()

    plan_payload = {
        "premise": "修复师追查会回应的档案",
        "macro_plan": {"promise": "找到档案来源"},
        "volumes": [], "arcs": [],
        "chapters": [{
            "number": 1, "volume_number": 1, "arc_number": 1,
            "title": "回声", "summary": "档案首次回应",
            "objective": "接受调查", "conflict": "回应带来代价",
            "scene_beats": [], "must_events": [], "forbidden_events": [],
            "end_state": {"case": "open"},
        }],
    }
    monkeypatch.setattr(
        auto_module, "generate_plan",
        lambda *args, **kwargs: (
            plan_payload,
            SimpleNamespace(provider="fake", model="fake", request_id="plan", latency_ms=0, usage={"total_tokens": 4}),
        ),
    )

    class AuthorChainProvider:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def generate(self, messages, model):
            from app.ai.provider import ModelResult
            self.calls += 1
            prompt = "\n".join(item.get("content", "") for item in messages)
            if "actor_view" in prompt:
                payload = {
                    "decision_type": "OBSERVE", "intent": "推进调查", "chosen_action": "检查档案",
                    "motivation": "确认线索", "target_character_id": None, "target_entity_id": None,
                    "goal_refs": [], "knowledge_used": [], "memory_refs": [], "ability_refs": [],
                    "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None,
                    "accepted_cost": None, "expected_personal_result": "获得线索", "uncertainties": [],
                    "refused_options": [], "boundary_override_reason": None, "decision_summary": "检查档案。",
                }
                action = {"visibility": "PUBLIC", "observable_action": "检查档案", "spoken_content": None,
                          "requires_world_resolution": False, "world_resolution_request": None,
                          "disclosure_knowledge_ids": [], "scene_beat_refs": [], "target_character_id": None}
                beats = re.search(r'"scene_beats"\s*:\s*\[(.*?)\]', prompt)
                if beats:
                    action["scene_beat_refs"] = re.findall(r'"([^\"]+)"', beats.group(1))
                payload = {"decision": payload, "action": action}
            elif "quality_context" in prompt:
                payload = {"decision": "PASS", "scores": {key: 95 for key in [
                    "factual_grounding", "pov_compliance", "reveal_safety", "style_naturalness",
                    "repetition", "pacing", "voice_consistency", "overall",
                ]}, "findings": []}
            elif "chapter_title" in prompt and "source_manifest" in prompt:
                context = json.loads(messages[-1]["content"])["context"]
                payload = {"chapter_title": "回声", "prose": "修复师检查档案，纸页深处传来微弱回声。",
                           "scene_coverage": [item["scene_id"] for item in context["source_manifest"]["scenes"]],
                           "source_refs": [], "pov_character_id": context["rendering_contract"]["pov_character_id"],
                           "task_coverage": []}
            else:
                payload = {"decision": "PASS", "scores": {"overall": 95}, "findings": []}
            return ModelResult(content=json.dumps(payload, ensure_ascii=False), latency_ms=0,
                               request_id=f"author-fake-{self.calls}", provider="fake", model=model,
                               usage={"total_tokens": 3})

    provider = AuthorChainProvider()
    monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(runtime_module, "get_model_provider", lambda *args, **kwargs: provider)
    created = client.post(
        f"/projects/{project_id}/author-guided-volume/runs",
        json={"window_size": 1, "idempotency_key": "author-e2e"},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    worker = AutoDirectorWorker(Session, poll_seconds=0)
    assert worker.run_once() is True
    assert client.get(f"/projects/{project_id}/author-guided-volume/runs/{run_id}").json()["status"] == "PAUSED"
    assert client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/continue").status_code == 200
    assert worker.run_once() is True

    with Session() as db:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.number == 1, Chapter.active.is_(True)))
        assert chapter and chapter.content
        original_content = chapter.content
        assert chapter.current_writer_draft_id
        draft = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True)))
        run = db.get(AutoDirectorRun, run_id)
        assert draft and draft.status.value == "ADOPTED"
        assert assessment and assessment.status.value == "PASS"
        assert run.status.value == "PAUSED"
        assert run.current_stage.value == "VOLUME_PROGRESS_ASSESSMENT"
        assert run.token_usage["total_tokens"] > 0

    retry = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/retry")
    assert retry.status_code == 200 and retry.json()["status"] == "PAUSED"
    assert client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/continue").status_code == 200
    assert worker.run_once() is True
    with Session() as db:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.number == 1, Chapter.active.is_(True)))
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.project_id == project_id).order_by(ChapterPlanningWindow.start_chapter_number)).all()
        assert chapter.content == original_content
        assert len(windows) == 2 and windows[0].status.value == "COMPLETED", [(item.start_chapter_number, item.end_chapter_number, item.status.value) for item in windows]
        assert windows[1].start_chapter_number > windows[0].end_chapter_number
