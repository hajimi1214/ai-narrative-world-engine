from __future__ import annotations

from fastapi.testclient import TestClient
import json
import re
import pytest
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
from app.models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep, AutoDirectorStepStatus, BookContract, Chapter, ChapterPlanningWindow, ChapterQualityAssessment, ChapterWriterDraft, ChapterWorldBranch, Character, CharacterKnowledge, ChapterWindowStatus, EntityType, ForeshadowingLedger, ForeshadowingStatus, KnowledgeStatus, Project, StoryPlan, StoryPlanChapter, StoryPlanVolume, VolumeContinuitySnapshot, VolumeContract, VolumeContractStatus, WorldEntity


def _setup(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="作者卷级测试", story_seed="主角寻找失踪的记忆")
        db.add(project); db.commit(); project_id = project.id
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    def plan_window(db, project, framing, premise, style_guide, anti_ai_rules, **kwargs):
        return ({"chapters": [{"number": number, "title": f"档案调查{number}", "summary": "修复师核对失踪档案中的线索", "objective": "核对档案上的旧印记", "conflict": "档案管理员拒绝提供原件", "scene_beats": ["检查档案并比对旧印记", "向管理员追问原件去向"], "must_events": [], "forbidden_events": []} for number in range(1, framing["target_chapters"] + 1)]}, SimpleNamespace(provider="fake", model="fake", request_id="window-plan", latency_ms=0, usage={"total_tokens": 4}))
    monkeypatch.setattr(auto_module, "generate_plan", plan_window)
    return TestClient(app), Session, project_id


def _advance_until_paused(worker, session_factory, run_id, *, max_polls=20, expected_status="PAUSED"):
    for _ in range(max_polls):
        assert worker.run_once() is True
        with session_factory() as db:
            run = db.get(AutoDirectorRun, run_id)
            assert run.status.value in {"RUNNING", "CREATED", expected_status}, (run.pause_reason, (run.context or {}).get("worker_error"), (run.context or {}).get("current_chapter_number"), (run.context or {}).get("worker_stage"))
            if run.status.value == expected_status:
                return
    pytest.fail(f"Author-guided run did not pause within {max_polls} worker polls")


def _trust_seeded_chapters_for_volume_lifecycle(monkeypatch):
    from app.author_guided_volume import AuthorGuidedVolumeService

    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_chapter_evidence",
        lambda self, db, chapter: {"assessment_id": "fixture", "draft_id": "fixture-draft", "content_fingerprint": "fixture-content", "planning_task": {}, "evidence": []},
    )
    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_chapter_scenes_published",
        staticmethod(lambda db, chapter: True),
    )
    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_formal_world_matches_target",
        lambda self, db, volume: True,
    )
    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_published_snapshot_projection",
        lambda self, db, volume, chapters, evidence: {
            "confirmed_facts": [],
            "relationship_changes": [],
            "timeline_end": {"chapter_start": chapters[0].number if chapters else None, "chapter_end": chapters[-1].number if chapters else None, "scene_count": 1, "formal_world_fingerprint": "fixture-world", "adopted_chapters": [{"chapter_id": chapter.id} for chapter in chapters]},
            "location_states": {},
            "item_states": {},
            "active_threads": [],
            "formal_world_fingerprint": "fixture-world",
            "scene_ids": ["fixture-scene"],
            "chapter_rows": [{"chapter_id": chapter.id} for chapter in chapters],
        },
    )
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


def test_book_auto_cross_volume_setting_is_explicit_and_exposed(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "book-auto-setting"})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    assert created.json()["book_policy"]["auto_cross_volume"] is False
    enabled = client.patch(f"/projects/{project_id}/book-contract/automation", json={"auto_cross_volume": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["auto_cross_volume"] is True
    assert enabled.json()["contract"]["length_policy"]["auto_cross_volume"] is True
    assert client.get(f"/projects/{project_id}/author-guided-volume/runs/{run_id}").json()["book_policy"]["auto_cross_volume"] is True
    disabled = client.patch(f"/projects/{project_id}/book-contract/automation", json={"auto_cross_volume": False})
    assert disabled.status_code == 200
    with Session() as db:
        contract = db.scalar(select(BookContract).where(BookContract.project_id == project_id))
        assert contract.length_policy["auto_cross_volume"] is False
        assert contract.length_policy["continuous_volume_progression"] is False


def test_idempotent_author_guided_run_does_not_duplicate_contract(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    first = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"estimated_chapters": None, "idempotency_key": "same"}).json()
    second = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"estimated_chapters": 2000, "idempotency_key": "same"}).json()
    assert first["id"] == second["id"]
    with Session() as db:
        assert len(db.scalars(select(BookContract).where(BookContract.project_id == project_id)).all()) == 1


