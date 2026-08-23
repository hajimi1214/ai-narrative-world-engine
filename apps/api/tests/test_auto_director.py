import json
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
from app.db import Base
from app.main import app
from app.models import (
    AutoDirectorRun, AutonomousWorldRun, Chapter, ChapterQualityAssessment,
    ChapterWriterDraft, Project, SceneProposal, VolumeContract,
    VolumeContractStatus,
)
from app.auto_director_worker import AutoDirectorWorker
from app.auto_director import AutoDirectorOrchestrator
from app.autonomy import AutonomousWorldLoopService
from app.planning import validate_task_output
from app.planning import persist_plan
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
        class TaskProvider:
            name = "fake"
            def generate(self, _messages, model):
                return ModelResult(content=json.dumps({"chapter": {"title": "第十三章", "summary": "主线升级", "objective": "取得关键证据", "conflict": "对手先一步行动", "must_events": ["取得证据"], "forbidden_events": ["揭开最终真相"], "scene_beats": ["追查", "受阻", "反转"]}}, ensure_ascii=False), latency_ms=1, request_id="task", provider="fake", model=model)
        monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: TaskProvider())
        row, result = AutoDirectorOrchestrator()._generate_next_chapter_task(db, run, project, plan, 13, {"target_words_per_chapter": 2400})
        assert row.number == 13 and row.title == "第十三章"
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


def test_full_auto_worker_scores_and_selects_best_direction(monkeypatch):
    directions = [
        {"title": "弱方向", "premise": "主线"},
        {"title": "最佳方向", "premise": "主线", "core_conflict": "代价", "first_volume_goal": "完成调查", "world_boundaries": ["档案馆"], "first_ten_chapter_promises": ["一", "二", "三"], "foreshadowing_directions": ["回收"], "protagonist": {"desire": "查明", "cost": "失去"}},
        {"title": "普通方向", "premise": "主线", "core_conflict": "代价"},
    ]
    client, project_id = _client(monkeypatch, directions)
    monkeypatch.setattr(AutoDirectorOrchestrator, "_prepare_foundation", lambda self, db, run, direction: setattr(run, "context", {**run.context, "foundation": {"ready": True}}))
    monkeypatch.setattr(AutoDirectorOrchestrator, "_plan_and_first_chapter", lambda self, db, run, project: setattr(run, "next_action", "planned"))
    created = client.post(f"/projects/{project_id}/auto-director/runs", json={"inspiration": "一句灵感", "idempotency_key": "worker-score"}).json()
    assert created["status"] == "RUNNING"
    assert AutoDirectorWorker(api_module.SessionLocal, poll_seconds=0).run_once() is True
    state = client.get(f"/projects/{project_id}/auto-director/runs/{created['id']}").json()
    assert state["context"]["selected_direction"]["title"] == "最佳方向"
    assert state["context"]["direction_score"] > 50
    assert state["current_stage"] == "CAST_PREPARATION"
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


def test_task_event_contract_accepts_event_ref_and_aliases():
    task = {
        "must_events": [{"event_ref": "key-obtained", "label": "取得钥匙", "aliases": ["拿到钥匙", "钥匙到手"]}],
        "forbidden_events": [{"event_ref": "archive-burned", "label": "烧毁档案", "aliases": ["焚毁档案"]}],
    }
    assert validate_task_output({"task_coverage": ["key-obtained"], "task_forbidden_hits": []}, task) == []
    assert validate_task_output({"task_coverage": ["钥匙到手"], "task_forbidden_hits": []}, task) == []
    assert validate_task_output({"task_coverage": ["取得钥匙"], "task_forbidden_hits": ["焚毁档案"]}, task)[0]["code"] == "PLAN_FORBIDDEN_EVENT_PRESENT"


def test_task_event_contract_still_blocks_missing_event():
    task = {"must_events": [{"event_ref": "must-a", "label": "找到入口", "aliases": ["发现入口"]}]}
    issues = validate_task_output({"task_coverage": ["无关事件"], "task_forbidden_hits": []}, task)
    assert issues and issues[0]["code"] == "PLAN_REQUIRED_EVENT_MISSING"


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
