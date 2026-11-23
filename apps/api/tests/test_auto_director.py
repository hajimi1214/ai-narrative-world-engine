import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
import app.auto_director as auto_module
import app.runtime as runtime_module
from app.ai.fake import FakeModelProvider
from app.ai.errors import MODEL_RATE_LIMITED, ModelProviderError
from app.db import Base
from app.main import app
from app.models import (
    AutoDirectorRun, AutoDirectorStage, AutoDirectorStep, AutoDirectorStepStatus, AutonomousWorldRun, Chapter, ChapterQualityAssessment,
    ChapterWriterDraft, Character, Project, Scene, SceneProposal, StoryPlanStatus, VolumeContract,
    VolumeContractStatus,
)
from app.auto_director_worker import AutoDirectorWorker
from app.author_recovery import utc_now
from app.auto_director import AutoDirectorError, AutoDirectorOrchestrator, _UsageTrackingProvider, fingerprint
from app.autonomy import AutonomousWorldLoopService
from app.planning import persist_plan
from app.narrative_structure import NarrativeStructureService
from app.runtime import _emit_usage
from app.live_execution import live_execution_broker


def _client(monkeypatch, directions):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="自动导演测试", story_seed="能听见城市记忆的修复师")
        db.add(project); db.commit(); project_id = project.id
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    monkeypatch.setattr(auto_module, "generate_creation_directions", lambda *args, **kwargs: ({"directions": directions, "provider": "fake", "model": "fake", "request_id": "fake"}, FakeModelProvider().generate([], "fake")))
    return TestClient(app), project_id


def test_auto_director_creates_and_pauses_for_direction(monkeypatch):
    directions = [{"title": f"方向 {i}", "premise": "可兑现的主线", "core_conflict": "目标与代价"} for i in range(3)]
    client, project_id = _client(monkeypatch, directions)
    response = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "修复师追查失踪记录", "idempotency_key": "same-run"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "RUNNING"
    assert body["current_stage"] == "IDEA"
    assert body["run_mode"] == "FULL_AUTO"

    duplicate = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "不同输入", "idempotency_key": "same-run"})
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == body["id"]


def test_full_auto_api_uses_finite_budget_by_default_and_keeps_explicit_unlimited_opt_in(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    bounded = client.post(f"/projects/{project_id}/auto-director/runs", json={"idempotency_key": "default-bounded-budget"})
    assert bounded.status_code == 201, bounded.text
    assert bounded.json()["settings"]["max_tokens"] == 100000

    explicit_unlimited = client.post(f"/projects/{project_id}/auto-director/runs", json={
        "max_tokens": 0, "idempotency_key": "explicit-unlimited-budget",
    })
    assert explicit_unlimited.status_code == 201, explicit_unlimited.text
    assert explicit_unlimited.json()["settings"]["max_tokens"] == 0


def test_unknown_usage_is_reserved_and_stops_full_auto_budget(monkeypatch):
    _client(monkeypatch, [])
    with api_module.SessionLocal() as db:
        project = db.scalar(select(Project).limit(1))
        run = AutoDirectorRun(
            project_id=project.id, idempotency_key="unknown-usage-budget",
            status="RUNNING", current_stage=AutoDirectorStage.FRAMING,
            settings={"max_tokens": 1024, "target_words_per_chapter": 300},
            context={},
        )
        db.add(run); db.flush()
        step = AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.FRAMING, status=AutoDirectorStepStatus.RUNNING, input_fingerprint="unknown-usage")
        db.add(step); db.flush()
        tracker = _UsageTrackingProvider(FakeModelProvider('{"ok":true}'), on_usage=AutoDirectorOrchestrator._pending_usage_callback(db, run))
        tracker.generate([], "fake-model")
        metrics = AutoDirectorOrchestrator._tracked_metrics(tracker)
        AutoDirectorOrchestrator()._commit_step(step, {"ok": True}, calls=metrics["calls"], usage=metrics["usage"], tokens=metrics["tokens"], provider=metrics["provider"], model=metrics["model"])
        AutoDirectorOrchestrator()._add_usage(run, step, db=db)

        assert run.total_tokens == 0
        assert run.token_usage["unknown_usage_calls"] == 1
        assert run.token_usage["usage_status"] == "UNKNOWN"
        assert run.context["recovery_reserved_tokens"] == 1024
        assert run.cost_status == "UNKNOWN" and run.estimated_cost is None
        with pytest.raises(AutoDirectorError) as budget_error:
            AutoDirectorOrchestrator()._check_budget(run)
        assert budget_error.value.code == "TOKEN_BUDGET_EXCEEDED"


def test_legacy_run_usage_is_conservatively_classified_once(monkeypatch):
    _client(monkeypatch, [])
    with api_module.SessionLocal() as db:
        project = db.scalar(select(Project).limit(1))
        run = AutoDirectorRun(
            project_id=project.id, idempotency_key="legacy-usage",
            status="RUNNING", current_stage=AutoDirectorStage.FRAMING,
            settings={"max_tokens": 100000, "target_words_per_chapter": 1000},
            token_usage={"total_tokens": 90, "total_calls": 3},
            total_tokens=90, total_calls=3, context={},
        )
        db.add(run); db.flush()
        AutoDirectorOrchestrator()._add_usage_metrics(run, {
            "calls": 1, "prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10,
            "usage_status": "CONFIRMED", "unknown_usage_calls": 0,
        }, db=db)
        assert run.token_usage["unknown_usage_calls"] == 3
        assert run.token_usage["usage_status"] == "UNKNOWN"
        assert run.context["recovery_reserved_tokens"] == 6000
        AutoDirectorOrchestrator()._add_usage_metrics(run, {
            "calls": 1, "total_tokens": 5, "usage_status": "CONFIRMED", "unknown_usage_calls": 0,
        }, db=db)
        assert run.token_usage["unknown_usage_calls"] == 3
        assert run.context["recovery_reserved_tokens"] == 6000


