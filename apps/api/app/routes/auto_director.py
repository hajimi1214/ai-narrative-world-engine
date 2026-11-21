from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auto_director import AutoDirectorError, AutoDirectorOrchestrator, enum_value, fingerprint, score_direction
from ..api_types import AutoDirectorRunCreatePayload, DirectionSelectionPayload
from ..models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep, AutoDirectorStepStatus, Project
from .common import get_db, require_project

router = APIRouter(tags=["auto-director"])


def _run_or_404(db: Session, project_id: str, run_id: str) -> AutoDirectorRun:
    require_project(db, project_id)
    run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="Auto director run not found")
    return run


def _retry_checkpoint(db: Session, run: AutoDirectorRun) -> AutoDirectorStep | None:
    """Resolve one failed checkpoint, never sum unrelated historical failures."""
    recorded_id = (run.context or {}).get("failed_step_id")
    recorded = db.get(AutoDirectorStep, recorded_id) if recorded_id else None
    failed_statuses = {AutoDirectorStepStatus.FAILED, AutoDirectorStepStatus.BLOCKED}
    if recorded and recorded.run_id == run.id and recorded.status in failed_statuses:
        return recorded
    if run.status not in {AutoDirectorRunStatus.FAILED, AutoDirectorRunStatus.BLOCKED}:
        return None
    # Older runs have no recorded checkpoint ID. The most recent failure is
    # their recovery point; attempts on that row are the persisted budget.
    failures = db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id, AutoDirectorStep.status.in_(failed_statuses)).order_by(AutoDirectorStep.completed_at.desc(), AutoDirectorStep.updated_at.desc(), AutoDirectorStep.created_at.desc(), AutoDirectorStep.id.desc())).all()
    stage_hint = (run.context or {}).get("worker_stage") or (run.context or {}).get("failed_stage")
    hinted = next((step for step in failures if enum_value(step.stage) == stage_hint), None)
    if hinted:
        return hinted
    # SQLite's legacy timestamps have second precision. When several old
    # failures tie, a current chapter is a stronger signal than random UUIDs.
    if run.current_chapter_id:
        chapter_step = next((step for step in failures if step.stage == AutoDirectorStage.CHAPTER_EXECUTION), None)
        if chapter_step:
            return chapter_step
    return failures[0] if failures else None


def _retry_limit_reached(run: AutoDirectorRun, checkpoint: AutoDirectorStep | None) -> bool:
    configured = (run.settings or {}).get("max_retries", 2)
    maximum = int(configured if configured is not None else 2)
    # attempt 1 is the initial generation; max_retries counts recoveries.
    return checkpoint is not None and checkpoint.attempt > maximum


def _payload(db: Session, run: AutoDirectorRun, *, step_limit: int | None = None) -> dict:
    step_query = select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id)
    if step_limit is None:
        steps = db.scalars(step_query.order_by(AutoDirectorStep.created_at, AutoDirectorStep.id)).all()
    else:
        steps = list(reversed(db.scalars(step_query.order_by(AutoDirectorStep.created_at.desc(), AutoDirectorStep.id.desc()).limit(step_limit)).all()))
    return {
        "id": run.id, "project_id": run.project_id, "status": enum_value(run.status), "current_stage": enum_value(run.current_stage),
        "current_chapter_id": run.current_chapter_id, "run_mode": run.run_mode, "pause_reason": run.pause_reason,
        "next_action": run.next_action, "idempotency_key": run.idempotency_key, "token_usage": run.token_usage or {},
        "usage_metrics": {"calls": run.total_calls, "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens, "total_tokens": run.total_tokens, "latency_ms": run.latency_ms, "provider": ",".join(run.token_usage.get("providers", [])) or None, "model": ",".join(run.token_usage.get("models", [])) or None, "estimated_cost": run.estimated_cost, "cost_status": run.cost_status},
        "settings": run.settings or {}, "context": run.context or {}, "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None, "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "steps": [{"id": s.id, "stage": enum_value(s.stage), "status": enum_value(s.status), "attempt": s.attempt, "input_fingerprint": s.input_fingerprint, "output_artifact_id": s.output_artifact_id, "output_payload": s.output_payload or {}, "error_code": s.error_code, "error_summary": s.error_summary, "token_usage": s.token_usage or {}, "usage_metrics": {"calls": s.calls, "prompt_tokens": s.prompt_tokens, "completion_tokens": s.completion_tokens, "total_tokens": s.total_tokens, "latency_ms": s.latency_ms, "provider": s.provider, "model": s.model, "estimated_cost": s.estimated_cost, "cost_status": s.cost_status}, "started_at": s.started_at.isoformat() if s.started_at else None, "completed_at": s.completed_at.isoformat() if s.completed_at else None} for s in steps],
    }


@router.post("/projects/{project_id}/auto-director/runs", status_code=status.HTTP_201_CREATED)
def create_run(project_id: str, payload: AutoDirectorRunCreatePayload, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    idempotency_key = payload.idempotency_key or f"auto-{project_id}-{fingerprint(payload.model_dump())}"
    existing = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.idempotency_key == idempotency_key))
    if existing:
        return _payload(db, existing)
    if payload.name and project.name == "未命名小说": project.name = payload.name
    if payload.inspiration: project.story_seed = payload.inspiration
    try:
        run = AutoDirectorOrchestrator().create(db, project, payload); db.commit(); db.refresh(run)
        return _payload(db, run)
    except Exception as exc:
        db.rollback()
        code = getattr(exc, "code", None) or "AUTO_DIRECTOR_CREATE_FAILED"
        raise HTTPException(status_code=502, detail={"code": code, "message": str(exc)}) from exc