def test_book_auto_cross_volume_setting_is_work_scoped_and_toggleable(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "book-setting"})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    assert created.json()["book_policy"]["auto_cross_volume"] is False
    enabled = client.patch(f"/projects/{project_id}/book-contract/automation", json={"auto_cross_volume": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["auto_cross_volume"] is True
    with Session() as db:
        contract = db.scalar(select(BookContract).where(BookContract.project_id == project_id))
        assert contract.length_policy["auto_cross_volume"] is True
        assert contract.length_policy["continuous_volume_progression"] is True
    disabled = client.patch(f"/projects/{project_id}/book-contract/automation", json={"auto_cross_volume": False})
    assert disabled.status_code == 200, disabled.text
    state = client.get(f"/projects/{project_id}/author-guided-volume/runs/{run_id}")
    assert state.status_code == 200
    assert state.json()["book_policy"]["auto_cross_volume"] is False


def test_author_guided_runs_can_continue_across_projects(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    with Session() as db:
        other_project = Project(name="第二部小说")
        db.add(other_project)
        db.commit()
        other_project_id = other_project.id
    first_runs_url = f"/projects/{project_id}/author-guided-volume/runs"
    other_runs_url = f"/projects/{other_project_id}/author-guided-volume/runs"
    payload = {"idempotency_key": "author-run"}
    first = client.post(first_runs_url, json=payload)
    assert first.status_code == 201, first.text
    duplicate = client.post(first_runs_url, json=payload)
    assert duplicate.status_code == 201 and duplicate.json()["id"] == first.json()["id"]
    second = client.post(other_runs_url, json=payload)
    assert second.status_code == 201, second.text
    first_id, second_id = first.json()["id"], second.json()["id"]
    assert first_id != second_id
    assert first.json()["context"]["volume_id"] != second.json()["context"]["volume_id"]

    first_url = f"{first_runs_url}/{first_id}"
    other_url = f"{other_runs_url}/{second_id}"
    paused = client.post(f"{first_url}/pause", json={"reason": "作者暂停"})
    assert paused.status_code == 200 and paused.json()["status"] == "PAUSED"
    assert client.get(other_url).json()["status"] == "RUNNING"
    assert client.post(f"{other_runs_url}/{first_id}/continue").status_code == 404
    assert client.get(first_url).json()["status"] == "PAUSED"

    resumed = client.post(f"{first_url}/continue")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["context"]["volume_id"] == first.json()["context"]["volume_id"]
    assert client.get(other_url).json()["context"] == second.json()["context"]


def test_existing_auto_director_endpoint_accepts_author_guided_mode(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    response = client.post(f"/projects/{project_id}/auto-director/runs", json={"run_mode": "AUTHOR_GUIDED_VOLUME", "premise": "作者主线", "estimated_chapters": 2000, "idempotency_key": "compat-author"})
    assert response.status_code == 201, response.text
    assert response.json()["run_mode"] == "AUTHOR_GUIDED_VOLUME"


def test_sealed_volume_requires_author_confirmation_and_snapshot(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
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


def test_author_run_takeover_and_continue_are_durable(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "controls"}).json()
    run_id = created["id"]
    takeover = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/takeover")
    assert takeover.status_code == 200 and takeover.json()["pause_reason"] == "AUTHOR_TAKEOVER"
    continued = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/continue")
    assert continued.status_code == 200
    assert continued.json()["id"] == run_id and continued.json()["status"] == "RUNNING"
    with Session() as db:
        assert db.get(AutoDirectorRun, run_id).context["execute_window"] is True


@pytest.mark.parametrize("attempt,maximum,expected", [(1, 2, "RUNNING"), (3, 2, "BLOCKED"), (1, 0, "BLOCKED")])
def test_author_retry_uses_current_checkpoint_budget(monkeypatch, attempt, maximum, expected):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "author-retry-budget"}).json()
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        old = AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.CHAPTER_EXECUTION, status=AutoDirectorStepStatus.FAILED, attempt=99, input_fingerprint="old-chapter")
        current = AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.QUALITY_REVIEW, status=AutoDirectorStepStatus.FAILED, attempt=attempt, input_fingerprint="current-critic")
        db.add_all([old, current]); db.flush()
        run.status = "FAILED"; run.current_stage = AutoDirectorStage.FAILED
        run.context = {**run.context, "failed_step_id": current.id}
        run.settings = {**run.settings, "max_retries": maximum}
        db.commit()
    url = f"/projects/{project_id}/author-guided-volume/runs/{created['id']}"
    body = client.post(f"{url}/retry").json()
    assert body["status"] == expected
    duplicate = client.post(f"{url}/retry").json()
    assert duplicate["status"] == expected
    if expected == "BLOCKED":
        assert body["pause_reason"] == "MAX_RETRIES_REACHED"
        assert client.post(f"{url}/continue").json()["status"] == "BLOCKED"


def test_next_volume_window_inherits_only_sealed_snapshot_reference(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
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
    next_volume_id = response.json()["volume"]["id"]
    with Session() as db:
        window = db.get(ChapterPlanningWindow, next_window_id)
        assert window.source_volume_snapshot_id == response.json()["source_snapshot_id"]
        assert window.start_chapter_number == 2
    started = client.post(f"/projects/{project_id}/volumes/{next_volume_id}/start")
    assert started.status_code == 200, started.text
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        next_run = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.context["volume_id"].as_string() == next_volume_id))
        assert next_run and next_run.status.value == "RUNNING"
        assert next_run.context["current_volume_number"] == 2
        assert next_run.context["window_id"] == next_window_id


def _prepare_auto_cross_boundary(Session, project_id, *, processed=1, chapter_budget=5, token_budget=0, tokens_used=0, include_next_plan=True):
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        project = db.get(Project, project_id)
        service = AuthorGuidedVolumeService()
        contract = service.ensure_contract(db, project, {
            "title": "自动跨卷测试", "auto_cross_volume": True,
            "continuous_chapter_progression": True,
            "operational_run_chapter_budget": chapter_budget,
            "operational_token_budget": token_budget or None,
        })
        volume = service.ensure_volume(db, project, contract, {
            "volume_goal": "完成当前调查", "core_conflict": "线索带来代价",
            "target_closing_state": {"case": "closed"},
        })
        volume.target_closing_state = {"case": "closed"}
        volume.actual_chapter_start = 1
        volume.actual_chapter_end = 1
        plan = StoryPlan(project_id=project_id, version=1, source_fingerprint="auto-cross-test")
        db.add(plan); db.flush()
        task = StoryPlanChapter(project_id=project_id, plan_id=plan.id, number=1, volume_number=1, arc_number=1, title="收束", summary="完成调查", objective="结案", conflict="代价", end_state={"case": "closed"})
        db.add(task)
        db.add(Chapter(project_id=project_id, number=1, content="本卷完成", status="QUALITY_APPROVED", active=True))
        if include_next_plan:
            db.add(StoryPlanVolume(plan_id=plan.id, number=2, title="第二卷", summary="承接后果", start_chapter=2, end_chapter=3, major_conflict="新的阻力", end_state={"case": "resolved"}))
        window = ChapterPlanningWindow(project_id=project_id, volume_id=volume.id, start_chapter_number=1, end_chapter_number=1, status=ChapterWindowStatus.ACTIVE, plan_fingerprint="auto-cross-window")
        db.add(window); db.flush()
        request = {"auto_cross_volume": True, "continuous_chapter_progression": True, "operational_run_chapter_budget": chapter_budget}
        run = AutoDirectorRun(
            project_id=project_id, idempotency_key=f"auto-cross-{processed}-{chapter_budget}-{token_budget}-{tokens_used}-{include_next_plan}",
            run_mode="AUTHOR_GUIDED_VOLUME", status=AutoDirectorRunStatus.PAUSED,
            current_stage=AutoDirectorStage.VOLUME_PROGRESS_ASSESSMENT,
            settings={"operational_run_chapter_budget": chapter_budget, "max_chapters": chapter_budget, "operational_token_budget": token_budget, "max_tokens": token_budget},
            token_usage={"total_tokens": tokens_used}, total_tokens=tokens_used,
            context={"request": request, "book_contract_id": contract.id, "volume_id": volume.id, "current_volume_number": 1, "window_id": window.id, "current_chapter_number": 2, "execute_window": True, "window_complete": True, "processed_this_run": processed, "adopted_this_run": processed, "generated_this_run": processed, "last_adopted_chapter_number": 1},
        )
        db.add(run)
        db.commit()
    return run.id