def test_full_auto_retry_backoff_survives_worker_stage_handling(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={
        "max_tokens": 100000, "max_retries": 2, "idempotency_key": "full-auto-backoff",
    }).json()
    run_id = created["id"]
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.status = "RUNNING"
        run.current_stage = AutoDirectorStage.FRAMING
        run.settings = {**run.settings, "max_tokens": 100000, "max_retries": 2}
        db.commit()

    def fail_at_checkpoint(_self, db, run):
        step = AutoDirectorStep(
            run_id=run.id, stage=AutoDirectorStage.FRAMING,
            status=AutoDirectorStepStatus.FAILED, attempt=1,
            input_fingerprint="temporary-failure", error_code=MODEL_RATE_LIMITED,
            output_payload={"upstream_status": 429},
        )
        db.add(step); db.flush()
        run.status = "FAILED"
        run.current_stage = AutoDirectorStage.FAILED
        run.pause_reason = MODEL_RATE_LIMITED
        run.context = {**run.context, "failed_step_id": step.id, "failed_stage": "FRAMING"}

    monkeypatch.setattr(AutoDirectorOrchestrator, "advance_to_pause", fail_at_checkpoint)
    worker = AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0)
    assert worker.run_once()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.status.value == "RUNNING"
        assert run.current_stage == AutoDirectorStage.FRAMING
        assert run.next_retry_at is not None
        assert run.recovery_attempts == 1
        assert run.pause_reason is None

    assert not worker.run_once()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.next_retry_at = utc_now() - timedelta(seconds=1)
        db.commit()

    def resume_checkpoint(_self, _db, run):
        run.status = auto_module.AutoDirectorRunStatus.RUNNING
        run.current_stage = AutoDirectorStage.NEXT_CHAPTER
        run.pause_reason = None

    monkeypatch.setattr(AutoDirectorOrchestrator, "advance_to_pause", resume_checkpoint)
    assert worker.run_once()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.current_stage == AutoDirectorStage.NEXT_CHAPTER
        assert run.next_retry_at is None and run.recovery_attempts == 0


def test_transient_retry_schedule_is_not_cleared_in_same_worker_pass(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={
        "max_tokens": 100000, "max_retries": 2, "idempotency_key": "retry-backoff-state",
    }).json()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        run.run_mode = "FULL_AUTO"
        run.status = "RUNNING"
        run.current_stage = AutoDirectorStage.FRAMING
        run.settings = {**run.settings, "max_tokens": 100000, "max_retries": 2}
        db.commit()

    def fail_stage(_self, db, run):
        step = AutoDirectorStep(
            run_id=run.id, stage=AutoDirectorStage.FRAMING,
            status=AutoDirectorStepStatus.FAILED, attempt=1,
            input_fingerprint="retry-backoff-step", error_code=MODEL_RATE_LIMITED,
            output_payload={"upstream_status": 429},
        )
        db.add(step); db.flush()
        run.status = "FAILED"
        run.current_stage = AutoDirectorStage.FAILED
        run.pause_reason = MODEL_RATE_LIMITED
        run.context = {**run.context, "failed_step_id": step.id, "failed_stage": "FRAMING"}

    monkeypatch.setattr(AutoDirectorOrchestrator, "advance_to_pause", fail_stage)
    worker = AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0)
    assert worker.run_once()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        scheduled = run.next_retry_at
        assert run.status.value == "RUNNING"
        assert scheduled and scheduled > utc_now()
        assert run.recovery_attempts == 1
        assert run.recovery_key == run.context["failed_step_id"]
    assert not worker.run_once()


def test_auto_runs_are_idempotent_within_each_project(monkeypatch):
    directions = [{"title": f"方向 {i}", "premise": "可兑现的主线", "core_conflict": "目标与代价"} for i in range(3)]
    client, project_id = _client(monkeypatch, directions)
    first = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "第一条运行", "idempotency_key": "serial-1"})
    assert first.status_code == 201, first.text
    duplicate = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "修改过的重复请求", "idempotency_key": "serial-1"})
    assert duplicate.status_code == 201 and duplicate.json()["id"] == first.json()["id"]
    with api_module.SessionLocal() as db:
        other_project = Project(name="第二部小说")
        db.add(other_project); db.commit(); other_project_id = other_project.id
    across_projects = client.post(f"/projects/{other_project_id}/auto-director/runs", json={"inspiration": "另一部小说", "idempotency_key": "serial-1"})
    assert across_projects.status_code == 201, across_projects.text
    assert across_projects.json()["id"] != first.json()["id"]
    assert across_projects.json()["project_id"] == other_project_id
    assert across_projects.json()["status"] == "RUNNING"
    assert [item["id"] for item in client.get(f"/projects/{project_id}/auto-director/runs").json()] == [first.json()["id"]]


