from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import app.auto_director as auto_module
import app.runtime as runtime_module
from app.ai.errors import MODEL_RATE_LIMITED, ModelProviderError
from app.ai.provider import ModelResult
from app.author_guided_volume import AuthorGuidedVolumeService
from app.models import AutoDirectorRun, AutoDirectorStage, AutoDirectorStep, AutonomousWorldRun, Chapter, ChapterQualityAssessment, ChapterWorldBranch, ChapterWriterDraft, Scene
from app.author_recovery import recovery_payload, utc_now
from app.auto_director_worker import AutoDirectorWorker
from test_author_guided_volume import _setup


class InterruptedProcess(BaseException):
    pass


@pytest.fixture
def author_run(monkeypatch):
    client, sessions, project_id = _setup(monkeypatch)

    class Provider:
        name = "fake"

        def __init__(self):
            self.writer_calls = 0
            self.actor_calls = 0
            self.critic_calls = 0
            self.critic_failure = None
            self.critic_decision = "PASS"

        def generate(self, messages, model):
            prompt = "\n".join(item.get("content", "") for item in messages)
            if "actor_view" in prompt:
                self.actor_calls += 1
                actor_view = json.loads(messages[1]["content"])["actor_view"]
                beats = (actor_view.get("planning_task") or {}).get("scene_beats", [])
                payload = {
                    "decision": {
                        "decision_type": "OBSERVE", "intent": "调查", "chosen_action": "；".join(beats) or "检查档案并比对旧印记", "motivation": "发现线索",
                        "target_character_id": None, "target_entity_id": None, "goal_refs": [], "knowledge_used": [], "memory_refs": [],
                        "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None,
                        "expected_personal_result": "核对印记", "uncertainties": [], "refused_options": [], "boundary_override_reason": None, "decision_summary": "检查档案。",
                    },
                    "action": {
                        "visibility": "PUBLIC", "observable_action": "；".join(beats) or "检查档案并比对旧印记", "spoken_content": None, "requires_world_resolution": False,
                        "world_resolution_request": None, "disclosure_knowledge_ids": [], "target_character_id": None,
                        "scene_beat_refs": beats,
                    },
                }
            elif "quality_context" in prompt:
                self.critic_calls += 1
                if self.critic_failure:
                    failure, self.critic_failure = self.critic_failure, None
                    raise failure
                payload = {
                    "decision": self.critic_decision,
                    "scores": dict.fromkeys(("factual_grounding", "pov_compliance", "reveal_safety", "style_naturalness", "repetition", "pacing", "voice_consistency", "overall"), 95),
                    "findings": [],
                }
            else:
                self.writer_calls += 1
                context = json.loads(messages[-1]["content"])["context"]
                payload = {"chapter_title": "回声", "prose": "修复师翻过封面，看到一枚褪色的旧印记。", "scene_coverage": [item["scene_id"] for item in context["source_manifest"]["scenes"]], "source_refs": [], "pov_character_id": context["rendering_contract"]["pov_character_id"], "task_coverage": []}
            return ModelResult(content=json.dumps(payload, ensure_ascii=False), latency_ms=0, request_id="recovery-fake", provider="fake", model=model, usage={"total_tokens": 3})

    provider = Provider()
    monkeypatch.setattr(auto_module, "get_model_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(runtime_module, "get_model_provider", lambda *args, **kwargs: provider)
    base = f"/projects/{project_id}/author-guided-volume/runs"
    created = client.post(base, json={"window_size": 1, "idempotency_key": "author-recovery-stages", "continuous_chapter_progression": True, "operational_run_chapter_budget": 1})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]

    def tick():
        # Each worker poll has a fresh DB session. An interrupted call exits
        # without committing its own changes, exactly as a dead worker would.
        with sessions(info={"author_recovery": True}) as db:
            run = db.get(AutoDirectorRun, run_id)
            AuthorGuidedVolumeService().advance_run(db, run)
            stage = db.info.get("author_checkpoint_stage")
            db.commit()
            return stage

    def through(stage):
        for _ in range(10):
            if tick() == stage.value:
                return
            with sessions() as db:
                run = db.get(AutoDirectorRun, run_id)
                worlds = db.scalars(select(AutonomousWorldRun).where(AutonomousWorldRun.project_id == project_id)).all()
                assert run.status.value == "RUNNING", (run.current_stage, run.pause_reason, [(world.stop_reason, world.last_error_code) for world in worlds])
        pytest.fail(f"Did not persist {stage.value}")

    return client, sessions, project_id, run_id, provider, tick, through


