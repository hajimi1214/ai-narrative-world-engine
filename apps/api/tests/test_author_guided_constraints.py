"""Author input survives planning without becoming public character knowledge."""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.author_guided_volume import AuthorGuidedVolumeService, safe_author_style
from app.db import Base
from app.models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep, Chapter, Project, StoryPlan, StoryPlanChapter


@pytest.fixture()
def author_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        project = Project(name="雾港来信", story_seed="作者的完整大纲，不是公开事实")
        db.add(project); db.flush()
        yield db, project
    engine.dispose()


def semantic_plan(count=1):
    return {"chapters": [{"number": number, "title": f"码头来信{number}", "summary": "主角核对旧信上的印记", "objective": "确认信封上的旧印记", "conflict": "守门人拒绝开门", "scene_beats": ["取出旧信比对印记", "向守门人确认寄信时间"]} for number in range(1, count + 1)]}, SimpleNamespace(provider="fake", model="fake-director", request_id="window-plan", latency_ms=3, usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7})


def setup_window(db, project, *, size=2, request=None):
    service = AuthorGuidedVolumeService()
    request = {"window_size": size, **(request or {})}
    run = AutoDirectorRun(project_id=project.id, run_mode="AUTHOR_GUIDED_VOLUME", idempotency_key="semantic-window", settings=request, context={"request": request})
    db.add(run); db.flush()
    service.advance_run(db, run)
    from app.models import BookContract, ChapterPlanningWindow, VolumeContract
    contract = db.get(BookContract, run.context["book_contract_id"])
    volume = db.get(VolumeContract, run.context["volume_id"])
    window = db.get(ChapterPlanningWindow, run.context["window_id"])
    plan = db.scalar(select(StoryPlan).where(StoryPlan.project_id == project.id).order_by(StoryPlan.version.desc()))
    return service, run, contract, volume, window, plan


def test_entry_material_and_style_are_preserved_in_author_planning(author_session):
    db, project = author_session
    service = AuthorGuidedVolumeService()
    source = "已有正文与作者注释：终局的幕后人是钟表师。"
    payload = {
        "mode": "prose", "source_material": source,
        "global_plot_direction": "逐卷追查记忆档案，最后面对幕后人。",
        "ending_direction": "终局的幕后人是钟表师。",
        "style_samples": ["他停在门槛边，先收好了手里的信。"],
        "forbidden_content": ["不得出现穿越桥段"],
        "style_contract": {"tone": "克制", "style_samples": ["先写动作，再写感受。"], "director_private_notes": "尚未发生的世界秘密"},
    }
    contract = service.ensure_contract(db, project, payload)
    volume = service.ensure_volume(db, project, contract, {"volume_goal": "找回第一封信", "core_conflict": "线人拒绝交出档案"})
    window = service.ensure_window(db, project, volume, size=2)
    service.ensure_window_tasks(db, project, volume, window, contract)
    plan = db.scalar(select(StoryPlan).where(StoryPlan.project_id == project.id))
    planning = plan.framing["author_planning_context"]
    assert planning["input_mode"] == "prose"
    assert planning["source_material"] == source
    assert planning["book"]["ending_direction"] == payload["ending_direction"]
    assert planning["book"]["global_plot_direction"] == payload["global_plot_direction"]
    assert plan.style_guide["tone"] == "克制"
    assert plan.style_guide["style_samples"] == ["先写动作，再写感受。", payload["style_samples"][0]]
    assert plan.style_guide["forbidden_content"] == ["不得出现穿越桥段"]
    assert "source_material" not in plan.style_guide
    assert "director_private_notes" not in plan.style_guide
    assert contract.style_contract["source_material"] == source