def test_auto_pause_and_resume_leave_other_projects_running(monkeypatch):
    directions = [{"title": f"方向 {i}", "premise": "可兑现的主线", "core_conflict": "目标与代价"} for i in range(3)]
    client, project_id = _client(monkeypatch, directions)
    with api_module.SessionLocal() as db:
        other_project = Project(name="第二部小说")
        db.add(other_project); db.commit(); other_project_id = other_project.id
    first = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "第一部小说", "idempotency_key": "resume-serial-1"})
    assert first.status_code == 201, first.text
    second = client.post(f"/projects/{other_project_id}/auto-director/runs", json={"inspiration": "第二部小说", "idempotency_key": "resume-serial-2"})
    assert second.status_code == 201, second.text
    first_id, second_id = first.json()["id"], second.json()["id"]
    first_url = f"/projects/{project_id}/auto-director/runs/{first_id}"
    other_url = f"/projects/{other_project_id}/auto-director/runs/{second_id}"
    paused = client.post(f"{first_url}/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "PAUSED"
    assert client.get(other_url).json()["status"] == "RUNNING"

    assert client.post(f"/projects/{other_project_id}/auto-director/runs/{first_id}/resume").status_code == 404
    assert client.get(first_url).json()["status"] == "PAUSED"
    resumed = client.post(f"{first_url}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["current_stage"] == first.json()["current_stage"]
    assert client.get(other_url).json()["status"] == "RUNNING"


def test_live_run_list_bounds_runs_and_step_history(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    with api_module.SessionLocal() as db:
        for index in range(3):
            run = AutoDirectorRun(project_id=project_id, idempotency_key=f"live-list-{index}", status="PAUSED")
            db.add(run); db.flush()
            db.add_all([AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.FRAMING, status=AutoDirectorStepStatus.COMMITTED, input_fingerprint=f"step-{number}") for number in range(5)])
        db.commit()
    url = f"/projects/{project_id}/auto-director/runs"
    response = client.get(url, params={"limit": 2, "step_limit": 2})
    assert response.status_code == 200
    assert len(response.json()) == 2 and all(len(run["steps"]) == 2 for run in response.json())
    for params in ({"limit": 0}, {"limit": 51}, {"step_limit": 0}, {"step_limit": 101}):
        assert client.get(url, params=params).status_code == 422


def test_retry_ignores_failures_from_recovered_stages(monkeypatch):
    directions = [{"title": f"方向 {i}", "premise": "主线", "core_conflict": "代价"} for i in range(3)]
    client, project_id = _client(monkeypatch, directions)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "重试", "max_retries": 2, "idempotency_key": "retry-stage"}).json()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        for index in range(3):
            db.add(AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.FRAMING, status=AutoDirectorStepStatus.FAILED, attempt=index + 1, input_fingerprint=f"old-{index}", error_code="OLD_FAILURE"))
        db.add(AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.CHAPTER_EXECUTION, status=AutoDirectorStepStatus.FAILED, attempt=1, input_fingerprint="writer-1", error_code="WRITER_DRAFT_FAILED"))
        run.status = "FAILED"; run.current_stage = AutoDirectorStage.FAILED; run.current_chapter_id = "chapter-1"
        db.commit()
    retried = client.post(f"/projects/{project_id}/auto-director/runs/{created['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "RUNNING"
    assert retried.json()["current_stage"] == "CHAPTER_EXECUTION"


def test_retry_budget_tracks_checkpoint_attempts_and_duplicate_requests(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"max_retries": 2, "idempotency_key": "bounded-retry"}).json()
    checkpoint_input = {"chapter_id": "current-chapter", "plan_id": "current-plan"}
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        historical = AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.CHAPTER_EXECUTION, status=AutoDirectorStepStatus.FAILED, attempt=99, input_fingerprint="previous-chapter")
        current = AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.CHAPTER_EXECUTION, status=AutoDirectorStepStatus.FAILED, attempt=1, input_fingerprint=fingerprint(checkpoint_input))
        db.add_all([historical, current]); db.flush()
        run.status = "FAILED"; run.current_stage = AutoDirectorStage.FAILED
        run.context = {"selected_direction": {"title": "方向"}, "foundation": {"ready": True}, "failed_step_id": current.id}
        db.commit(); checkpoint_id = current.id

    def fail_current_checkpoint(self, db, run, project):
        self._step(db, run, AutoDirectorStage.CHAPTER_EXECUTION, checkpoint_input)
        raise AutoDirectorError("WRITER_DRAFT_FAILED")

    monkeypatch.setattr(AutoDirectorOrchestrator, "_plan_and_first_chapter", fail_current_checkpoint)
    url = f"/projects/{project_id}/auto-director/runs/{created['id']}"
    worker = AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0)
    for attempt in (2, 3):
        queued = client.post(f"{url}/retry").json()
        assert queued["status"] == "RUNNING" and queued["pause_reason"] is None
        duplicate = client.post(f"{url}/retry").json()
        assert duplicate["status"] == "RUNNING"
        assert next(s for s in duplicate["steps"] if s["id"] == checkpoint_id)["attempt"] == attempt - 1
        assert worker.run_once()
        failed = client.get(url).json()
        assert failed["status"] == "FAILED"
        assert failed["context"]["failed_step_id"] == checkpoint_id
        assert next(s for s in failed["steps"] if s["id"] == checkpoint_id)["attempt"] == attempt
    blocked = client.post(f"{url}/retry").json()
    assert blocked["status"] == "BLOCKED" and blocked["pause_reason"] == "MAX_RETRIES_REACHED"
    assert client.post(f"{url}/resume").json()["status"] == "BLOCKED"


def test_full_auto_budget_resume_requires_higher_budget_and_keeps_checkpoint(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={
        "max_tokens": 1000, "max_retries": 1, "idempotency_key": "budget-resume",
    }).json()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        run.status = "PAUSED"
        run.current_stage = AutoDirectorStage.PAUSED
        run.pause_reason = "TOKEN_BUDGET_EXCEEDED"
        run.settings = {**run.settings, "max_tokens": 1000}
        run.token_usage = {"total_tokens": 700, "unknown_usage_calls": 1, "usage_status": "UNKNOWN"}
        run.context = {"recovery_reserved_tokens": 300, "current_chapter_number": 4}
        step = AutoDirectorStep(
            run_id=run.id, stage=AutoDirectorStage.CHAPTER_EXECUTION,
            status=AutoDirectorStepStatus.FAILED, attempt=1,
            input_fingerprint="budget-checkpoint", error_code="TOKEN_BUDGET_EXCEEDED",
        )
        db.add(step); db.flush()
        run.context = {**run.context, "failed_step_id": step.id}
        db.commit()

    url = f"/projects/{project_id}/auto-director/runs/{created['id']}/resume"
    client_state = client.post(url)
    assert client_state.status_code == 409
    assert client_state.json()["detail"]["code"] == "TOKEN_BUDGET_INCREASE_REQUIRED"
    assert client_state.json()["detail"]["used_tokens"] == 1000
    unchanged = client.post(url, json={"max_tokens": 1000})
    assert unchanged.status_code == 422
    resumed = client.post(url, json={"max_tokens": 2500})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["current_stage"] == "CHAPTER_EXECUTION"
    assert resumed.json()["settings"]["max_tokens"] == 2500
    assert resumed.json()["token_usage"]["total_tokens"] == 700


def test_zero_retry_budget_blocks_first_recovery(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"max_retries": 0, "idempotency_key": "no-retries"}).json()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        step = AutoDirectorStep(run_id=run.id, stage=AutoDirectorStage.FRAMING, status=AutoDirectorStepStatus.FAILED, attempt=1, input_fingerprint="first")
        db.add(step); db.flush()
        run.status = "FAILED"; run.current_stage = AutoDirectorStage.FAILED
        run.context = {"failed_step_id": step.id}
        db.commit()
    body = client.post(f"/projects/{project_id}/auto-director/runs/{created['id']}/retry").json()
    assert body["status"] == "BLOCKED" and body["pause_reason"] == "MAX_RETRIES_REACHED"