def test_auto_cross_volume_seals_snapshot_and_reuses_run_budget(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, processed=1, chapter_budget=5)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        run = db.get(AutoDirectorRun, run_id)
        old_volume_id = run.context["volume_id"]
        old_context = dict(run.context)
        result = AuthorGuidedVolumeService().advance_run(db, run)
        db.commit()
        db.refresh(run)
        old_volume = db.get(VolumeContract, old_volume_id)
        next_volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2))
        snapshot = db.get(VolumeContinuitySnapshot, old_volume.sealed_snapshot_id)
        assert result.id == run.id
        assert old_volume.status == VolumeContractStatus.SEALED
        assert snapshot is not None
        assert next_volume.status == VolumeContractStatus.ACTIVE
        assert next_volume.opening_state["source_volume_snapshot_id"] == snapshot.id
        assert run.context["volume_id"] == next_volume.id
        assert run.context["processed_this_run"] == old_context["processed_this_run"]
        assert run.status == AutoDirectorRunStatus.RUNNING
        assert run.pause_reason is None


def test_auto_cross_volume_seals_before_chapter_budget_pause(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, processed=5, chapter_budget=5)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        run = db.get(AutoDirectorRun, run_id)
        old_volume_id = run.context["volume_id"]
        AuthorGuidedVolumeService().advance_run(db, run)
        db.commit()
        old_volume = db.get(VolumeContract, old_volume_id)
        next_volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2))
        assert old_volume.status == VolumeContractStatus.SEALED
        assert old_volume.sealed_snapshot_id
        assert next_volume.status == VolumeContractStatus.ACTIVE
        assert run.status == AutoDirectorRunStatus.PAUSED
        assert run.pause_reason == "RUN_CHAPTER_BUDGET_REACHED"
        assert run.context["volume_id"] == next_volume.id


def test_auto_cross_volume_seals_then_pauses_when_token_budget_is_exhausted(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, processed=1, chapter_budget=5, token_budget=10, tokens_used=10)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        run = db.get(AutoDirectorRun, run_id)
        old_volume_id = run.context["volume_id"]
        AuthorGuidedVolumeService().advance_run(db, run)
        db.commit()
        old_volume = db.get(VolumeContract, old_volume_id)
        next_volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2))
        assert old_volume.status == VolumeContractStatus.SEALED
        assert next_volume.status == VolumeContractStatus.ACTIVE
        assert run.status == AutoDirectorRunStatus.PAUSED
        assert run.pause_reason == "TOKEN_BUDGET_EXCEEDED"
        assert "增加预算" in run.next_action


def test_auto_cross_volume_missing_successor_plan_keeps_resumable_draft(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, processed=2, chapter_budget=5, tokens_used=41, include_next_plan=False)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        run = db.get(AutoDirectorRun, run_id)
        old_volume_id = run.context["volume_id"]
        original_context = dict(run.context)
        AuthorGuidedVolumeService().advance_run(db, run)
        db.commit()
        old_volume = db.get(VolumeContract, old_volume_id)
        successor = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2))
        assert old_volume.status == VolumeContractStatus.SEALED
        assert old_volume.sealed_snapshot_id
        assert run.status == AutoDirectorRunStatus.BLOCKED
        assert run.pause_reason == "AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED"
        assert successor.status == VolumeContractStatus.DRAFT
        assert successor.opening_state["source_volume_snapshot_id"] == old_volume.sealed_snapshot_id
        assert run.context["volume_id"] == old_volume_id
        assert run.context["processed_this_run"] == original_context["processed_this_run"] == 2
        assert run.total_tokens == 41
        assert run.context["auto_cross_volume_checkpoint"]["next_volume_id"] == successor.id
        assert run.context["auto_cross_volume_checkpoint"]["source_snapshot_id"] == old_volume.sealed_snapshot_id
        assert run.context["auto_cross_volume_checkpoint"]["missing_fields"] == ["volume_goal", "core_conflict", "target_closing_state"]

    volumes = client.get(f"/projects/{project_id}/volumes").json()
    successor_payload = next(item for item in volumes if item["volume_number"] == 2)
    assert successor_payload["auto_cross_volume_checkpoint"]["missing_fields"] == ["volume_goal", "core_conflict", "target_closing_state"]
    assert successor_payload["source_snapshot"]["available"] is True
    assert successor_payload["source_snapshot"]["id"] == successor_payload["opening_state"]["source_volume_snapshot_id"]
    assert "第 2 卷缺少下一卷目标、核心冲突、结束状态" in client.get(f"/projects/{project_id}/author-guided-volume/runs/{run_id}").json()["next_action"]
    manual_start = client.post(f"/projects/{project_id}/volumes/{successor_payload['id']}/start")
    assert manual_start.status_code == 409
    assert manual_start.json()["detail"]["code"] == "AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED"
    manual_next = client.post(f"/projects/{project_id}/volumes/{old_volume_id}/next")
    assert manual_next.status_code == 409
    assert manual_next.json()["detail"]["code"] == "AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED"


