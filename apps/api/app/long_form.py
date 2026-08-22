"""Long-form chapter progression and regression evaluation (Phase 5)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Chapter, ChapterQualityAssessment, Project, Scene, StoryPlan, StoryPlanChapter, StoryPlanStatus
from .planning import approved_plan


class LongFormWorkflowError(ValueError):
    def __init__(self, code: str, detail: Any | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is not None else {}


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


class LongFormEvaluationService:
    """Read-only view over planned chapters and formal chapter approvals."""

    def _project(self, db: Session, project_id: str) -> Project:
        project = db.get(Project, project_id)
        if not project:
            raise LongFormWorkflowError("PROJECT_NOT_FOUND")
        return project

    def _plan(self, db: Session, project_id: str) -> StoryPlan | None:
        return approved_plan(db, project_id)

    def _chapters(self, db: Session, project_id: str) -> dict[int, Chapter]:
        rows = db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True)).order_by(Chapter.number, Chapter.id)).all()
        return {row.number: row for row in rows}

    def next_chapter(self, db: Session, project_id: str) -> dict[str, Any]:
        self._project(db, project_id)
        plan = self._plan(db, project_id)
        if not plan:
            return {"ready": False, "status": "PLAN_REQUIRED", "blocked_reasons": ["APPROVED_PLAN_REQUIRED"], "task": None, "chapter": None}
        actual = self._chapters(db, project_id)
        next_number = max(actual.keys(), default=0) + 1
        task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id, StoryPlanChapter.number == next_number))
        if not task:
            return {"ready": False, "status": "PLAN_COMPLETE", "blocked_reasons": ["NO_REMAINING_PLANNED_CHAPTER"], "task": None, "chapter": None, "plan_id": plan.id, "plan_version": plan.version}
        chapter = actual.get(next_number)
        blocked: list[str] = []
        if task.locked is False:
            blocked.append("CHAPTER_TASK_NOT_LOCKED")
        if chapter:
            if chapter.quality_status == "PASS" or chapter.status == "QUALITY_APPROVED":
                blocked.append("CHAPTER_ALREADY_APPROVED")
            elif chapter.current_writer_draft_id is None:
                blocked.append("CHAPTER_SOURCE_NOT_READY")
        return {"ready": not blocked, "status": "READY" if not blocked else "BLOCKED", "blocked_reasons": blocked, "plan_id": plan.id, "plan_version": plan.version, "task": self._task(task), "chapter": self._chapter(chapter) if chapter else None}

    def evaluate(self, db: Session, project_id: str) -> dict[str, Any]:
        self._project(db, project_id)
        plan = self._plan(db, project_id)
        actual = self._chapters(db, project_id)
        if not plan:
            return {"enabled": False, "status": "PLAN_REQUIRED", "chapters": [], "summary": {"planned": 0, "started": len(actual), "approved": 0, "quality_pass_rate": 0.0}, "continuity": {"timeline_errors": [], "quality_errors": ["APPROVED_PLAN_REQUIRED"]}}
        planned = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
        statuses: list[dict[str, Any]] = []
        approved = started = blocked = 0
        finding_counts: Counter[str] = Counter()
        for task in planned:
            chapter = actual.get(task.number)
            assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True)).order_by(ChapterQualityAssessment.version.desc())) if chapter else None
            quality_status = _value(assessment.status) if assessment else None
            if chapter:
                started += 1
            if quality_status == "PASS" or (chapter and chapter.quality_status == "PASS"):
                approved += 1
            if quality_status in {"BLOCKED", "REPAIR_REQUIRED", "FAILED"}:
                blocked += 1
            if assessment:
                for code in assessment.decision_reason_codes or []:
                    finding_counts[str(code)] += 1
            statuses.append({"number": task.number, "title": task.title, "task_status": _value(task.status), "task_locked": task.locked, "chapter": self._chapter(chapter) if chapter else None, "quality_status": quality_status or (chapter.quality_status if chapter else "NOT_STARTED"), "overall_score": assessment.overall_score if assessment else None, "decision_reason_codes": assessment.decision_reason_codes if assessment else []})
        timeline_errors = self._timeline_errors(db, project_id, actual)
        quality_rate = round(approved / max(1, started), 4)
        task_completion = round(started / max(1, len(planned)), 4)
        return {"enabled": True, "status": "HEALTHY" if not timeline_errors and not blocked else "ATTENTION_REQUIRED", "plan_id": plan.id, "plan_version": plan.version, "chapters": statuses, "summary": {"planned": len(planned), "started": started, "approved": approved, "blocked": blocked, "quality_pass_rate": quality_rate, "task_progress": task_completion, "finding_reason_counts": dict(sorted(finding_counts.items()))}, "continuity": {"timeline_errors": timeline_errors, "quality_errors": sorted(finding_counts)}}

    def _timeline_errors(self, db: Session, project_id: str, actual: dict[int, Chapter]) -> list[dict[str, Any]]:
        rows = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE", Scene.status == "OCCURRED").order_by(Scene.sequence, Scene.id)).all()
        errors: list[dict[str, Any]] = []
        previous: datetime | None = None
        previous_sequence: int | None = None
        seen_sequences: set[int] = set()
        locations: dict[tuple[str, str], tuple[str, str]] = {}
        for scene in rows:
            if scene.sequence in seen_sequences or (previous_sequence is not None and scene.sequence <= previous_sequence):
                errors.append({"code": "TIMELINE_SEQUENCE_INVALID", "scene_id": scene.id, "sequence": scene.sequence})
            seen_sequences.add(scene.sequence); previous_sequence = scene.sequence
            if not scene.world_time:
                continue
            if previous is not None and scene.world_time < previous:
                errors.append({"code": "TIMELINE_ORDER_INVALID", "scene_id": scene.id, "sequence": scene.sequence})
            previous = scene.world_time
            for character_id in scene.participants or []:
                key = (str(character_id), scene.world_time.isoformat())
                prior = locations.get(key)
                if prior and prior[0] != (scene.location or ""):
                    errors.append({"code": "LOCATION_CONFLICT", "scene_id": scene.id, "prior_scene_id": prior[1], "character_id": character_id, "world_time": scene.world_time.isoformat()})
                locations[key] = (scene.location or "", scene.id)
        return errors

    def _task(self, task: StoryPlanChapter) -> dict[str, Any]:
        return {column.name: getattr(task, column.name).value if hasattr(getattr(task, column.name), "value") else getattr(task, column.name) for column in task.__table__.columns}

    def _chapter(self, chapter: Chapter | None) -> dict[str, Any] | None:
        if not chapter:
            return None
        return {"id": chapter.id, "number": chapter.number, "title": chapter.title, "status": chapter.status, "quality_status": chapter.quality_status, "word_count": chapter.word_count, "current_writer_draft_id": chapter.current_writer_draft_id, "quality_approved_at": chapter.quality_approved_at.isoformat() if chapter.quality_approved_at else None}


def evaluate_golden_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    """Run the Phase 0 corpus checks as a stable regression contract."""
    from .golden_baseline import check_chapter_coverage, check_foreshadowings, check_knowledge_uses, check_style, check_timeline, validate_corpus
    issues = validate_corpus(corpus)
    issues.extend(check_timeline(corpus.get("timeline", [])))
    issues.extend(check_foreshadowings(foreshadowings=corpus.get("foreshadowings", []), chapters=corpus.get("chapters", [])))
    issues.extend(check_style("\n".join(corpus.get("style_samples", []))))
    for chapter in corpus.get("chapters", []):
        issues.extend(check_chapter_coverage(chapter))
    for character_id, uses in (corpus.get("knowledge_uses", {}) or {}).items():
        issues.extend(check_knowledge_uses(character_id=character_id, uses=uses, knowledge_matrix=corpus.get("knowledge_matrix", {})))
    by_code = Counter(issue.code for issue in issues)
    return {"protocol": "golden-long-form-regression-v1", "passed": not issues, "issue_count": len(issues), "issue_counts": dict(sorted(by_code.items())), "issues": [issue.__dict__ for issue in issues]}