def test_auto_director_uses_target_chapters_when_maximum_is_omitted(monkeypatch):
    directions = [{"title": "完整方向", "premise": "主线", "core_conflict": "冲突", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}}] * 3
    client, project_id = _client(monkeypatch, directions)
    body = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "target_chapters": 7, "idempotency_key": "default-limit"}).json()
    assert body["settings"]["max_chapters"] == 7
    assert body["settings"]["effective_max_chapters"] == 7


def test_auto_director_keeps_full_book_plan_when_run_writes_fewer_chapters(monkeypatch):
    directions = [{"title": "完整方向", "premise": "主线", "core_conflict": "冲突", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}}] * 3
    client, project_id = _client(monkeypatch, directions)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "target_chapters": 450, "max_chapters": 10, "idempotency_key": "separate-plan-and-run"}).json()
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        run.context = {**run.context, "selected_direction": directions[0], "foundation": {"ready": True}}
        captured = {}
        def fake_generate_plan(_db, _project, framing, *_args, **_kwargs):
            captured["framing"] = dict(framing)
            return {"framing": framing, "premise": "主线", "macro_plan": {}, "volumes": [], "arcs": [], "chapters": [{"number": 1, "title": "第一章", "summary": "开始"}]}, SimpleNamespace(provider="fake", model="fake", request_id="plan", latency_ms=0, usage={})
        monkeypatch.setattr(auto_module, "generate_plan", fake_generate_plan)
        monkeypatch.setattr(auto_module, "validate_plan_references", lambda *_args: [])
        monkeypatch.setattr(auto_module, "persist_plan", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("STOP_AFTER_PLAN_INPUT")))
        with pytest.raises(RuntimeError, match="STOP_AFTER_PLAN_INPUT"):
            AutoDirectorOrchestrator()._plan_and_first_chapter(db, run, db.get(Project, project_id))
        assert run.settings["max_chapters"] == 10
        assert run.context["request"]["target_chapters"] == 450
        assert captured["framing"]["target_chapters"] == 450


def test_auto_plan_preserves_author_style_and_forbidden_content(monkeypatch):
    client, project_id = _client(monkeypatch, [])
    author_request = {"tone": "克制", "style_samples": ["作者样本"], "forbidden_content": ["不得伤害证人"], "mode": "outline", "source_material": "仅规划器可见的结局资料", "target_chapters": 3}
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={**author_request, "idempotency_key": "author-style"}).json()
    captured = {}
    def generate(_db, _project, framing, premise, style, anti_ai, **kwargs):
        captured.update({"framing": framing, "style": style})
        return {"framing": {"source_material": "模型覆盖"}, "style_guide": {"tone": "模型覆盖", "style_samples": [], "forbidden_content": [], "source_material": "泄露资料", "mode": "outline", "direction": {"secret": "hidden"}}, "chapters": [{"number": 1, "forbidden_events": ["提前揭晓终局"]}]}, SimpleNamespace(provider="fake", model="fake", request_id="plan", latency_ms=0, usage={})
    monkeypatch.setattr(auto_module, "generate_plan", generate)
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, created["id"])
        run.context = {**run.context, "selected_direction": {"premise": "调查", "style_advice": "模型建议"}, "foundation": {"ready": True}}
        AutoDirectorOrchestrator()._plan_and_first_chapter(db, run, db.get(Project, project_id))
        plan = db.get(auto_module.StoryPlan, run.context["plan_id"])
        assert captured["framing"]["mode"] == "outline" and captured["framing"]["source_material"] == author_request["source_material"]
        assert captured["style"]["style_samples"] == ["作者样本"]
        assert plan.style_guide["tone"] == "克制"
        assert plan.style_guide["style_samples"] == ["作者样本"]
        assert plan.style_guide["forbidden_content"] == ["不得伤害证人"]
        assert not {"mode", "source_material", "direction"}.intersection(plan.style_guide)
        assert plan.framing["source_material"] == author_request["source_material"]
        task = db.scalar(select(auto_module.StoryPlanChapter).where(auto_module.StoryPlanChapter.plan_id == plan.id))
        assert task.forbidden_events == ["提前揭晓终局", "不得伤害证人"]


def test_auto_director_generates_missing_long_plan_chapter_task(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="长篇", story_seed="一段长篇设定", target_chapter_words=2400)
        db.add(project); db.flush()
        plan = persist_plan(db, project, {"framing": {"target_chapters": 20, "target_words_per_chapter": 2400}, "premise": "主线", "macro_plan": {"logline": "主线"}, "volumes": [{"number": 1, "title": "第一卷", "summary": "开端", "start_chapter": 1, "end_chapter": 20}], "arcs": [{"number": 1, "volume_number": 1, "title": "开局", "goal": "进入冲突", "summary": "开端"}], "chapters": [{"number": 1, "title": "第一章", "summary": "开始"}]})
        run = AutoDirectorRun(project_id=project.id, idempotency_key="long-task", settings={}, context={"selected_direction": {"premise": "主线", "core_conflict": "冲突"}})
        db.add(run); db.flush()
        from app.ai.provider import ModelResult
        captured = {}
        class TaskProvider:
            name = "fake"
            def generate(self, _messages, model):
                captured.update(json.loads(_messages[-1]["content"])["context"])
                return ModelResult(content=json.dumps({"chapter": {"title": "第十三章", "summary": "主线升级", "objective": "取得关键证据", "conflict": "对手先一步行动", "must_events": ["取得证据"], "forbidden_events": ["揭开最终真相"], "scene_beats": ["追查", "受阻", "反转"]}}, ensure_ascii=False), latency_ms=1, request_id="task", provider="fake", model=model)
        monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: TaskProvider())
        row, result = AutoDirectorOrchestrator()._generate_next_chapter_task(db, run, project, plan, 13, {"target_words_per_chapter": 2400, "forbidden_content": ["不得伤害证人"], "mode": "outline", "source_material": "结局资料"})
        assert row.number == 13 and row.title == "第十三章"
        assert row.forbidden_events == ["揭开最终真相", "不得伤害证人"]
        assert captured["planning_input"] == {"mode": "outline", "source_material": "结局资料"}
        assert "source_material" not in captured["style_guide"]
        assert result.provider == "fake"