def test_auto_cross_volume_partial_plan_reports_only_missing_fields(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, include_next_plan=False)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        plan = db.scalar(select(StoryPlan).where(StoryPlan.project_id == project_id))
        db.add(StoryPlanVolume(
            plan_id=plan.id,
            number=2,
            title="第二卷",
            summary="",
            start_chapter=2,
            end_chapter=6,
            major_conflict="档案管理局封锁原始记录",
            end_state={},
        ))
        db.commit()
        run = db.get(AutoDirectorRun, run_id)
        AuthorGuidedVolumeService().advance_run(db, run)
        db.commit()
        successor = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2))
        assert successor.volume_goal is None
        assert successor.core_conflict == "档案管理局封锁原始记录"
        assert run.context["auto_cross_volume_checkpoint"]["missing_fields"] == ["volume_goal", "target_closing_state"]

    payload = next(item for item in client.get(f"/projects/{project_id}/volumes").json() if item["volume_number"] == 2)
    assert payload["auto_cross_volume_checkpoint"]["missing_fields"] == ["volume_goal", "target_closing_state"]


def test_auto_cross_volume_plan_recovery_resumes_same_run_and_budget(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, processed=2, chapter_budget=5, tokens_used=41, include_next_plan=False)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        run = db.get(AutoDirectorRun, run_id)
        AuthorGuidedVolumeService().advance_run(db, run)
        db.commit()
        next_volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2))
        source_snapshot_id = next_volume.opening_state["source_volume_snapshot_id"]

    updated = client.patch(f"/projects/{project_id}/volumes/{next_volume.id}/contract", json={
        "volume_goal": "追查旧档案背后的失踪名单",
        "core_conflict": "档案管理局封锁原始记录并追捕调查者",
        "target_closing_state": {"project": {"name": "作者卷级测试"}},
        "author_note": "补齐自动跨卷规划",
    })
    assert updated.status_code == 200, updated.text

    resumed = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/continue")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["id"] == run_id
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["context"]["volume_id"] != next_volume.id
    assert resumed.json()["context"]["processed_this_run"] == 2
    assert resumed.json()["token_usage"]["total_tokens"] == 41

    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        run = db.get(AutoDirectorRun, run_id)
        successor = db.get(VolumeContract, next_volume.id)
        source_volume = db.get(VolumeContract, run.context["auto_cross_volume_checkpoint"]["source_volume_id"])
        assert run.id == run_id
        assert run.context["volume_id"] == successor.id
        assert run.context["processed_this_run"] == 2
        assert run.total_tokens == 41
        assert successor.status == VolumeContractStatus.ACTIVE
        assert successor.opening_state["source_volume_snapshot_id"] == source_snapshot_id
        assert source_volume.sealed_snapshot_id == source_snapshot_id
        assert db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == successor.id)).source_volume_snapshot_id == source_snapshot_id


def test_auto_cross_volume_transition_is_idempotent(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    run_id = _prepare_auto_cross_boundary(Session, project_id, processed=1, chapter_budget=5)
    from app.author_guided_volume import AuthorGuidedVolumeService
    with Session() as db:
        service = AuthorGuidedVolumeService()
        run = db.get(AutoDirectorRun, run_id)
        original_volume = db.get(VolumeContract, run.context["volume_id"])
        original_context = dict(run.context)
        service.advance_run(db, run)
        db.flush()
        first_snapshot_id = original_volume.sealed_snapshot_id
        service._transition_auto_cross_volume(db, run, db.get(Project, project_id), db.get(BookContract, run.context["book_contract_id"]), original_volume, original_context.get("request") or {}, original_context, 5)
        db.commit()
        snapshots = db.scalars(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.volume_id == original_volume.id)).all()
        successors = db.scalars(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 2)).all()
        assert len(snapshots) == 1
        assert snapshots[0].id == first_snapshot_id
        assert len(successors) == 1