def test_window_tasks_apply_book_and_volume_constraints_without_forcing_finale(author_session):
    db, project = author_session
    service = AuthorGuidedVolumeService()
    contract = service.ensure_contract(db, project, {
        "global_required_events": ["最后一卷才揭晓幕后人"],
        "global_forbidden_events": ["主角不能突然获得超能力"],
        "forbidden_content": ["不得出现穿越桥段"],
        "style_contract": {"pov": "FIRST_PERSON"},
    })
    volume = service.ensure_volume(db, project, contract, {
        "volume_goal": "找回第一封信", "core_conflict": "线人拒绝交出档案",
        "required_events": ["核对信上的印记"], "forbidden_events": ["不得离开雾港"],
    })
    window = service.ensure_window(db, project, volume, size=2)
    task_ids = service.ensure_window_tasks(db, project, volume, window, contract)
    tasks = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.id.in_(task_ids))).all()
    assert len(tasks) == 2
    for task in tasks:
        assert "找回第一封信" in task.objective
        assert task.conflict == "线人拒绝交出档案"
        assert task.pov_mode == "FIRST_PERSON"
        assert task.must_events == ["核对信上的印记"]
        assert set(task.forbidden_events) == {"主角不能突然获得超能力", "不得出现穿越桥段", "不得离开雾港"}
        assert "最后一卷才揭晓幕后人" not in task.must_events
    # Reopening the window must reuse its existing tasks and plan.
    assert service.ensure_window_tasks(db, project, volume, window, contract) == task_ids


def test_forbidden_answers_are_references_in_public_tasks(author_session):
    db, project = author_session
    service = AuthorGuidedVolumeService()
    secret = "钟表师其实是失踪的父亲"
    fact_id = str(uuid4())
    contract = service.ensure_contract(db, project, {"ending_direction": secret, "style_samples": [f"结尾才说：{secret}", "只用具体动作写情绪。"]})
    volume = service.ensure_volume(db, project, contract, {
        "volume_goal": f"终局揭晓：{secret}",
        "forbidden_reveals": [{"id": fact_id, "description": secret}, "另一个未来答案"],
    })
    window = service.ensure_window(db, project, volume, size=1)
    task_id = service.ensure_window_tasks(db, project, volume, window, contract)[0]
    task = db.get(StoryPlanChapter, task_id)
    plan = db.get(StoryPlan, task.plan_id)
    public = {"summary": task.summary, "objective": task.objective, "conflict": task.conflict, "forbidden_reveals": task.forbidden_reveals, "style": plan.style_guide}
    assert secret not in json.dumps(public, ensure_ascii=False)
    assert "另一个未来答案" not in json.dumps(public, ensure_ascii=False)
    assert {"id": fact_id} in task.forbidden_reveals
    assert plan.style_guide["style_samples"] == ["只用具体动作写情绪。"]


def test_author_runtime_direction_does_not_promote_source_or_future_plot(author_session, monkeypatch):
    import app.auto_director as auto_module
    from app.auto_director import AutoDirectorOrchestrator

    db, project = author_session
    service = AuthorGuidedVolumeService()
    source = "作者资料中的隐藏身份不能成为人物公开知识"
    ending = "终局谜底只能在全书最后揭露"
    request = {
        "premise": source, "source_material": source, "global_plot_direction": f"先调查，最后才知道{ending}", "ending_direction": ending,
        "protagonist_contract": {"name": "林真", "desire": "找回第一封信", "secret": ending, "future_knowledge": source},
        "volume": {"volume_goal": "调查码头失踪案"}, "window_size": 1,
    }
    run = AutoDirectorRun(project_id=project.id, run_mode="AUTHOR_GUIDED_VOLUME", idempotency_key="safe-author-direction", settings=request, context={"request": request, "execute_window": True})
    db.add(run); db.flush()
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: semantic_plan())
    monkeypatch.setattr(AutoDirectorOrchestrator, "advance_to_pause", lambda self, session, target: target)
    service.advance_run(db, run)
    direction = run.context["selected_direction"]
    encoded = json.dumps(direction, ensure_ascii=False)
    assert source not in encoded and ending not in encoded
    assert direction["premise"] == "调查码头失踪案"
    assert direction["protagonist"] == {"name": "林真", "desire": "找回第一封信"}
    assert run.status == AutoDirectorRunStatus.RUNNING


