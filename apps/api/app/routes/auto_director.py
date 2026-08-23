from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auto_director import AutoDirectorError, AutoDirectorOrchestrator, enum_value, score_direction
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


def _payload(db: Session, run: AutoDirectorRun) -> dict:
    steps = db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id).order_by(AutoDirectorStep.created_at, AutoDirectorStep.id)).all()
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
def list_runs(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [_payload(db, run) for run in db.scalars(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id).order_by(AutoDirectorRun.created_at.desc())).all()]


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/pause")
def pause_run(project_id: str, run_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id); run.context = {**(run.context or {}), "resume_stage": enum_value(run.current_stage), "stop_requested": True}; run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = (payload or {}).get("reason", "USER_PAUSED"); run.next_action = "点击继续恢复自动导演。"; db.commit(); return _payload(db, run)


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/resume")
def resume_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id)
    if run.current_stage == AutoDirectorStage.PAUSED:
        run.current_stage = AutoDirectorStage(run.context.get("resume_stage", "DIRECTION_SELECTION" if not run.context.get("selected_direction") else "CAST_PREPARATION"))
    run.status = AutoDirectorRunStatus.RUNNING; run.context = {**(run.context or {}), "stop_requested": False}; run.pause_reason = None
    run.next_action = "已加入本地自动导演队列，等待 worker 继续。"; db.commit(); return _payload(db, run)


@router.post("/projects/{project_id}/auto-director/runs/{run_id}/retry")
def retry_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, project_id, run_id)
    if run.status == AutoDirectorRunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail={"code": "RUN_ALREADY_COMPLETED", "message": "已采用正文的运行不可重新生成。"})
    steps = db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id)).all()
    failures = sum(1 for step in steps if step.status in {AutoDirectorStepStatus.FAILED, AutoDirectorStepStatus.BLOCKED})
    if failures >= int((run.settings or {}).get("max_retries", 0) or 0):
        run.status = AutoDirectorRunStatus.BLOCKED; run.current_stage = AutoDirectorStage.BLOCKED; run.pause_reason = "MAX_RETRIES_REACHED"; run.next_action = "请接管运行或重新规划。"; db.commit(); return _payload(db, run)
    run.status = AutoDirectorRunStatus.RUNNING; run.context = {**(run.context or {}), "stop_requested": False}
    if run.current_stage == AutoDirectorStage.DIRECTION_SELECTION:
        run.context = {**(run.context or {}), "directions": [], "regenerate_nonce": datetime.utcnow().isoformat()}
        run.current_stage = AutoDirectorStage.FRAMING
        run.pause_reason = None
    if run.current_stage in {AutoDirectorStage.FAILED, AutoDirectorStage.BLOCKED}: run.current_stage = AutoDirectorStage.CHAPTER_EXECUTION if run.current_chapter_id else AutoDirectorStage.FRAMING
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