def test_unseal_reseal_creates_immutable_snapshot_and_replans_next_volume(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "reseal-snapshot"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()

    from app.author_guided_volume import AuthorGuidedVolumeError, AuthorGuidedVolumeService
    service = AuthorGuidedVolumeService()
    with Session() as db:
        project = db.get(Project, project_id)
        contract = db.scalar(select(BookContract).where(BookContract.project_id == project_id))
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number == 1))
        actor = Character(project_id=project_id, name="主角", current_state={"location_id": "old-location", "stance": "第一版"})
        db.add(actor); db.flush()
        db.add(CharacterKnowledge(character_id=actor.id, proposition="第一版已知事实", status=KnowledgeStatus.KNOWN))
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1
        volume.target_closing_state = {"case": "closed"}
        db.add(Chapter(project_id=project_id, number=1, content="本卷结束", status="QUALITY_APPROVED", active=True))
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"case": "closed"}
        db.flush()

        first = service.seal(db, volume, author_confirmed=True)
        first_snapshot = db.get(VolumeContinuitySnapshot, first.sealed_snapshot_id)
        first_payload = {"character_states": dict(first_snapshot.character_states), "fingerprint": first_snapshot.source_fingerprint, "id": first_snapshot.id}
        next_volume = service.ensure_volume(db, project, contract, {"opening_state": {"source_volume_snapshot_id": first_snapshot.id}}, 2)
        old_window = service.ensure_window(db, project, next_volume, size=2)
        service.ensure_window_tasks(db, project, next_volume, old_window, contract)
        old_plan_id = next((item.id for item in db.scalars(select(StoryPlan).where(StoryPlan.project_id == project_id)).all() if (item.generation_report or {}).get("author_window_id") == old_window.id), None)
        db.commit()

        service.unseal(db, volume, "作者修正封存状态")
        character = db.get(Character, actor.id)
        character.current_state = {"location_id": "new-location", "stance": "修订版"}
        db.add(CharacterKnowledge(character_id=actor.id, proposition="修订版新事实", status=KnowledgeStatus.KNOWN))
        with pytest.raises(AuthorGuidedVolumeError, match="VOLUME_SOURCE_SNAPSHOT_STALE"):
            service.continuity_context(db, next_volume)
        db.refresh(old_window)
        assert old_window.status.value == "STALE" and old_window.error_code == "SOURCE_VOLUME_UNSEALED"
        assert first_snapshot.character_states == first_payload["character_states"]
        db.commit()

        resealed = service.seal(db, volume, author_confirmed=True)
        second_snapshot = db.get(VolumeContinuitySnapshot, resealed.sealed_snapshot_id)
        assert second_snapshot.id != first_snapshot.id
        assert second_snapshot.snapshot_version == 2
        assert second_snapshot.character_states[actor.id]["current_state"]["stance"] == "修订版"
        assert any(item["proposition"] == "修订版新事实" and item["knowledge_id"] for item in second_snapshot.character_states[actor.id]["known_facts_at_seal"])
        assert first_snapshot.id == first_payload["id"] and first_snapshot.source_fingerprint == first_payload["fingerprint"]
        assert first_snapshot.character_states == first_payload["character_states"]

        db.refresh(next_volume)
        assert next_volume.opening_state["source_volume_snapshot_id"] == second_snapshot.id
        db.refresh(old_window)
        assert old_window.status.value == "STALE"
        assert old_window.error_code == "SOURCE_VOLUME_SNAPSHOT_CHANGED"
        # The stale window retains its old source as an audit record. Continue
        # reuses its chapter range, binds the new snapshot, and creates a fresh
        # author plan whose continuity input has the updated character state.
        continuity = service.continuity_context(db, next_volume)
        assert continuity["source_snapshot_id"] == second_snapshot.id
        assert continuity["character_states"][actor.id]["current_state"]["stance"] == "修订版"
        resumed_window = service.ensure_window(db, project, next_volume, size=2)
        assert resumed_window.id == old_window.id
        assert resumed_window.source_volume_snapshot_id == second_snapshot.id
        assert resumed_window.status.value == "ACTIVE" and resumed_window.error_code is None
        new_task_ids = service.ensure_window_tasks(db, project, next_volume, resumed_window, contract)
        new_plans = db.scalars(select(StoryPlan).where(StoryPlan.project_id == project_id)).all()
        new_plan = next(item for item in new_plans if (item.generation_report or {}).get("author_window_id") == resumed_window.id and (item.generation_report or {}).get("author_window_source_snapshot_id") == second_snapshot.id)
        assert new_plan.id != old_plan_id
        assert new_plan.framing["continuity_constraints"]["source_snapshot_id"] == second_snapshot.id
        assert new_plan.framing["continuity_constraints"]["character_states"][actor.id]["current_state"]["stance"] == "修订版"
        assert len(new_task_ids) == 2
        snapshots = db.scalars(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.volume_id == volume.id).order_by(VolumeContinuitySnapshot.snapshot_version)).all()
        assert [(item.snapshot_version, item.id) for item in snapshots] == [(1, first_snapshot.id), (2, second_snapshot.id)]


def test_sealed_snapshot_does_not_expose_future_character_secret(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
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
        assert not any(item["proposition"] == "未来才揭示的秘密" for item in snapshot.character_states[character.id]["known_facts_at_seal"])


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
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
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
    client.post(f"/projects/{other_project_id}/author-guided-volume/runs/{other_response.json()['id']}/pause", json={"reason": "fixture idle"})

    import app.auto_director as volume_module
    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": 3}
        run.status = "COMPLETED"
        return run
    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue")
    with Session() as db:
        db.expire_all()
    worker = AutoDirectorWorker(Session, poll_seconds=0)
    worker.run_once()  # Persist the semantic planning checkpoint.
    worker.run_once()  # Execute the mocked runtime checkpoint.
    with Session() as db:
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id).order_by(ChapterPlanningWindow.start_chapter_number)).all()
        assert len(windows) == 2 and windows[1].start_chapter_number == 6


def test_next_window_is_not_planned_until_two_chapters_remain(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 5, "idempotency_key": "window-horizon"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()

    import app.auto_director as volume_module

    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": 1}
        run.status = "RUNNING"
        return run

    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    assert client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue").status_code == 200
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id)).all()
        assert len(windows) == 1


def test_preplanned_followup_window_is_reused_without_skipping_its_chapters(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 5, "continuous_chapter_progression": False, "idempotency_key": "reuse-next-window"}).json()
    worker = AutoDirectorWorker(Session, poll_seconds=0)
    worker.run_once()  # Build the first bounded window and wait for the author.
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume_id = volume.id
    assert client.post(f"/projects/{project_id}/volumes/{volume_id}/continue").status_code == 200

    state = {"adopted": 3, "status": "RUNNING"}
    import app.auto_director as volume_module

    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": state["adopted"], "adopted_this_run": state["adopted"]}
        run.status = state["status"]
        return run

    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    worker.run_once()  # Semantically plan the current window.
    worker.run_once()  # With two chapters left, pre-plan chapters 6–10.
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        assert "next_window_id" in run.context, {"status": run.status, "pause": run.pause_reason, "stage": run.current_stage, "last": run.context.get("last_adopted_chapter_number"), "window": run.context.get("window_id"), "execute": run.context.get("execute_window"), "next": run.next_action}
        planned_id = run.context["next_window_id"]
        planned = db.get(ChapterPlanningWindow, planned_id)
        assert (planned.start_chapter_number, planned.end_chapter_number) == (6, 10)

    state.update(adopted=5, status="PAUSED")
    worker.run_once()  # Finish chapters 1–5 and stop at this window boundary.
    assert client.post(f"/projects/{project_id}/volumes/{volume_id}/continue").status_code == 200
    state.update(adopted=5, status="RUNNING")
    worker.run_once()

    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume_id).order_by(ChapterPlanningWindow.start_chapter_number)).all()
        task_numbers = [db.get(StoryPlanChapter, task_id).number for task_id in run.context["window_task_ids"]]
        assert run.context["window_id"] == planned_id
        assert run.context["current_chapter_number"] == 6
        assert task_numbers == [6, 7, 8, 9, 10]
        assert [(item.start_chapter_number, item.end_chapter_number) for item in windows] == [(1, 5), (6, 10)]