def test_restart_reuses_draft_and_critic_report_until_adoption_commits(author_run, monkeypatch):
    _, sessions, project_id, run_id, provider, tick, through = author_run
    through(AutoDirectorStage.CHAPTER_EXECUTION)
    with sessions() as db:
        draft_id = db.scalar(select(ChapterWriterDraft.id).where(ChapterWriterDraft.project_id == project_id))
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id))
        assert draft_id and not chapter.current_writer_draft_id
        assert provider.writer_calls == 1 and provider.critic_calls == 0
    actor_calls = provider.actor_calls
    provider.critic_failure = InterruptedProcess()
    with pytest.raises(InterruptedProcess):
        tick()
    assert tick() == AutoDirectorStage.QUALITY_REVIEW.value
    with sessions() as db:
        assessment_id = db.scalar(select(ChapterQualityAssessment.id).where(ChapterQualityAssessment.writer_draft_id == draft_id))
        assert assessment_id and not db.get(ChapterQualityAssessment, assessment_id).approved_at

    adopt = auto_module.AutoDirectorOrchestrator.adopt_chapter

    def interrupt_after_adopt(self, db, run):
        adopt(self, db, run)
        raise InterruptedProcess()

    with monkeypatch.context() as patch:
        patch.setattr(auto_module.AutoDirectorOrchestrator, "adopt_chapter", interrupt_after_adopt)
        with pytest.raises(InterruptedProcess):
            tick()
    assert tick() == AutoDirectorStage.AUTHOR_ADOPTION.value
    with sessions() as db:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id))
        assert chapter.current_writer_draft_id == draft_id
        assert chapter.current_quality_assessment_id == assessment_id
        assert db.get(ChapterQualityAssessment, assessment_id).approved_at
        assert db.get(AutoDirectorRun, run_id).context["processed_this_run"] == 1
        scene_count = db.scalar(select(func.count(Scene.id)).where(Scene.project_id == project_id))
        assert scene_count == 5
    assert (provider.actor_calls, provider.writer_calls, provider.critic_calls) == (actor_calls, 1, 2)


def test_expired_worker_claim_resumes_same_run_and_chapter_branch(author_run):
    client, sessions, project_id, run_id, provider, _, through = author_run
    through(AutoDirectorStage.CHAPTER_EXECUTION)

    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        branch_id = run.context["chapter_world_branch_id"]
        branch = db.get(ChapterWorldBranch, branch_id)
        chapter_number = branch.chapter_number
        assert branch.run_id == run_id
        assert branch.status.value in {"OPEN", "READY"}
        assert provider.writer_calls == 1

    crashed_worker = AutoDirectorWorker(sessions, poll_seconds=0, lease_seconds=45)
    stale_claim = crashed_worker._claim()
    assert stale_claim and stale_claim[0] == run_id
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.worker_lease_until = utc_now() - timedelta(seconds=1)
        db.commit()

    restarted_worker = AutoDirectorWorker(sessions, poll_seconds=0, lease_seconds=45)
    assert restarted_worker.run_once() is True
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        branch = db.get(ChapterWorldBranch, branch_id)
        chapter = db.get(Chapter, branch.chapter_id)
        assert run.id == run_id
        assert run.context["chapter_world_branch_id"] == branch_id
        assert run.context["current_chapter_number"] == chapter_number
        assert branch.run_id == run.id and branch.chapter_number == chapter_number
        assert chapter.pending_world_branch_id == branch_id
        assert db.scalar(select(func.count(ChapterWorldBranch.id)).where(ChapterWorldBranch.run_id == run_id)) == 1
        assert recovery_payload(run)["last_checkpoint"] is not None
        assert run.recovery_usage_uncertain is True
        assert int(run.context.get("recovery_reserved_tokens") or 0) > 0
        assert provider.writer_calls == 1

    taken_over = client.post(
        f"/projects/{project_id}/author-guided-volume/runs/{run_id}/takeover"
    )
    assert taken_over.status_code == 200
    assert taken_over.json()["id"] == run_id
    resumed = client.post(
        f"/projects/{project_id}/author-guided-volume/runs/{run_id}/continue"
    )
    assert resumed.status_code == 200
    assert resumed.json()["id"] == run_id
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["context"]["chapter_world_branch_id"] == branch_id
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.context["chapter_world_branch_id"] == branch_id
        assert db.get(ChapterWorldBranch, branch_id).chapter_number == chapter_number