@router.get("/projects/{project_id}/auto-director/runs/{run_id}")
def get_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    return _payload(db, _run_or_404(db, project_id, run_id))


@router.get("/projects/{project_id}/auto-director/runs")
def list_runs(project_id: str, limit: int = Query(default=20, ge=1, le=50), step_limit: int | None = Query(default=None, ge=1, le=100), db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [_payload(db, run, step_limit=step_limit) for run in db.scalars(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id).order_by(AutoDirectorRun.created_at.desc(), AutoDirectorRun.id.desc()).limit(limit)).all()]


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/pause")
def pause_run(project_id: str, run_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id); run.context = {**(run.context or {}), "resume_stage": enum_value(run.current_stage), "stop_requested": True}; run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = (payload or {}).get("reason", "USER_PAUSED"); run.next_action = "点击继续恢复自动导演。"; db.commit(); return _payload(db, run)


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/resume")
def resume_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id)
    if run.status == AutoDirectorRunStatus.RUNNING:
        return _payload(db, run)
    if _retry_checkpoint(db, run) is not None:
        return retry_run(project_id, run_id, db)
    if run.current_stage == AutoDirectorStage.PAUSED:
        run.current_stage = AutoDirectorStage(run.context.get("resume_stage", "DIRECTION_SELECTION" if not run.context.get("selected_direction") else "CAST_PREPARATION"))
    context = {**(run.context or {}), "stop_requested": False}
    if context.get("run_budget_reached"):
        context.update({"adopted_this_run": 0, "generated_this_run": 0, "processed_this_run": 0, "run_budget_reached": False})
    run.status = AutoDirectorRunStatus.RUNNING; run.context = context; run.pause_reason = None
    run.next_action = "已加入本地自动导演队列，等待 worker 继续。"; db.commit(); return _payload(db, run)


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/retry")
def retry_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    run = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.id == run_id, AutoDirectorRun.project_id == project_id).with_for_update())
    if not run:
        raise HTTPException(status_code=404, detail="Auto director run not found")
    if run.status == AutoDirectorRunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail={"code": "RUN_ALREADY_COMPLETED", "message": "已采用正文的运行不可重新生成。"})
    if run.status == AutoDirectorRunStatus.RUNNING:
        return _payload(db, run)
    checkpoint = _retry_checkpoint(db, run)
    retry_stage = checkpoint.stage if checkpoint else None
    if _retry_limit_reached(run, checkpoint):
        run.status = AutoDirectorRunStatus.BLOCKED; run.current_stage = AutoDirectorStage.BLOCKED; run.pause_reason = "MAX_RETRIES_REACHED"; run.next_action = "请接管运行或重新规划。"; db.commit(); return _payload(db, run)
    context = {**(run.context or {}), "stop_requested": False}
    if context.get("run_budget_reached"):
        context.update({"adopted_this_run": 0, "generated_this_run": 0, "processed_this_run": 0, "run_budget_reached": False})
    run.status = AutoDirectorRunStatus.RUNNING; run.context = context; run.pause_reason = None
    if run.current_stage == AutoDirectorStage.DIRECTION_SELECTION:
        run.context = {**(run.context or {}), "directions": [], "regenerate_nonce": datetime.utcnow().isoformat()}
        run.current_stage = AutoDirectorStage.FRAMING
        run.pause_reason = None
    if run.current_stage in {AutoDirectorStage.FAILED, AutoDirectorStage.BLOCKED}:
        run.current_stage = retry_stage or (AutoDirectorStage.CHAPTER_EXECUTION if run.current_chapter_id else AutoDirectorStage.FRAMING)
    run.next_action = "已加入本地自动导演队列，等待 worker 重试。"; db.commit(); return _payload(db, run)


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/takeover")
def takeover_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id); run.context = {**(run.context or {}), "stop_requested": True, "resume_stage": enum_value(run.current_stage)}; run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = "AUTHOR_TAKEOVER"; run.next_action = "作者已接管，可从现有检查点进入手动工作台。"; db.commit(); return _payload(db, run)


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/select-direction")
def select_direction(project_id: str, run_id: str, payload: DirectionSelectionPayload, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id)
    try:
        choices = run.context.get("directions") or []
        selected = payload.direction or (choices[payload.direction_index or 0] if 0 <= (payload.direction_index or 0) < len(choices) else None)
        if not selected: raise AutoDirectorError("DIRECTION_NOT_FOUND")
        if not score_direction(selected)[1].get("valid"):
            raise AutoDirectorError("DIRECTION_INVALID", "只能采用结构化验证通过的方向。")
        run.context = {**(run.context or {}), "selected_direction": selected, "manual_direction_override": True}
        run.status = AutoDirectorRunStatus.RUNNING; run.current_stage = AutoDirectorStage.CAST_PREPARATION; run.pause_reason = None; run.next_action = "人工方向已保存，worker 将继续。"; db.commit(); return _payload(db, run)
    except AutoDirectorError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/adopt-chapter")
def adopt_chapter(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id)
    try:
        run = AutoDirectorOrchestrator().adopt_chapter(db, run); db.commit(); return _payload(db, run)
    except Exception as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": getattr(exc, "code", "AUTHOR_ADOPTION_FAILED"), "message": str(exc)}) from exc