def test_continuous_mode_automatically_moves_to_the_next_window(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 5, "operational_run_chapter_budget": 10, "continuous_chapter_progression": True, "idempotency_key": "continuous-windows"}).json()
    state = {"adopted": 5, "status": "PAUSED"}
    import app.auto_director as volume_module

    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": state["adopted"], "adopted_this_run": state["adopted"]}
        run.status = state["status"]
        return run

    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    worker = AutoDirectorWorker(Session, poll_seconds=0)
    worker.run_once()  # Semantically plan the first window.
    worker.run_once()  # First window is executed immediately by the new-book workflow.
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        assert run.context.get("window_complete") is True, {"status": run.status, "pause": run.pause_reason, "stage": run.current_stage, "last": run.context.get("last_adopted_chapter_number"), "window": run.context.get("window_id"), "execute": run.context.get("execute_window"), "next": run.next_action}
        assert run.status.value == "RUNNING"
        assert run.pause_reason is None
        assert run.context["window_complete"] is True

    state.update(adopted=5, status="RUNNING")
    worker.run_once()  # The worker opens and begins the next contiguous window.
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        window = db.get(ChapterPlanningWindow, run.context["window_id"])
        assert window.start_chapter_number == 6
        assert run.context["current_chapter_number"] == 6
        assert run.status.value == "RUNNING"

    state.update(adopted=10, status="PAUSED")
    worker.run_once()  # Respect the run budget after the second window.
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        assert run.status.value == "PAUSED", {"status": run.status, "pause": run.pause_reason, "window_complete": run.context.get("window_complete"), "last_processed": run.context.get("last_processed_chapter_number"), "last_adopted": run.context.get("last_adopted_chapter_number"), "processed_this_run": run.context.get("processed_this_run"), "window_id": run.context.get("window_id"), "stage": run.current_stage}
        assert run.pause_reason == "RUN_CHAPTER_BUDGET_REACHED"
        assert run.context["window_complete"] is True


def test_quality_debt_on_last_window_chapter_still_stops_at_run_budget(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 5, "operational_run_chapter_budget": 5, "continuous_chapter_progression": False, "idempotency_key": "quality-debt-budget"}).json()
    worker = AutoDirectorWorker(Session, poll_seconds=0)
    worker.run_once()
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        contract = db.get(BookContract, run.context["book_contract_id"])
        contract_policy = {**contract.length_policy, "continuous_chapter_progression": True}
        contract.length_policy = contract_policy
        run_request = {**run.context["request"], "continuous_chapter_progression": True, "length_policy": contract_policy}
        run.context = {**run.context, "request": run_request}
        db.commit()
    assert client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue").status_code == 200
    import app.auto_director as volume_module

    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": 4, "last_processed_chapter_number": 5, "current_chapter_number": 6, "adopted_this_run": 4, "generated_this_run": 5, "processed_this_run": 5, "quality_debts": ["QUALITY_GATE_NOT_PASS"]}
        run.status = "PAUSED"
        run.pause_reason = "RUN_CHAPTER_BUDGET_REACHED"
        run.next_action = "第五章保留质量债务"
        return run

    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    worker.run_once()  # Complete semantic planning for the continuous window.
    worker.run_once()  # Last chapter is processed but remains quality debt.
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        assert run.status.value == "PAUSED", {"status": run.status, "pause": run.pause_reason, "window_complete": run.context.get("window_complete"), "last_processed": run.context.get("last_processed_chapter_number"), "last_adopted": run.context.get("last_adopted_chapter_number"), "processed_this_run": run.context.get("processed_this_run"), "window_id": run.context.get("window_id"), "stage": run.current_stage}
        assert run.pause_reason == "RUN_CHAPTER_BUDGET_REACHED"
        assert run.context["window_complete"] is True
        assert run.context["last_processed_chapter_number"] == 5


def test_single_chapter_window_does_not_preplan_duplicate_followup(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"window_size": 1, "idempotency_key": "single-window-horizon"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()

    import app.auto_director as volume_module

    def fake_advance(self, db, run):
        run.context = {**(run.context or {}), "last_adopted_chapter_number": 1}
        run.status = "RUNNING"
        return run

    monkeypatch.setattr(volume_module.AutoDirectorOrchestrator, "advance_to_pause", fake_advance)
    assert client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue").status_code == 200
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id)).all()
        assert len(windows) == 1


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
    observed_budgets = []
    def budget_error(self, db, run):
        observed_budgets.append(int((run.settings or {}).get("max_tokens") or 0))
        if observed_budgets[-1] <= 1:
            raise AutoDirectorError("TOKEN_BUDGET_EXCEEDED")
        run.status = "PAUSED"
        run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
        run.pause_reason = "TEST_STOP_AFTER_BUDGET_INCREASE"
        run.next_action = "test checkpoint retained"
        return run
    monkeypatch.setattr(auto_module.AutoDirectorOrchestrator, "advance_to_pause", budget_error)
    client.post(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}/continue")
    assert worker.run_once() is True
    state = client.get(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}").json()
    assert state["status"] == "PAUSED" and state["pause_reason"] == "TOKEN_BUDGET_EXCEEDED"
    assert worker.run_once() is False  # A paused budget checkpoint is no longer claimable.
    denied = client.post(f"/projects/{project_id}/volumes/{state['context']['volume_id']}/continue", json={})
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "TOKEN_BUDGET_INCREASE_REQUIRED"
    raised = client.post(f"/projects/{project_id}/volumes/{state['context']['volume_id']}/continue", json={"operational_token_budget": 10})
    assert raised.status_code == 200, raised.text
    assert worker.run_once() is True
    resumed = client.get(f"/projects/{project_id}/author-guided-volume/runs/{created['id']}").json()
    assert observed_budgets[-1] == 10
    assert resumed["status"] == "PAUSED" and resumed["pause_reason"] == "TEST_STOP_AFTER_BUDGET_INCREASE"


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