def test_style_projection_does_not_copy_arbitrary_plot_fields():
    safe = safe_author_style({"tone": "冷峻", "style_samples": ["风从门缝进来。"], "ending_direction": "后续答案", "source_material": "私人资料", "global_plot_direction": "未公开的主线", "secret": "身份"})
    assert safe["tone"] == "冷峻"
    assert safe["style_samples"] == ["风从门缝进来。"]
    assert not {"ending_direction", "source_material", "global_plot_direction", "secret"}.intersection(safe)


def test_worker_plans_only_after_continue_and_reuses_exact_window_checkpoint(author_session, monkeypatch):
    import app.auto_director as auto_module

    db, project = author_session
    calls = []
    def generate(session, target, framing, premise, style, anti_ai, **kwargs):
        calls.append(framing)
        assert framing["author_planning_context"]["source_material"] == "旧信由雾港的钟表师寄出"
        assert style["style_samples"] == ["只用具体动作推进。"]
        return semantic_plan(framing["target_chapters"])
    monkeypatch.setattr(auto_module, "generate_plan", generate)
    service, run, contract, volume, window, plan = setup_window(db, project, request={"mode": "prose", "source_material": "旧信由雾港的钟表师寄出", "style_samples": ["只用具体动作推进。"]})
    assert calls == [] and run.status == AutoDirectorRunStatus.PAUSED
    run.context = {**run.context, "execute_window": True}; run.status = AutoDirectorRunStatus.RUNNING
    service.advance_run(db, run)
    assert len(calls) == 1
    assert run.token_usage["total_tokens"] == 7
    assert run.current_stage == AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
    tasks = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
    assert [task.title for task in tasks] == ["码头来信1", "码头来信2"]
    seen = []
    def inspect_checkpoint(self, session, target):
        framing = dict(target.context["request"])
        framing.update({"target_chapters": int(framing.get("target_chapters") or 10), "target_words_per_chapter": int(framing.get("target_words_per_chapter") or project.target_chapter_words), "pov": framing.get("pov") or "THIRD_PERSON_LIMITED"})
        step = self._step(session, target, AutoDirectorStage.CHAPTER_PLANNING, {"direction": target.context["selected_direction"], "framing": framing})
        assert step.status.value == "COMMITTED"
        assert step.output_payload["plan_id"] == plan.id
        seen.append(step.id)
        return target
    monkeypatch.setattr(auto_module.AutoDirectorOrchestrator, "advance_to_pause", inspect_checkpoint)
    service.advance_run(db, run)
    service.advance_run(db, run)
    assert len(calls) == 1 and len(set(seen)) == 1
    assert run.token_usage["total_tokens"] == 7


def test_semantic_planning_maps_later_window_and_preserves_written_or_locked_tasks(author_session, monkeypatch):
    import app.auto_director as auto_module
    from app.models import BookContract

    db, project = author_session
    db.add(Chapter(project_id=project.id, number=600, content="上一卷的正式正文", active=True)); db.flush()
    service, run, contract, volume, window, plan = setup_window(db, project, size=3)
    tasks = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
    tasks[0].locked = True
    locked_title, written_title = tasks[0].title, tasks[1].title
    db.add(Chapter(project_id=project.id, number=602, content="已经采用的正文", active=True)); db.flush()
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: semantic_plan(3))
    assert service.refine_window_plan(db, run, project, contract, volume, window, plan) == "PLANNED"
    assert [task.number for task in tasks] == [601, 602, 603]
    assert tasks[0].title == locked_title and tasks[1].title == written_title
    assert tasks[2].title == "码头来信3"
    assert plan.generation_report["semantic_planning"]["updated_chapters"] == [603]
    assert len(db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project.id)).all()) == 3