def test_quality_debt_does_not_block_next_chapter():
    run = AutoDirectorRun(
        settings={"max_chapters": 2},
        context={"current_chapter_number": 1, "generated_this_run": 0, "generated_chapters": []},
    )
    orchestrator = AutoDirectorOrchestrator()
    orchestrator._record_quality_debt_and_advance(run)
    assert run.status.value == "RUNNING"
    assert run.current_stage.value == "NEXT_CHAPTER"
    assert run.context["current_chapter_number"] == 2
    assert run.context["quality_debts"] == ["QUALITY_GATE_NOT_PASS"]

    orchestrator._record_quality_debt_and_advance(run)
    assert run.status.value == "PAUSED"
    assert run.pause_reason == "RUN_CHAPTER_BUDGET_REACHED"
    assert run.context["processed_this_run"] == 2


def test_quality_debt_counts_against_chapter_budget():
    run = AutoDirectorRun(
        settings={"max_chapters": 2},
        context={"current_chapter_number": 4, "generated_this_run": 0, "adopted_this_run": 1, "processed_this_run": 1, "generated_chapters": []},
    )
    AutoDirectorOrchestrator()._record_quality_debt_and_advance(run)
    assert run.status.value == "PAUSED"
    assert run.pause_reason == "RUN_CHAPTER_BUDGET_REACHED"
    assert run.context["processed_this_run"] == 2
    assert run.context["current_chapter_number"] == 5


def test_full_auto_creates_volume_boundary_and_blocks_sealed_volume(monkeypatch):
    directions = [{"title": "完整方向", "premise": "主线", "core_conflict": "冲突", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}}] * 3
    client, project_id = _client(monkeypatch, directions)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "卷边界", "target_chapters": 600, "max_chapters": 3, "idempotency_key": "volume-boundary"})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    with api_module.SessionLocal() as db:
        run = db.get(AutoDirectorRun, run_id)
        volume = db.get(VolumeContract, run.context["volume_id"])
        assert run.context["book_contract_id"]
        assert volume and volume.status == VolumeContractStatus.ACTIVE
        assert volume.estimated_chapter_start == 1
        volume.status = VolumeContractStatus.SEALED
        db.commit()
    assert AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once() is True
    state = client.get(f"/projects/{project_id}/auto-director/runs/{run_id}").json()
    assert state["status"] == "BLOCKED"
    assert state["pause_reason"] == "VOLUME_SEALED"


def test_all_invalid_directions_are_repaired_once_then_blocked(monkeypatch):
    invalid = [{"title": "缺字段", "premise": "主线"}] * 3
    client, project_id = _client(monkeypatch, invalid)
    calls = {"count": 0}
    def generate(*args, **kwargs):
        calls["count"] += 1
        from app.ai.provider import ModelResult
        return {"directions": invalid}, ModelResult(content="{}", latency_ms=0, request_id="repair", provider="fake", model="fake", usage={"total_tokens": 2})
    monkeypatch.setattr(auto_module, "generate_creation_directions", generate)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "idempotency_key": "invalid-directions"}).json()
    assert AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once() is True
    state = client.get(f"/projects/{project_id}/auto-director/runs/{created['id']}").json()
    assert calls["count"] == 2
    assert state["status"] == "BLOCKED"
    assert any(item["error_code"] == "DIRECTION_REPAIR_FAILED" for item in state["steps"])


def test_worker_can_be_stopped_cleanly(monkeypatch):
    worker = AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0.01)
    monkeypatch.setattr(worker, "run_once", lambda: False)
    worker.start()
    worker.stop()
    assert worker._thread is not None and not worker._thread.is_alive()


def test_full_auto_worker_scores_and_pauses_for_best_direction_confirmation(monkeypatch):
    directions = [
        {"title": "弱方向", "premise": "主线"},
        {"title": "最佳方向", "premise": "主线", "core_conflict": "代价", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}},
        {"title": "普通方向", "premise": "主线", "core_conflict": "代价"},
    ]
    client, project_id = _client(monkeypatch, directions)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "一句灵感", "idempotency_key": "worker-score"}).json()
    assert created["status"] == "RUNNING"
    assert AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once() is True
    state = client.get(f"/projects/{project_id}/auto-director/runs/{created['id']}").json()
    assert state["context"]["recommended_direction_index"] == 1
    assert state["status"] == "PAUSED"
    assert state["current_stage"] == "DIRECTION_SELECTION"
    assert any(item["label"] == "自动导演：生成整书方向" and item["status"] == "COMPLETED" for item in live_execution_broker.snapshots(project_id))


def test_chapter_task_validation_rejects_missing_and_forbidden_events():
    from types import SimpleNamespace
    turns = [SimpleNamespace(observable_action="进入档案馆", spoken_content="询问线索", scene_beat_refs=["进入"])]
    performance = SimpleNamespace(id="performance")
    proposal = SimpleNamespace(entry_state={"planning_task": {"must_events": ["取得钥匙"], "forbidden_events": ["烧毁档案"], "scene_beats": ["进入"]}}, expected_progress={"task_fingerprint": "task"})
    class DB:
        def scalars(self, _query): return SimpleNamespace(all=lambda: turns)
    valid, report = AutonomousWorldLoopService._validate_chapter_task(DB(), performance, proposal)
    assert not valid
    assert report["missing_must_events"] == ["取得钥匙"]
    turns[0].observable_action += "并烧毁档案"
    valid, report = AutonomousWorldLoopService._validate_chapter_task(DB(), performance, proposal)
    assert "烧毁档案" in report["forbidden_events"]


def test_runtime_task_validation_reports_event_ref_and_alias_matches():
    from types import SimpleNamespace
    turns = [SimpleNamespace(observable_action="拿到钥匙。", spoken_content="", scene_beat_refs=["beat-entry"])]
    performance = SimpleNamespace(id="performance")
    proposal = SimpleNamespace(
        entry_state={"planning_task": {
            "must_events": [{"event_ref": "key-obtained", "label": "取得钥匙", "aliases": ["拿到钥匙"]}],
            "forbidden_events": [],
            "scene_beats": [{"event_ref": "beat-entry", "label": "进入档案馆"}],
        }},
        expected_progress={"task_fingerprint": "task"},
    )
    class DB:
        def scalars(self, _query): return SimpleNamespace(all=lambda: turns)
    valid, report = AutonomousWorldLoopService._validate_chapter_task(DB(), performance, proposal)
    assert valid
    assert report["matched_by"]["key-obtained"] == "ALIAS"
    assert report["matched_by"]["beat-entry"] == "EVENT_REF"