def test_completion_proposal_ignores_unverified_chapter_body(monkeypatch):
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
        db.commit(); volume_id = volume.id
    proposal = client.get(f"/projects/{project_id}/volumes/{volume_id}/completion-proposal")
    assert proposal.status_code == 200
    assert proposal.json()["status"] == "NOT_READY"
    assert proposal.json()["evidence_chapter_ids"] == []


def test_volume_progress_uses_formal_world_and_ignores_candidate_branch(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "branch-cannot-seal"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        run = db.get(AutoDirectorRun, created["id"])
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1
        volume.actual_chapter_end = 1
        volume.target_closing_state = {"case": "closed"}
        entity = WorldEntity(project_id=project_id, entity_type=EntityType.ITEM, name="卷宗", profile={"case": "open"})
        db.add(entity)
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"case": "closed"}
        branch = ChapterWorldBranch(
            project_id=project_id, run_id=run.id, chapter_number=1,
            base_world_fingerprint="formal-before", base_history_fingerprint="history-before",
            current_fingerprint="candidate-after",
        )
        db.add(branch); db.flush()
        candidate = Chapter(
            project_id=project_id, number=1, content="终局证据已经找到",
            status="QUALITY_APPROVED", active=True,
            world_branch_id=branch.id, pending_world_branch_id=branch.id,
        )
        db.add(candidate); db.flush()
        branch.chapter_id = candidate.id

        service = __import__("app.author_guided_volume", fromlist=["AuthorGuidedVolumeService"]).AuthorGuidedVolumeService()
        report = service.progress(db, volume)
        assert report["target_closing_state_reached"] is False
        assert report["adopted_chapter_ids"] == []
        assert report["incomplete_chapters"] == [1]
        assert report["should_prepare_seal"] is False

        entity.profile = {"case": "closed"}
        assert service._formal_world_matches_target(db, volume) is True
        report = service.progress(db, volume)
        assert report["target_closing_state_reached"] is False
        assert report["adopted_chapter_ids"] == []
        assert report["should_prepare_seal"] is False


def test_due_foreshadowing_blocks_seal_until_adopted_review_evidence(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
    client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"idempotency_key": "foreshadow-evidence-seal"})
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    from app.author_guided_volume import AuthorGuidedVolumeService

    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1
        volume.actual_chapter_end = 1
        volume.target_closing_state = {"case": "closed"}
        chapter = Chapter(project_id=project_id, number=1, content="她交出信件", status="QUALITY_APPROVED", active=True)
        db.add(chapter); db.flush()
        ledger = ForeshadowingLedger(
            project_id=project_id,
            foreshadow_ref="letter-mark",
            title="回收信件缺口",
            status=ForeshadowingStatus.PAID_OFF,
            source_chapter_id=chapter.id,
            target_payoff_volume=1,
        )
        forbidden = ForeshadowingLedger(
            project_id=project_id,
            foreshadow_ref="hidden-name",
            title="尚未允许揭示的姓名",
            status=ForeshadowingStatus.FORBIDDEN_TO_REVEAL,
        )
        db.add_all([ledger, forbidden])
        volume.foreshadowing_payoff_refs = [{"foreshadow_ref": ledger.foreshadow_ref, "label": "回收信件缺口"}]
        evidence = {"assessment_id": "assessment", "draft_id": "draft", "content_fingerprint": "prose-fp", "planning_task": {"foreshadow_payoff": [{"foreshadow_ref": ledger.foreshadow_ref, "label": "回收信件缺口"}]}, "evidence": []}
        service = AuthorGuidedVolumeService()
        monkeypatch.setattr(service, "_chapter_evidence", lambda _db, _chapter: evidence)

        report = service.progress(db, volume)
        assert report["should_prepare_seal"] is False
        assert report["pending_foreshadowing_payoffs"][0]["evidence_status"] == "MISSING_OR_UNVERIFIED"
        assert ledger.foreshadow_ref in {item["foreshadow_ref"] for item in report["pending_foreshadowings"]}

        evidence["evidence"].append({
            "requirement_ref": "foreshadow_payoff:letter-mark",
            "requirement_type": "foreshadow_payoff",
            "event_ref": "letter-mark",
            "label": "回收信件缺口",
            "start_offset": 2,
            "end_offset": 6,
            "excerpt": "交出信件",
            "content_fingerprint": "prose-fp",
            "verified": True,
        })
        report = service.progress(db, volume)
        assert report["should_prepare_seal"] is True
        assert report["paid_off_foreshadowings"][0]["evidence"]["excerpt"] == "交出信件"
        assert forbidden.foreshadow_ref in {item["foreshadow_ref"] for item in report["pending_foreshadowings"]}