def test_critic_rate_limit_preserves_typed_failure_and_retries_only_critic(author_run):
    client, sessions, project_id, run_id, provider, tick, through = author_run
    through(AutoDirectorStage.CHAPTER_EXECUTION)
    provider.critic_failure = ModelProviderError(MODEL_RATE_LIMITED, upstream_status=429)
    tick()
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        step_id = run.context["failed_step_id"]
        step = db.get(AutoDirectorStep, step_id)
        assert run.pause_reason == step.error_code == MODEL_RATE_LIMITED
        assert step.output_payload["upstream_status"] == 429
        assert step.stage == AutoDirectorStage.QUALITY_REVIEW and step.attempt == 1
    retried = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/retry")
    assert retried.status_code == 200, retried.text
    assert tick() == AutoDirectorStage.QUALITY_REVIEW.value
    assert tick() == AutoDirectorStage.AUTHOR_ADOPTION.value
    with sessions() as db:
        assert db.get(AutoDirectorStep, step_id).attempt == 2
    assert (provider.writer_calls, provider.critic_calls) == (1, 2)


def test_planning_budget_pause_reopens_partial_checkpoint(monkeypatch):
    client, sessions, project_id = _setup(monkeypatch)
    created = client.post(
        f"/projects/{project_id}/author-guided-volume/runs",
        json={"window_size": 1, "operational_token_budget": 3, "idempotency_key": "planning-budget-resume"},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    from app.auto_director_worker import AutoDirectorWorker
    worker = AutoDirectorWorker(sessions, poll_seconds=0)
    assert worker.run_once() is True
    assert client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/continue").status_code == 200
    results = [
        ModelResult(content="not-json", provider="fake", model="fake", latency_ms=0, request_id="partial", usage={"total_tokens": 3}),
        ModelResult(content="", provider="fake", model="fake", latency_ms=0, request_id="resumed", usage={"total_tokens": 3}),
    ]
    planning_calls = 0

    def plan_with_budget_fence(*args, **kwargs):
        nonlocal planning_calls
        planning_calls += 1
        on_usage = kwargs["on_usage"]
        on_usage(results[planning_calls - 1])
        if planning_calls == 1:
            rejected = ModelProviderError("TOKEN_BUDGET_EXCEEDED")
            rejected.accounted_calls = 0
            on_usage(rejected)
            raise rejected
        return ({"chapters": [{"number": 1, "title": "档案回声", "summary": "档案回应调查", "objective": "核对旧印记", "conflict": "管理员拒绝提供原件", "scene_beats": ["检查档案"]}]}, results[1])

    monkeypatch.setattr(auto_module, "generate_plan", plan_with_budget_fence)
    assert worker.run_once() is True
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        step = db.scalar(select(AutoDirectorStep).where(
            AutoDirectorStep.run_id == run_id,
            AutoDirectorStep.stage == AutoDirectorStage.CHAPTER_WINDOW_PLANNING,
        ))
        volume_id = run.context["volume_id"]
        assert run.pause_reason == "TOKEN_BUDGET_EXCEEDED"
        assert run.total_calls == 1 and run.total_tokens == 3
        assert step and step.status.value == "RUNNING" and step.completed_at is None

    increased = client.post(f"/projects/{project_id}/volumes/{volume_id}/continue", json={"operational_token_budget": 10})
    assert increased.status_code == 200, increased.text

    assert worker.run_once() is True
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        step = db.scalar(select(AutoDirectorStep).where(
            AutoDirectorStep.run_id == run_id,
            AutoDirectorStep.stage == AutoDirectorStage.CHAPTER_WINDOW_PLANNING,
        ))
        assert run.current_stage == AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
        assert run.status.value == "RUNNING"
        assert run.total_calls == 2 and run.total_tokens == 6
        assert step and step.status.value == "COMMITTED"
        assert step.calls == 2 and step.total_tokens == 6


def test_blocked_critic_stops_without_adopting_or_advancing(author_run):
    _, sessions, project_id, run_id, provider, tick, through = author_run
    through(AutoDirectorStage.CHAPTER_EXECUTION)
    provider.critic_decision = "BLOCKED"
    assert tick() == AutoDirectorStage.QUALITY_REVIEW.value
    tick()
    with sessions() as db:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id))
        run = db.get(AutoDirectorRun, run_id)
        assert chapter.current_writer_draft_id is None
        assert run.pause_reason == "QUALITY_REVIEW_REQUIRED"
        assert run.status.value == "PAUSED"
        assert run.context["current_chapter_number"] == 1
