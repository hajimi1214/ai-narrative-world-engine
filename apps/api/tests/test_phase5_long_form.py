from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.golden_baseline import GOLDEN_STYLE_RULES
from app.long_form import LongFormEvaluationService, evaluate_golden_corpus
from app.models import Chapter, Project, Scene, SceneStatus, StoryPlan, StoryPlanChapter, StoryPlanStatus


def test_phase5_next_chapter_requires_locked_task_and_reports_progress():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        project = Project(name="long form")
        db.add(project); db.flush()
        plan = StoryPlan(project_id=project.id, version=1, status=StoryPlanStatus.APPROVED, framing={"target_chapters": 2}, premise="seed", macro_plan={}, style_guide={}, anti_ai_rules={}, source_fingerprint="plan")
        db.add(plan); db.flush()
        task = StoryPlanChapter(project_id=project.id, plan_id=plan.id, number=1, volume_number=1, arc_number=1, title="开端", summary="建立冲突", objective="接受委托", conflict="代价不明", start_state={}, end_state={"case": "open"}, must_events=["接案"], forbidden_events=[], allowed_reveals=[], forbidden_reveals=[], foreshadow_create=[], foreshadow_payoff=[], character_changes=[], consequences=[], scene_beats=[], target_words=3000, pace="MEDIUM", locked=False)
        db.add(task); db.commit()
        service = LongFormEvaluationService()
        assert service.next_chapter(db, project.id)["blocked_reasons"] == ["CHAPTER_TASK_NOT_LOCKED"]
        task.locked = True; db.commit()
        result = service.next_chapter(db, project.id)
        assert result["ready"] is True and result["task"]["number"] == 1
        evaluation = service.evaluate(db, project.id)
        assert evaluation["summary"]["planned"] == 1
        assert evaluation["summary"]["started"] == 0


def test_phase5_evaluation_detects_formal_history_timeline_regression():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        project = Project(name="timeline")
        db.add(project); db.flush()
        plan = StoryPlan(project_id=project.id, version=1, status=StoryPlanStatus.APPROVED, framing={}, premise="seed", macro_plan={}, style_guide={}, anti_ai_rules={}, source_fingerprint="plan")
        db.add(plan); db.flush()
        db.add(StoryPlanChapter(project_id=project.id, plan_id=plan.id, number=1, volume_number=1, arc_number=1, title="一", summary="一", objective="一", conflict="一", start_state={}, end_state={}, must_events=[], forbidden_events=[], allowed_reveals=[], forbidden_reveals=[], foreshadow_create=[], foreshadow_payoff=[], character_changes=[], consequences=[], scene_beats=[], target_words=3000, pace="MEDIUM", locked=True))
        db.add_all([Scene(project_id=project.id, sequence=1, world_time=datetime(2041, 1, 2), status=SceneStatus.OCCURRED, history_status="ACTIVE"), Scene(project_id=project.id, sequence=2, world_time=datetime(2041, 1, 1), status=SceneStatus.OCCURRED, history_status="ACTIVE")]); db.commit()
        result = LongFormEvaluationService().evaluate(db, project.id)
        assert result["status"] == "ATTENTION_REQUIRED"
        assert result["continuity"]["timeline_errors"]


def test_phase5_golden_regression_returns_machine_readable_summary():
    corpus = {"book": {}, "characters": [{"id": "c"}] * 10, "canon": [{"id": f"f{i}"} for i in range(20)], "threads": [{"id": f"t{i}"} for i in range(10)], "arcs": [], "chapters": [], "timeline": [], "foreshadowings": [], "knowledge_matrix": {}, "style_samples": []}
    report = evaluate_golden_corpus(corpus)
    assert report["protocol"] == "golden-long-form-regression-v1"
    assert report["passed"] is False
    assert isinstance(report["issue_counts"], dict)