def test_completion_proposal_waits_for_global_contract_events(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    created = client.post(f"/projects/{project_id}/author-guided-volume/runs", json={"global_required_events": ["终局证据"], "idempotency_key": "global-ending"}).json()
    AutoDirectorWorker(Session, poll_seconds=0).run_once()
    with Session() as db:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project_id))
        volume.actual_chapter_start = 1; volume.actual_chapter_end = 1; volume.target_closing_state = {"done": True}
        db.add(Chapter(project_id=project_id, number=1, content="终局证据", status="QUALITY_APPROVED", active=True))
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project_id, StoryPlanChapter.number == 1))
        task.end_state = {"done": True}
        db.commit(); volume_id = volume.id
    proposal = client.get(f"/projects/{project_id}/volumes/{volume_id}/completion-proposal")
    assert proposal.status_code == 200
    assert proposal.json()["status"] == "NOT_READY"


def test_completion_confirmation_only_auto_completes_when_enabled(monkeypatch):
    client, Session, project_id = _setup(monkeypatch)
    _trust_seeded_chapters_for_volume_lifecycle(monkeypatch)
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


@pytest.mark.parametrize("critic_mode", ["disabled", "invalid_then_valid"])
def test_author_guided_fake_provider_executes_writer_quality_and_adoption(monkeypatch, critic_mode):
    client, Session, project_id = _setup(monkeypatch)
    with Session() as db:
        project = db.get(Project, project_id)
        project.autonomy_settings = {"quality_gate": {"require_critic": critic_mode != "disabled"}}
        db.commit()

    plan_payload = {
        "premise": "修复师追查会回应的档案",
        "macro_plan": {"promise": "找到档案来源"},
        "volumes": [], "arcs": [],
        "chapters": [{
            "number": 1, "volume_number": 1, "arc_number": 1,
            "title": "回声", "summary": "档案首次回应",
            "objective": "接受调查", "conflict": "回应带来代价",
            "scene_beats": ["检查档案"], "must_events": [], "forbidden_events": [],
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
            self.invalid_critic_responses = 2 if critic_mode == "invalid_then_valid" else 0

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
                actor_view = json.loads(messages[1]["content"])["actor_view"]
                action["scene_beat_refs"] = (actor_view.get("planning_task") or {}).get("scene_beats", [])
                payload = {"decision": payload, "action": action}
            elif "quality_context" in prompt:
                if self.invalid_critic_responses:
                    self.invalid_critic_responses -= 1
                    return ModelResult(content="not-json", latency_ms=0, request_id=f"author-fake-{self.calls}", provider="fake", model=model, usage={"total_tokens": 3})
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
    if critic_mode == "invalid_then_valid":
        _advance_until_paused(worker, Session, run_id, expected_status="FAILED")
        with Session() as db:
            failed_run = db.get(AutoDirectorRun, run_id)
            checkpoint = db.get(AutoDirectorStep, failed_run.context["failed_step_id"])
            assert failed_run.pause_reason == "CRITIC_OUTPUT_INVALID"
            assert checkpoint.stage == AutoDirectorStage.QUALITY_REVIEW and checkpoint.attempt == 1
            pending_draft = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.project_id == project_id))
            assert pending_draft.status.value == "VALIDATED"
            pending_draft_id = pending_draft.id
            assert not db.scalar(select(Chapter.id).where(Chapter.project_id == project_id, Chapter.status == "QUALITY_APPROVED"))
            from app.models import ChapterWorldBranch, Scene, SceneCommit, SceneStateCheckpoint, TimelineEvent
            branch = db.get(ChapterWorldBranch, failed_run.context["chapter_world_branch_id"])
            assert branch and branch.status.value == "READY"
            assert branch.scene_ids and all(db.get(Scene, scene_id).history_status == "STAGED" for scene_id in branch.scene_ids)
            assert db.scalar(select(SceneCommit.id).where(SceneCommit.project_id == project_id)) is None
            assert db.scalar(select(SceneStateCheckpoint.id).where(SceneStateCheckpoint.project_id == project_id)) is None
            assert db.scalar(select(TimelineEvent.id).where(TimelineEvent.project_id == project_id)) is None
        retried = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/retry")
        assert retried.json()["status"] == "RUNNING"
    _advance_until_paused(worker, Session, run_id)

    with Session() as db:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.number == 1, Chapter.active.is_(True)))
        assert chapter and chapter.content
        original_content = chapter.content
        assert chapter.current_writer_draft_id
        draft = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True)))
        run = db.get(AutoDirectorRun, run_id)
        assert draft and draft.status.value == "ADOPTED"
        if critic_mode == "invalid_then_valid":
            assert draft.id == pending_draft_id
            checkpoint = db.get(AutoDirectorStep, run.context["failed_step_id"])
            assert checkpoint.status == AutoDirectorStepStatus.COMMITTED and checkpoint.attempt == 2
        assert assessment and assessment.status.value == "PASS"
        assert run.status.value == "PAUSED"
        assert run.current_stage.value == "VOLUME_PROGRESS_ASSESSMENT"
        assert run.token_usage["total_tokens"] > 0
        from app.models import ChapterWorldBranch, Scene, SceneCommit, TimelineEvent
        from sqlalchemy import func
        branch = db.get(ChapterWorldBranch, run.context["chapter_world_branch_id"])
        assert branch and branch.status.value == "ADOPTED"
        assert chapter.source_world_branch_id == branch.id and chapter.pending_world_branch_id is None
        assert chapter.source_scene_ids == branch.scene_ids
        assert all(db.get(Scene, scene_id).history_status == "ACTIVE" for scene_id in branch.scene_ids)
        assert db.scalar(select(func.count(SceneCommit.id)).where(SceneCommit.project_id == project_id)) == len(branch.scene_ids)
        assert db.scalar(select(func.count(TimelineEvent.id)).where(TimelineEvent.project_id == project_id)) > 0
        from app.author_guided_volume import AuthorGuidedVolumeService
        progress = AuthorGuidedVolumeService().progress(db, db.get(VolumeContract, run.context["volume_id"]))
        assert progress["adopted_chapter_ids"] == [chapter.id]
        assert progress["incomplete_chapters"] == []

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