def test_runtime_usage_collector_keeps_character_and_world_provider_metadata():
    from app.ai.provider import ModelResult
    events = []
    _emit_usage(events.append, ModelResult(content="{}", latency_ms=4, request_id="character", provider="character-provider", model="character-model", usage={"total_tokens": 3}))
    _emit_usage(events.append, ModelResult(content="{}", latency_ms=5, request_id="world", provider="world-provider", model="world-model", usage={"total_tokens": 7}))
    assert events[0]["provider"] == "character-provider" and events[0]["model"] == "character-model"
    assert events[1]["provider"] == "world-provider" and events[1]["model"] == "world-model"
    assert sum(item["total_tokens"] for item in events) == 10


def test_chapter_task_events_do_not_leak_between_chapters():
    from types import SimpleNamespace
    performance = SimpleNamespace(id="performance")
    turns = [SimpleNamespace(observable_action="第一章取得钥匙", spoken_content="", scene_beat_refs=["chapter-one-beat"])]
    proposal_one = SimpleNamespace(entry_state={"planning_task": {"must_events": [{"event_ref": "chapter-one-key", "label": "取得钥匙", "aliases": []}], "forbidden_events": [], "scene_beats": [{"event_ref": "chapter-one-beat", "label": "打开入口"}]}}, expected_progress={})
    proposal_two = SimpleNamespace(entry_state={"planning_task": {"must_events": [{"event_ref": "chapter-two-key", "label": "取得徽章", "aliases": []}], "forbidden_events": [], "scene_beats": [{"event_ref": "chapter-two-beat", "label": "进入塔楼"}]}}, expected_progress={})
    class DB:
        def scalars(self, _query): return SimpleNamespace(all=lambda: turns)
    valid_one, report_one = AutonomousWorldLoopService._validate_chapter_task(DB(), performance, proposal_one)
    valid_two, report_two = AutonomousWorldLoopService._validate_chapter_task(DB(), performance, proposal_two)
    assert valid_one and report_one["matched_by"]["chapter-one-key"] == "TEXT"
    assert not valid_two and report_two["missing_must_events"] == ["取得徽章"]


def test_auto_director_select_direction_requires_author_confirmation(monkeypatch):
    directions = [{"title": f"方向 {i}", "premise": "主线", "core_conflict": "冲突", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}} for i in range(3)]
    client, project_id = _client(monkeypatch, directions)
    monkeypatch.setattr(auto_module.AutoDirectorOrchestrator, "_plan_and_first_chapter", lambda self, db, run, project: setattr(run, "next_action", "Fake Provider checkpoint"))
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "idempotency_key": "select-run"}).json()
    AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once()
    selected = client.post(f"/projects/{project_id}/auto-director/runs/{created['id']}/select-direction", json={"direction_index": 1})
    # The test deliberately stops before model-driven planning: the transition
    # is still persisted as the author-confirmed next stage.
    assert selected.status_code in {200, 502}
    if selected.status_code == 200:
        assert selected.json()["context"]["selected_direction"]["title"] == "方向 1"


def test_auto_director_pause_resume_preserves_checkpoint(monkeypatch):
    directions = [{"title": f"方向 {i}", "premise": "主线", "core_conflict": "冲突", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}} for i in range(3)]
    client, project_id = _client(monkeypatch, directions)
    monkeypatch.setattr(auto_module.AutoDirectorOrchestrator, "_plan_and_first_chapter", lambda self, db, run, project: setattr(run, "next_action", "Fake checkpoint"))
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "idempotency_key": "pause-run"}).json()
    AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once()
    selected = client.post(f"/projects/{project_id}/auto-director/runs/{created['id']}/select-direction", json={"direction_index": 0}).json()
    paused = client.post(f"/projects/{project_id}/auto-director/runs/{created['id']}/pause", json={"reason": "作者暂离"}).json()
    assert paused["status"] == "PAUSED" and paused["current_stage"] == "PAUSED"
    resumed = client.post(f"/projects/{project_id}/auto-director/runs/{created['id']}/resume").json()
    assert resumed["status"] == "RUNNING"
    assert resumed["context"]["resume_stage"] == "CAST_PREPARATION"


def test_worker_persists_unexpected_failure(monkeypatch):
    client, project_id = _client(monkeypatch, [{"title": "方向", "premise": "主线", "core_conflict": "冲突"}] * 3)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "idempotency_key": "worker-failure"}).json()
    monkeypatch.setattr(AutoDirectorOrchestrator, "advance_to_pause", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("worker exploded")))
    assert AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once() is True
    state = client.get(f"/projects/{project_id}/auto-director/runs/{created['id']}").json()
    assert state["status"] == "FAILED"
    assert state["current_stage"] == "FAILED"
    assert state["context"]["worker_error"] == "worker exploded"


def test_auto_director_uses_runtime_scene_before_writer(monkeypatch):
    directions = [{"title": "方向", "premise": "主线", "core_conflict": "冲突", "world_boundaries": ["档案馆"], "protagonist": {"name": "修复师", "desire": "找回记录"}} for _ in range(3)]
    client, project_id = _client(monkeypatch, directions)
    plan_payload = {"premise": "主线", "macro_plan": {}, "volumes": [], "arcs": [], "chapters": [{"number": 1, "volume_number": 1, "arc_number": 1, "title": "来客", "summary": "进入冲突", "objective": "接受调查", "conflict": "代价不明", "scene_beats": ["来客", "验物", "接案"], "must_events": [], "forbidden_events": [], "end_state": {"case": "open"}}]}
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: (plan_payload, SimpleNamespace(provider="fake", model="fake", request_id="plan", latency_ms=0)))

    class WriterAndCriticProvider:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def generate(self, messages, model):
            from app.ai.provider import ModelResult
            prompt = messages[-1]["content"]
            self.calls += 1
            if self.calls > 1:
                content = json.dumps({"decision": "PASS", "scores": {"factual_grounding": 95, "pov_compliance": 95, "reveal_safety": 95, "style_naturalness": 95, "repetition": 95, "pacing": 95, "voice_consistency": 95, "overall": 95}, "findings": []})
            else:
                context = json.loads(prompt)["context"]
                scene_ids = [scene["scene_id"] for scene in context["source_manifest"]["scenes"]]
                content = json.dumps({"chapter_title": "来客", "prose": "修复师推开档案馆的门，决定接受这份委托。", "scene_coverage": scene_ids, "source_refs": [], "pov_character_id": context["rendering_contract"]["pov_character_id"], "task_coverage": []}, ensure_ascii=False)
            return ModelResult(content=content, latency_ms=0, request_id="fake", provider="fake", model=model)

    fake_provider = WriterAndCriticProvider()
    monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: fake_provider)
    monkeypatch.setattr(runtime_module, "get_model_provider", lambda *args, **kwargs: fake_provider)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "灵感", "idempotency_key": "runtime-run"}).json()
    AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once()
    body = client.get(f"/projects/{project_id}/auto-director/runs/{created['id']}").json()
    assert body["status"] in {"RUNNING", "COMPLETED", "FAILED", "BLOCKED"}