@pytest.mark.parametrize("bad_numbers", [[1], [1, 99], [1, 1]])
def test_invalid_window_plan_is_visible_and_retry_does_not_change_placeholder_history(author_session, monkeypatch, bad_numbers):
    import app.auto_director as auto_module

    db, project = author_session
    service, run, contract, volume, window, plan = setup_window(db, project)
    tasks = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
    original = [task.title for task in tasks]
    bad, result = semantic_plan(2)
    bad["chapters"] = [{**bad["chapters"][0], "number": number} for number in bad_numbers]
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: (bad, result))
    assert service.refine_window_plan(db, run, project, contract, volume, window, plan) == "FAILED"
    assert run.status == AutoDirectorRunStatus.FAILED and window.error_code
    assert [task.title for task in tasks] == original
    failed_step_id = run.context["failed_step_id"]
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: semantic_plan(2))
    run.status = AutoDirectorRunStatus.RUNNING
    assert service.refine_window_plan(db, run, project, contract, volume, window, plan) == "PLANNED"
    step = db.get(AutoDirectorStep, failed_step_id)
    assert step.attempt == 2 and step.status.value == "COMMITTED"
    assert run.token_usage["total_tokens"] == 14 and step.total_tokens == 14
    assert window.error_code is None


def test_semantic_planner_respects_budget_before_call_and_rejects_future_answer(author_session, monkeypatch):
    import app.auto_director as auto_module

    db, project = author_session
    service, run, contract, volume, window, plan = setup_window(db, project, size=1, request={"ending_direction": "钟表师就是最终的幕后人", "operational_token_budget": 1})
    calls = []
    def generated(*args, **kwargs):
        calls.append(True)
        data, result = semantic_plan()
        data["chapters"][0]["objective"] = "钟表师就是最终的幕后人"
        return data, result
    monkeypatch.setattr(auto_module, "generate_plan", generated)
    run.token_usage = {"total_tokens": 1}
    assert service.refine_window_plan(db, run, project, contract, volume, window, plan) == "FAILED"
    assert run.status == AutoDirectorRunStatus.PAUSED and run.pause_reason == "TOKEN_BUDGET_EXCEEDED" and calls == []
    run.token_usage = {}; run.settings = {**run.settings, "max_tokens": 0}; run.status = AutoDirectorRunStatus.RUNNING
    assert service.refine_window_plan(db, run, project, contract, volume, window, plan) == "FAILED"
    assert window.error_code == "AUTHOR_WINDOW_PLAN_REVEAL_LEAK"
    assert len(calls) == 1


def test_preplanned_followup_does_not_skip_remaining_current_window_chapters(author_session, monkeypatch):
    import app.auto_director as auto_module
    from app.models import ChapterPlanningWindow, StoryPlanStatus
    from app.planning import chapter_task_context

    db, project = author_session
    service, run, contract, volume, window, plan = setup_window(db, project, size=5)
    monkeypatch.setattr(auto_module, "generate_plan", lambda *args, **kwargs: semantic_plan(5))
    run.context = {**run.context, "execute_window": True}; run.status = AutoDirectorRunStatus.RUNNING
    service.advance_run(db, run)  # Save semantic planning before runtime.
    def adopt_three(self, session, target):
        target.context = {**target.context, "last_adopted_chapter_number": 3, "current_chapter_number": 4}
        return target
    monkeypatch.setattr(auto_module.AutoDirectorOrchestrator, "advance_to_pause", adopt_three)
    service.advance_run(db, run)
    followup = db.get(ChapterPlanningWindow, run.context["next_window_id"])
    assert followup.start_chapter_number == 6 and followup.status.value == "PLANNING"
    assert chapter_task_context(db, project.id, 4, required=True)["plan_id"] == plan.id
    future_plan = db.scalar(select(StoryPlan).where(StoryPlan.project_id == project.id, StoryPlan.id != plan.id))
    assert future_plan.status == StoryPlanStatus.GENERATED
    service.advance_run(db, run)
    assert run.context["window_id"] == window.id
    assert run.context["current_chapter_number"] == 4
    assert chapter_task_context(db, project.id, 5, required=True)["plan_id"] == plan.id