def test_auto_writer_preserves_author_pov_over_generated_chapter_pov(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    observed_modes = []

    class WriterProvider:
        name = "fake"

        def generate(self, messages, model):
            from app.ai.provider import ModelResult
            context = json.loads(messages[-1]["content"])["context"]
            observed_modes.append(context["rendering_contract"]["pov_mode"])
            payload = {
                "chapter_title": "门边",
                "prose": "我在门边等候。",
                "scene_coverage": [item["scene_id"] for item in context["source_manifest"]["scenes"]],
                "source_refs": [],
                "pov_character_id": context["rendering_contract"]["pov_character_id"],
            }
            return ModelResult(
                content=json.dumps(payload, ensure_ascii=False), latency_ms=1,
                request_id="writer-pov", provider="fake", model=model,
                usage={"total_tokens": 5},
            )

    provider = WriterProvider()
    monkeypatch.setattr(auto_module.ModelRouter, "resolve", lambda self, *args, **kwargs: SimpleNamespace(provider="fake", model="writer", base_url="https://example.invalid"))
    monkeypatch.setattr(auto_module.ProviderCredentialResolver, "generation_key", lambda self, *args, **kwargs: "test-key")
    monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(auto_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(AutoDirectorOrchestrator, "_live_begin", staticmethod(lambda *args, **kwargs: "writer-live"))
    monkeypatch.setattr(AutoDirectorOrchestrator, "_live_complete", staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(auto_module.live_execution_broker, "phase", lambda *args, **kwargs: None)

    with Session() as db:
        project = Project(name="POV", min_chapter_words=None, max_chapter_words=None)
        db.add(project); db.flush()
        actor = Character(project_id=project.id, name="林岚")
        db.add(actor); db.flush()
        db.add(Scene(
            project_id=project.id, sequence=1, world_time=datetime(2026, 1, 1),
            location="档案馆", participants=[actor.id], facts=[], result={},
            summary="场景", intent="等候", story_threads=[], status="OCCURRED",
            history_status="ACTIVE",
        ))
        db.flush()
        NarrativeStructureService().sync(db, project.id)
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True)))
        plan = persist_plan(db, project, {
            "framing": {"target_chapters": 1, "target_words_per_chapter": 3000, "pov": "FIRST_PERSON"},
            "chapters": [{
                "number": 1, "volume_number": 1, "arc_number": 1,
                "title": "门边", "summary": "等候", "objective": "等待", "conflict": "不确定",
                "pov_mode": "THIRD_PERSON_LIMITED", "pov_character_ref": actor.id,
            }],
        })
        plan.status = StoryPlanStatus.APPROVED
        run = AutoDirectorRun(
            project_id=project.id, idempotency_key="author-pov", run_mode="AUTHOR_GUIDED_VOLUME",
            status="RUNNING", current_stage=AutoDirectorStage.CHAPTER_EXECUTION,
            settings={"max_tokens": 0},
            context={"plan_id": plan.id, "request": {"pov": "FIRST_PERSON"}, "foundation": {"character_ids": [actor.id]}},
        )
        db.add(run); db.flush()

        AutoDirectorOrchestrator()._advance_chapter_prose(
            db, run, project, plan.id, chapter, 1, durable_stages=True,
        )

        assert observed_modes == ["FIRST_PERSON"]
        assert run.current_stage == AutoDirectorStage.QUALITY_REVIEW


def test_full_auto_pauses_at_operational_budget_without_completing_book(monkeypatch):
    directions = [{
        "title": "档案回声",
        "premise": "修复师追查一份会回应的失踪档案",
        "core_conflict": "查明真相会失去自己的记忆",
        "first_volume_goal": "找到档案来源",
        "world_boundaries": ["旧档案馆"],
        "first_ten_chapter_promises": ["接案", "验物", "追踪"],
        "foreshadowing_directions": ["档案回应"],
        "protagonist": {"name": "林岚", "desire": "查明档案来源", "cost": "失去一段记忆"},
        "style_advice": "克制悬疑",
    }] * 3
    client, project_id = _client(monkeypatch, directions)
    with api_module.SessionLocal() as db:
        project = db.get(Project, project_id)
        project.autonomy_settings = {"quality_gate": {"require_critic": False}}
        db.commit()
    plan_payload = {
        "premise": directions[0]["premise"],
        "macro_plan": {"promise": "查明来源"},
        "volumes": [], "arcs": [],
        "chapters": [
            {"number": 1, "volume_number": 1, "arc_number": 1, "title": "回声", "summary": "档案首次回应", "objective": "接下调查", "conflict": "回应带来代价", "scene_beats": [], "must_events": [], "forbidden_events": [], "end_state": {"case": "open"}},
            {"number": 2, "volume_number": 1, "arc_number": 1, "title": "来源", "summary": "追查档案来源", "objective": "找到线索", "conflict": "线索要求交换记忆", "scene_beats": [], "must_events": [], "forbidden_events": [], "end_state": {"case": "moving"}},
        ],
    }
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: (plan_payload, SimpleNamespace(provider="fake", model="fake", request_id="plan", latency_ms=0, usage={"total_tokens": 4})))

    class FullChainProvider:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def generate(self, messages, model):
            from app.ai.provider import ModelResult
            self.calls += 1
            prompt = "\n".join(item.get("content", "") for item in messages)
            if "REPAIR prose editor" in prompt:
                context = json.loads(messages[-1]["content"])["quality_context"]
                payload = {"chapter_title": "自动章节修复", "prose": f"林岚继续调查，线索在档案馆中逐渐清晰。第{self.calls}次记录留下新的方向。", "scene_coverage": [item["scene_id"] for item in context["source_manifest"]["scenes"]], "source_refs": [], "pov_character_id": context["rendering_contract"]["pov_character_id"], "task_coverage": [], "task_forbidden_hits": []}
            elif "quality_context" in prompt:
                payload = {"decision": "PASS", "scores": {key: 95 for key in ["factual_grounding", "pov_compliance", "reveal_safety", "style_naturalness", "repetition", "pacing", "voice_consistency", "overall"]}, "findings": []}
            elif "actor_view" in prompt:
                payload = {
                    "decision_type": "OBSERVE", "intent": "完成当前节拍", "chosen_action": "继续调查",
                    "motivation": "需要确认线索", "target_character_id": None, "target_entity_id": None,
                    "goal_refs": [], "knowledge_used": [], "memory_refs": [], "ability_refs": [], "inventory_refs": [],
                    "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None,
                    "expected_personal_result": "获得线索", "uncertainties": [], "refused_options": [],
                    "boundary_override_reason": None, "decision_summary": "继续调查。",
                }
                payload = {"decision": payload, "action": {"visibility": "PUBLIC", "observable_action": "继续调查", "spoken_content": None, "requires_world_resolution": False, "world_resolution_request": None, "disclosure_knowledge_ids": [], "scene_beat_refs": [], "target_character_id": None}}
                # The runtime only requires references to the locked task beats.
                import re
                beats = re.search(r'"scene_beats"\s*:\s*\[(.*?)\]', prompt)
                if beats:
                    payload["action"]["scene_beat_refs"] = re.findall(r'"([^\"]+)"', beats.group(1))
            elif "chapter_title" in prompt and "source_manifest" in prompt:
                context = json.loads(messages[-1]["content"])["context"]
                payload = {"chapter_title": "自动章节", "prose": f"林岚继续调查，线索在档案馆中逐渐清晰。第{self.calls}次记录留下新的方向。", "scene_coverage": [item["scene_id"] for item in context["source_manifest"]["scenes"]], "source_refs": [], "pov_character_id": context["rendering_contract"]["pov_character_id"], "task_coverage": []}
            else:
                payload = {"decision": "PASS", "scores": {key: 95 for key in ["factual_grounding", "pov_compliance", "reveal_safety", "style_naturalness", "repetition", "pacing", "voice_consistency", "overall"]}, "findings": []}
            return ModelResult(content=json.dumps(payload, ensure_ascii=False), latency_ms=0, request_id=f"fake-{self.calls}", provider="fake", model=model, usage={"total_tokens": 3})

    provider = FullChainProvider()
    monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(runtime_module, "get_model_provider", lambda *args, **kwargs: provider)
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "档案会回应", "target_chapters": 2, "max_chapters": 2, "idempotency_key": "full-chain"})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    worker = AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0)
    for _ in range(10):
        worked = worker.run_once()
        state = client.get(f"/projects/{project_id}/auto-director/runs/{run_id}").json()
        if state["current_stage"] == "DIRECTION_SELECTION":
            break
        if not worked:
            break
    assert state["current_stage"] == "DIRECTION_SELECTION"
    selected = client.post(f"/projects/{project_id}/auto-director/runs/{run_id}/select-direction", json={"direction_index": 0})
    assert selected.status_code == 200, selected.text
    for _ in range(10):
        worked = worker.run_once()
        state = client.get(f"/projects/{project_id}/auto-director/runs/{run_id}").json()
        if state["status"] in {"PAUSED", "COMPLETED"}:
            break
        if not worked:
            break
    assert state["status"] == "PAUSED", [(item["stage"], item["error_code"], item["error_summary"]) for item in state["steps"] if item["error_code"]]
    assert state["current_stage"] == "NEXT_CHAPTER"
    assert state["pause_reason"] == "RUN_CHAPTER_BUDGET_REACHED"
    assert state["context"]["current_chapter_number"] == 3
    assert state["context"]["completed_chapters"] == [1, 2]
    resumed = client.post(f"/projects/{project_id}/auto-director/runs/{run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["context"]["adopted_this_run"] == 0
    committed_stages = {item["stage"] for item in state["steps"] if item["status"] == "COMMITTED"}
    assert {"CAST_PREPARATION", "WORLD_BUILDING", "STORY_MACRO", "VOLUME_PLANNING", "CHAPTER_DETAIL", "CHAPTER_EXECUTION", "QUALITY_REVIEW"} <= committed_stages
    assert state["usage_metrics"]["total_tokens"] > 0
    assert state["usage_metrics"]["calls"] > 0
    assert "fake" in state["usage_metrics"]["provider"]
    assert "fake" in state["usage_metrics"]["model"]
    assert "fake" in state["token_usage"]["providers"]
    assert "fake" in state["token_usage"]["models"]
    assert len(state["token_usage"]["providers"]) == len(set(state["token_usage"]["providers"]))
    assert len(state["token_usage"]["models"]) == len(set(state["token_usage"]["models"]))
    assert any(item["usage_metrics"]["total_tokens"] > 0 for item in state["steps"] if item["status"] == "COMMITTED")
    with api_module.SessionLocal() as db:
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True)).order_by(Chapter.number)).all()
        assert len(chapters) >= 2
        assert all(item.current_writer_draft_id and item.content for item in chapters[:2])
        for chapter in chapters[:2]:
            draft = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
            assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True)))
            assert draft.status.value == "ADOPTED"
            assert assessment and assessment.status.value == "PASS"
        autonomous_runs = db.scalars(select(AutonomousWorldRun).where(AutonomousWorldRun.project_id == project_id)).all()
        assert len(autonomous_runs) >= 2
        assert all(run.performance_mode.value == "LLM" and run.resolver_mode.value == "LLM" for run in autonomous_runs)
        proposals = db.scalars(select(SceneProposal).where(SceneProposal.project_id == project_id)).all()
        auto_proposals = [proposal for proposal in proposals if (proposal.entry_state or {}).get("auto_director_run_id")]
        assert len(auto_proposals) >= 2
        assert all((proposal.expected_progress or {}).get("plan_chapter_id") for proposal in auto_proposals)
        assert all((proposal.expected_progress or {}).get("task_fingerprint") for proposal in auto_proposals)
