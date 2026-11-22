from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api_types import AuthorCharacterPayload, AuthorGuidancePayload, AuthorGuidedRunCreatePayload, BookAutomationSettingsPayload, PlotDirectionPayload, VolumeActionPayload, VolumeContinuePayload, VolumeContractUpdatePayload
from ..author_guided_volume import AuthorGuidedVolumeError, AuthorGuidedVolumeService, _auto_next_volume_missing_fields, _contract_payload, _enum, _volume_payload, _window_payload
from ..models import AuthorGuidance, AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep, BookCompletionProposal, BookCompletionProposalStatus, BookContract, ChapterPlanningWindow, ChapterWindowStatus, Character, ForeshadowingLedger, ForeshadowingStatus, Project, VolumeContract, VolumeContractStatus, VolumeContinuitySnapshot
from ..author_recovery import recovery_payload, revoke_claim
from .common import get_db, require_project
from .auto_director import _retry_checkpoint, _retry_limit_reached

router = APIRouter(tags=["author-guided-volume"])


def _run_payload(db: Session, run: AutoDirectorRun) -> dict:
    steps = db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id)).all()
    def metrics(rows):
        return {"calls": sum(item.calls or 0 for item in rows), "prompt_tokens": sum(item.prompt_tokens or 0 for item in rows), "completion_tokens": sum(item.completion_tokens or 0 for item in rows), "total_tokens": sum(item.total_tokens or 0 for item in rows), "latency_ms": sum(item.latency_ms or 0 for item in rows), "estimated_cost": None, "cost_status": "UNKNOWN"}
    context = run.context or {}
    current_chapter = context.get("last_adopted_chapter_number") or context.get("current_chapter_number")
    chapter_rows = [item for item in steps if current_chapter is not None and (item.output_payload or {}).get("chapter_number") == current_chapter]
    window_id = context.get("window_id")
    volume_id = context.get("volume_id")
    window_rows = [item for item in steps if (item.output_payload or {}).get("window_id") == window_id]
    volume_rows = [item for item in steps if (item.output_payload or {}).get("volume_id") == volume_id]
    usage_summary = {"chapter": metrics(chapter_rows), "window": metrics(window_rows), "volume": metrics(volume_rows), "book": metrics(steps)}
    contract = db.get(BookContract, context.get("book_contract_id")) if context.get("book_contract_id") else None
    policy = contract.length_policy if contract else {}
    auto_cross = (policy or {}).get("auto_cross_volume")
    if auto_cross is None:
        auto_cross = (policy or {}).get("continuous_volume_progression", False)
    return {"id": run.id, "project_id": run.project_id, "status": _enum(run.status), "run_mode": run.run_mode, "current_stage": _enum(run.current_stage), "pause_reason": run.pause_reason, "next_action": run.next_action, "idempotency_key": run.idempotency_key, "context": run.context or {}, "settings": run.settings or {}, "book_policy": {"auto_cross_volume": bool(auto_cross)}, "token_usage": run.token_usage or {}, "usage_summary": usage_summary, "recovery": recovery_payload(run)}


def _source_volume_snapshot_payload(db: Session, volume: VolumeContract) -> dict | None:
    snapshot_id = (volume.opening_state or {}).get("source_volume_snapshot_id")
    if not snapshot_id:
        return None
    snapshot = db.get(VolumeContinuitySnapshot, snapshot_id)
    source_volume = db.get(VolumeContract, snapshot.volume_id) if snapshot else None
    if (not snapshot or snapshot.project_id != volume.project_id or not source_volume
            or source_volume.project_id != volume.project_id
            or source_volume.volume_number + 1 != volume.volume_number
            or source_volume.status != VolumeContractStatus.SEALED
            or source_volume.sealed_snapshot_id != snapshot.id):
        return {"id": snapshot_id, "available": False}
    timeline = snapshot.timeline_end or {}
    return {
        "id": snapshot.id,
        "available": True,
        "volume_id": snapshot.volume_id,
        "version": snapshot.snapshot_version,
        "summary": snapshot.summary,
        "source_fingerprint": snapshot.source_fingerprint,
        "formal_world_fingerprint": timeline.get("formal_world_fingerprint"),
        "chapter_start": timeline.get("chapter_start"),
        "chapter_end": timeline.get("chapter_end"),
        "adopted_chapter_count": len(timeline.get("adopted_chapters") or []),
        "confirmed_fact_count": len(snapshot.confirmed_facts or []),
        "character_count": len(snapshot.character_states or {}),
        "active_thread_count": len(snapshot.active_threads or []),
        "next_volume_hooks": snapshot.next_volume_hooks or [],
    }


def _volume_or_404(db: Session, project_id: str, volume_id: str) -> VolumeContract:
    require_project(db, project_id)
    volume = db.get(VolumeContract, volume_id)
    if not volume or volume.project_id != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")
    return volume


def _ensure_auto_cross_volume_uses_original_run(volume: VolumeContract) -> None:
    checkpoint = (volume.opening_state or {}).get("auto_cross_volume_checkpoint") or {}
    if checkpoint.get("run_id"):
        raise HTTPException(status_code=409, detail={
            "code": "AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED",
            "message": "请从原自动运行检查点继续，以保留当前运行与预算。",
            "run_id": checkpoint.get("run_id"),
        })


def _wake_volume_run(db: Session, project_id: str, volume_id: str) -> AutoDirectorRun | None:
    """Find only a run owned by this volume; never wake a sibling volume."""
    return db.scalar(select(AutoDirectorRun).where(
        AutoDirectorRun.project_id == project_id,
        AutoDirectorRun.run_mode == "AUTHOR_GUIDED_VOLUME",
        AutoDirectorRun.status.in_([
            AutoDirectorRunStatus.CREATED, AutoDirectorRunStatus.RUNNING,
            AutoDirectorRunStatus.PAUSED, AutoDirectorRunStatus.FAILED,
            AutoDirectorRunStatus.BLOCKED,
        ]),
        AutoDirectorRun.context["volume_id"].as_string() == volume_id,
    ).order_by(AutoDirectorRun.updated_at.desc(), AutoDirectorRun.id.desc()).with_for_update())


def _resume_volume_run(db: Session, project_id: str, volume_id: str) -> AutoDirectorRun | None:
    """Apply the shared checkpoint retry policy before changing volume/window state."""
    run = _wake_volume_run(db, project_id, volume_id)
    if not run or run.status == AutoDirectorRunStatus.RUNNING:
        return run

    checkpoint = _retry_checkpoint(db, run)
    if run.status == AutoDirectorRunStatus.BLOCKED and checkpoint is None:
        return run
    if checkpoint is not None or run.status in {AutoDirectorRunStatus.FAILED, AutoDirectorRunStatus.BLOCKED}:
        retry_author_guided_run(project_id, run.id, db)
    elif run.status == AutoDirectorRunStatus.PAUSED:
        # Deliberate author pauses and safe budget pauses have no failed
        # checkpoint; resume without spending a recovery attempt.
        revoke_claim(run)
        context = dict(run.context or {})
        if run.pause_reason in {"WINDOW_COMPLETE", "RUN_CHAPTER_BUDGET_REACHED"}:
            context["adopted_this_run"] = 0
            context["generated_this_run"] = 0
            context["processed_this_run"] = 0
            context["run_budget_reached"] = False
        resume_stage = context.get("resume_stage")
        if run.current_stage == AutoDirectorStage.PAUSED and resume_stage:
            try:
                run.current_stage = AutoDirectorStage(resume_stage)
            except ValueError:
                run.current_stage = AutoDirectorStage.VOLUME_ACTIVE
        run.context = {**context, "execute_window": True, "stop_requested": False}
        run.status = AutoDirectorRunStatus.RUNNING
        run.pause_reason = None
        run.next_action = "正在继续当前卷窗口。"
    db.flush()
    return run


def _run_for_volume(db: Session, project_id: str, volume: VolumeContract) -> AutoDirectorRun | None:
    """Create an idempotent worker run when an author starts a later volume."""
    run = _wake_volume_run(db, project_id, volume.id)
    if run:
        return run
    contract = db.get(BookContract, volume.book_contract_id) if volume.book_contract_id else None
    if not contract:
        return None
    if contract.status == "COMPLETED":
        raise HTTPException(status_code=409, detail={"code": "BOOK_ALREADY_COMPLETED"})
    source = db.scalar(select(AutoDirectorRun).where(
        AutoDirectorRun.project_id == project_id,
        AutoDirectorRun.run_mode == "AUTHOR_GUIDED_VOLUME",
        AutoDirectorRun.context["book_contract_id"].as_string() == contract.id,
    ).order_by(AutoDirectorRun.updated_at.desc(), AutoDirectorRun.id.desc()))
    if not source:
        return None
    key = f"author-volume-{contract.id}-volume-{volume.volume_number}"
    existing = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.idempotency_key == key))
    if existing:
        if existing.status == AutoDirectorRunStatus.COMPLETED:
            raise HTTPException(status_code=409, detail={"code": "BOOK_ALREADY_COMPLETED"})
        return existing
    context = dict(source.context or {})
    request = dict(context.get("request") or source.settings or {})
    active_window = db.scalar(select(ChapterPlanningWindow).where(
        ChapterPlanningWindow.project_id == project_id,
        ChapterPlanningWindow.volume_id == volume.id,
        ChapterPlanningWindow.status.in_([ChapterWindowStatus.PLANNING, ChapterWindowStatus.ACTIVE, ChapterWindowStatus.EXTENDING]),
    ).order_by(ChapterPlanningWindow.created_at.desc()))
    run = AutoDirectorRun(
        project_id=project_id,
        idempotency_key=key,
        status=AutoDirectorRunStatus.RUNNING,
        current_stage=AutoDirectorStage.VOLUME_ACTIVE,
        run_mode="AUTHOR_GUIDED_VOLUME",
        settings=dict(source.settings or {}),
        context={
            "request": request,
            "book_contract_id": contract.id,
            "current_volume_number": volume.volume_number,
            "volume_id": volume.id,
            "window_id": active_window.id if active_window else None,
            "current_chapter_number": active_window.start_chapter_number if active_window else None,
            "execute_window": True,
        },
    )
    db.add(run)
    db.flush()
    return run


def _ensure_volume_run_can_resume(run: AutoDirectorRun | None) -> None:
    """Surface a blocked checkpoint before activating its volume or window."""
    if run and run.status == AutoDirectorRunStatus.BLOCKED:
        code = run.pause_reason or "AUTHOR_RUN_BLOCKED"
        raise HTTPException(status_code=409, detail={
            "code": code,
            "message": run.next_action or "当前失败检查点已阻塞；请接管处理后再继续当前卷。",
            "run_id": run.id,
        })


def _apply_token_budget_increase(db: Session, run: AutoDirectorRun | None, payload: VolumeContinuePayload | None) -> None:
    if not run or run.pause_reason != "TOKEN_BUDGET_EXCEEDED":
        return
    requested = payload.operational_token_budget if payload else None
    if requested is None:
        raise HTTPException(status_code=409, detail={
            "code": "TOKEN_BUDGET_INCREASE_REQUIRED",
            "message": "本次 token 预算已耗尽。请填写更高的新预算后再继续；运行会从已保存检查点恢复。",
            "used_tokens": max(int(run.total_tokens or 0), int((run.token_usage or {}).get("total_tokens", 0) or 0)),
            "current_budget": int((run.settings or {}).get("operational_token_budget") or (run.settings or {}).get("max_tokens") or 0),
        })
    used = max(int(run.total_tokens or 0), int((run.token_usage or {}).get("total_tokens", 0) or 0))
    settings = dict(run.settings or {})
    current_budget = int(settings.get("operational_token_budget") or settings.get("max_tokens") or 0)
    if requested <= max(used, current_budget):
        raise HTTPException(status_code=422, detail={
            "code": "TOKEN_BUDGET_MUST_INCREASE",
            "message": "新 token 预算必须高于当前预算和已用 token 数。",
            "used_tokens": used,
            "current_budget": current_budget,
        })
    settings["operational_token_budget"] = requested
    settings["max_tokens"] = requested
    run.settings = settings
    context = dict(run.context or {})
    request = dict(context.get("request") or {})
    request["operational_token_budget"] = requested
    policy = dict(request.get("length_policy") or {})
    policy["operational_token_budget"] = requested
    request["length_policy"] = policy
    context["request"] = request
    run.context = context
    run.next_action = f"Token 预算已提高到 {requested:,}；正在从已保存检查点继续。"


@router.post("/projects/{project_id}/author-guided-volume/runs", status_code=status.HTTP_201_CREATED)
def create_author_guided_run(project_id: str, payload: AuthorGuidedRunCreatePayload, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    data = payload.model_dump()
    key = payload.idempotency_key or f"author-volume-{project_id}"
    existing = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.idempotency_key == key))
    if existing:
        return _run_payload(db, existing)
    contract = AuthorGuidedVolumeService().ensure_contract(db, project, data)
    volume = AuthorGuidedVolumeService().ensure_volume(db, project, contract, data.get("volume") or {})
    auto_cross_volume = bool(data.get("auto_cross_volume") or (data.get("length_policy") or {}).get("auto_cross_volume") or data.get("continuous_volume_progression") or (data.get("length_policy") or {}).get("continuous_volume_progression"))
    run = AutoDirectorRun(project_id=project_id, idempotency_key=key, status=AutoDirectorRunStatus.RUNNING, current_stage=AutoDirectorStage.BOOK_CONTRACT, run_mode="AUTHOR_GUIDED_VOLUME", settings=data, context={"request": data, "book_contract_id": contract.id, "volume_id": volume.id, "current_volume_number": volume.volume_number, "execute_window": payload.continuous_chapter_progression or auto_cross_volume})
    db.add(run); db.commit(); db.refresh(run)
    return _run_payload(db, run)


@router.patch("/projects/{project_id}/book-contract/automation")
def update_book_automation_settings(project_id: str, payload: BookAutomationSettingsPayload, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    contract = db.scalar(select(BookContract).where(BookContract.project_id == project.id).with_for_update())
    if not contract:
        raise HTTPException(status_code=404, detail={"code": "BOOK_CONTRACT_NOT_FOUND"})
    contract = AuthorGuidedVolumeService().set_auto_cross_volume(db, contract, payload.auto_cross_volume)
    db.commit()
    return {"contract": _contract_payload(contract), "auto_cross_volume": bool((contract.length_policy or {}).get("auto_cross_volume", False))}


@router.get("/projects/{project_id}/author-guided-volume/runs/{run_id}")
def get_author_guided_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id); run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME": raise HTTPException(status_code=404, detail="Author-guided run not found")
    return _run_payload(db, run)


@router.get("/projects/{project_id}/author-guided-volume/runs")
def list_author_guided_runs(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    runs = db.scalars(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.run_mode == "AUTHOR_GUIDED_VOLUME").order_by(AutoDirectorRun.updated_at.desc())).all()
    return [_run_payload(db, run) for run in runs]


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/continue")
def continue_author_guided_run(project_id: str, run_id: str, payload: VolumeContinuePayload | None = None, db: Session = Depends(get_db)):
    require_project(db, project_id); run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME": raise HTTPException(status_code=404, detail="Author-guided run not found")
    if run.status == AutoDirectorRunStatus.RUNNING:
        return _run_payload(db, run)
    revoke_claim(run)
    if run.pause_reason == "AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED":
        checkpoint = dict((run.context or {}).get("auto_cross_volume_checkpoint") or {})
        next_volume = db.get(VolumeContract, checkpoint.get("next_volume_id")) if checkpoint.get("next_volume_id") else None
        source_volume = db.get(VolumeContract, checkpoint.get("source_volume_id")) if checkpoint.get("source_volume_id") else None
        source_snapshot = db.get(VolumeContinuitySnapshot, checkpoint.get("source_snapshot_id")) if checkpoint.get("source_snapshot_id") else None
        if (not checkpoint or checkpoint.get("run_id") != run.id or not next_volume
                or next_volume.project_id != project_id or not source_volume
                or source_volume.project_id != project_id or not source_snapshot
                or source_snapshot.project_id != project_id or source_snapshot.volume_id != source_volume.id
                or source_volume.status != VolumeContractStatus.SEALED
                or source_volume.sealed_snapshot_id != source_snapshot.id
                or (next_volume.opening_state or {}).get("source_volume_snapshot_id") != source_snapshot.id):
            raise HTTPException(status_code=409, detail={"code": "VOLUME_NEXT_SOURCE_SNAPSHOT_STALE", "message": "承接快照与原运行检查点不一致，无法恢复自动跨卷。"})
        missing_fields = _auto_next_volume_missing_fields(next_volume)
        if missing_fields:
            raise HTTPException(status_code=409, detail={
                "code": "AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED",
                "message": "请补齐下一卷目标、核心冲突和结束状态后继续。",
                "missing_fields": missing_fields,
                "volume_id": next_volume.id,
            })
        run.status = AutoDirectorRunStatus.RUNNING
        run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
        run.pause_reason = None
        run.next_action = f"下一卷规划已补齐，正在从第 {source_volume.volume_number} 卷封存检查点继续。"
        run.context = {**(run.context or {}), "execute_window": True, "stop_requested": False}
        db.commit()
        return _run_payload(db, run)
    if _retry_checkpoint(db, run) is not None:
        return retry_author_guided_run(project_id, run_id, db)
    _apply_token_budget_increase(db, run, payload)
    context = dict(run.context or {})
    if run.status == AutoDirectorRunStatus.PAUSED and run.pause_reason in {"WINDOW_COMPLETE", "RUN_CHAPTER_BUDGET_REACHED"}:
        context["adopted_this_run"] = 0
        context["generated_this_run"] = 0
        context["processed_this_run"] = 0
        context["run_budget_reached"] = False
    run.status = AutoDirectorRunStatus.RUNNING; run.current_stage = AutoDirectorStage.VOLUME_ACTIVE; run.pause_reason = None; run.next_action = "正在继续当前卷窗口。"
    run.context = {**context, "execute_window": True, "stop_requested": False}
    db.commit(); return _run_payload(db, run)


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/retry")
def retry_author_guided_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    run = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.id == run_id, AutoDirectorRun.project_id == project_id).with_for_update())
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME":
        raise HTTPException(status_code=404, detail="Author-guided run not found")
    if run.status == AutoDirectorRunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail={"code": "RUN_ALREADY_COMPLETED"})
    if run.pause_reason == "TOKEN_BUDGET_EXCEEDED":
        raise HTTPException(status_code=409, detail={"code": "TOKEN_BUDGET_INCREASE_REQUIRED", "message": "Token 预算已用尽；请在卷导演页提高预算后继续。"})
    if run.pause_reason == "WINDOW_COMPLETE" and (run.context or {}).get("window_complete"):
        return _run_payload(db, run)
    if run.status == AutoDirectorRunStatus.RUNNING:
        return _run_payload(db, run)
    checkpoint = _retry_checkpoint(db, run)
    if _retry_limit_reached(run, checkpoint):
        run.status = AutoDirectorRunStatus.BLOCKED; run.current_stage = AutoDirectorStage.BLOCKED; run.pause_reason = "MAX_RETRIES_REACHED"; run.next_action = "请接管运行或重新规划。"
    else:
        context = dict(run.context or {})
        if run.pause_reason == "RUN_CHAPTER_BUDGET_REACHED":
            context["adopted_this_run"] = 0
            context["generated_this_run"] = 0
            context["processed_this_run"] = 0
            context["run_budget_reached"] = False
        run.status = AutoDirectorRunStatus.RUNNING; run.context = {**context, "execute_window": True, "stop_requested": False}; run.pause_reason = None; run.next_action = "已加入作者卷级队列，等待 worker 重试。"
        if checkpoint:
            run.current_stage = checkpoint.stage
    db.commit(); return _run_payload(db, run)


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/takeover")
def takeover_author_guided_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME":
        raise HTTPException(status_code=404, detail="Author-guided run not found")
    revoke_claim(run)
    run.context = {**(run.context or {}), "stop_requested": True, "resume_stage": _enum(run.current_stage)}
    run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = "AUTHOR_TAKEOVER"; run.next_action = "作者已接管，可从现有检查点进入手动工作台。"
    db.commit(); return _run_payload(db, run)


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/pause")
def pause_author_guided_run(project_id: str, run_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    require_project(db, project_id); run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME": raise HTTPException(status_code=404, detail="Author-guided run not found")
    revoke_claim(run)
    run.context = {**(run.context or {}), "resume_stage": _enum(run.current_stage), "stop_requested": True}
    run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = payload.reason or "AUTHOR_PAUSED"; run.next_action = "作者可以继续当前卷或接管修改。"
    db.commit(); return _run_payload(db, run)


@router.get("/projects/{project_id}/volumes")
def list_volumes(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    service = AuthorGuidedVolumeService()
    volumes = db.scalars(select(VolumeContract).where(VolumeContract.project_id == project_id).order_by(VolumeContract.volume_number)).all()
    return [
        {
            **_volume_payload(volume),
            "source_snapshot": _source_volume_snapshot_payload(db, volume),
            "progress": service.progress(db, volume),
            "windows": [
                _window_payload(item)
                for item in db.scalars(
                    select(ChapterPlanningWindow)
                    .where(ChapterPlanningWindow.volume_id == volume.id)
                    .order_by(ChapterPlanningWindow.created_at)
                ).all()
            ],
        }
        for volume in volumes
    ]


@router.post("/projects/{project_id}/volumes/{volume_id}/next")
def prepare_next_volume(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    current = _volume_or_404(db, project_id, volume_id)
    if current.status != VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "CURRENT_VOLUME_NOT_SEALED"})
    successor = db.scalar(select(VolumeContract).where(
        VolumeContract.project_id == project_id,
        VolumeContract.volume_number == current.volume_number + 1,
    ))
    if successor:
        _ensure_auto_cross_volume_uses_original_run(successor)
    contract = db.get(BookContract, current.book_contract_id); project = db.get(Project, project_id)
    next_start = (current.actual_chapter_end or current.estimated_chapter_end or current.actual_chapter_start or 0) + 1
    next_volume = AuthorGuidedVolumeService().ensure_volume(db, project, contract, {"title": f"第 {current.volume_number + 1} 卷", "estimated_chapter_start": next_start, "opening_state": {"source_volume_snapshot_id": current.sealed_snapshot_id}}, current.volume_number + 1)
    if next_volume.estimated_chapter_start is None:
        next_volume.estimated_chapter_start = next_start
    window = AuthorGuidedVolumeService().ensure_window(db, project, next_volume)
    db.commit(); return {"volume": _volume_payload(next_volume), "window": _window_payload(window), "source_snapshot_id": current.sealed_snapshot_id}


@router.post("/projects/{project_id}/volumes/{volume_id}/start")
def start_volume(project_id: str, volume_id: str, payload: VolumeContinuePayload | None = None, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    _ensure_auto_cross_volume_uses_original_run(volume)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    _apply_token_budget_increase(db, _wake_volume_run(db, project_id, volume.id), payload)
    run = _resume_volume_run(db, project_id, volume.id) or _run_for_volume(db, project_id, volume)
    _ensure_volume_run_can_resume(run)
    volume.status = VolumeContractStatus.ACTIVE
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window)}


@router.post("/projects/{project_id}/volumes/{volume_id}/continue")
def continue_volume(project_id: str, volume_id: str, payload: VolumeContinuePayload | None = None, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    _ensure_auto_cross_volume_uses_original_run(volume)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    run = _wake_volume_run(db, project_id, volume.id) or _run_for_volume(db, project_id, volume)
    _apply_token_budget_increase(db, run, payload)
    run = _resume_volume_run(db, project_id, volume.id) or _run_for_volume(db, project_id, volume)
    _ensure_volume_run_can_resume(run)
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume, author_note=payload.author_note if payload else None)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window)}


@router.post("/projects/{project_id}/volumes/{volume_id}/extend")
def extend_volume(project_id: str, volume_id: str, payload: VolumeContinuePayload | None = None, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    _ensure_auto_cross_volume_uses_original_run(volume)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    _apply_token_budget_increase(db, _wake_volume_run(db, project_id, volume.id), payload)
    run = _resume_volume_run(db, project_id, volume.id)
    _ensure_volume_run_can_resume(run)
    volume.status = VolumeContractStatus.EXTENDING
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume, author_note=payload.author_note if payload else None, size=payload.window_size if payload else None)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window), "extended": True}


@router.post("/projects/{project_id}/volumes/{volume_id}/pause")
def pause_volume(project_id: str, volume_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    run = _wake_volume_run(db, project_id, volume.id)
    if run and run.status in {AutoDirectorRunStatus.CREATED, AutoDirectorRunStatus.RUNNING}:
        revoke_claim(run)
        context = dict(run.context or {})
        if run.current_stage != AutoDirectorStage.PAUSED:
            context["resume_stage"] = _enum(run.current_stage)
        context["stop_requested"] = True
        run.context = context
        run.status = AutoDirectorRunStatus.PAUSED
        run.current_stage = AutoDirectorStage.PAUSED
        run.pause_reason = payload.reason or "AUTHOR_PAUSED"
        run.next_action = "作者已暂停当前卷；继续时将从已保存的检查点恢复。"
        window = db.get(ChapterPlanningWindow, context.get("window_id")) if context.get("window_id") else None
        if window and window.project_id == project_id and window.volume_id == volume.id and window.status == ChapterWindowStatus.ACTIVE:
            window.status = ChapterWindowStatus.PLANNING
            window.author_note = payload.reason or window.author_note
    db.commit()
    return {"volume": _volume_payload(volume), "paused": bool(run and run.status == AutoDirectorRunStatus.PAUSED), "reason": payload.reason, "run": _run_payload(db, run) if run else None}


@router.post("/projects/{project_id}/volumes/{volume_id}/seal")
def seal_volume(project_id: str, volume_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    try:
        volume = AuthorGuidedVolumeService().seal(db, _volume_or_404(db, project_id, volume_id), payload.author_confirmed)
        db.commit(); return {"volume": _volume_payload(volume), "snapshot_id": volume.sealed_snapshot_id}
    except AuthorGuidedVolumeError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/projects/{project_id}/volumes/{volume_id}/unseal")
def unseal_volume(project_id: str, volume_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    try:
        volume = AuthorGuidedVolumeService().unseal(db, _volume_or_404(db, project_id, volume_id), payload.reason or "作者解封")
        db.commit(); return {"volume": _volume_payload(volume), "audit": True}
    except AuthorGuidedVolumeError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


def _current_snapshot(db: Session, volume: VolumeContract) -> VolumeContinuitySnapshot | None:
    if volume.sealed_snapshot_id:
        snapshot = db.get(VolumeContinuitySnapshot, volume.sealed_snapshot_id)
        if snapshot and snapshot.project_id == volume.project_id and snapshot.volume_id == volume.id:
            return snapshot
    return db.scalar(select(VolumeContinuitySnapshot).where(
        VolumeContinuitySnapshot.project_id == volume.project_id,
        VolumeContinuitySnapshot.volume_id == volume.id,
    ).order_by(VolumeContinuitySnapshot.snapshot_version.desc(), VolumeContinuitySnapshot.id.desc()).limit(1))


@router.get("/projects/{project_id}/volumes/{volume_id}/snapshot")
def get_volume_snapshot(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id); snapshot = _current_snapshot(db, volume)
    if not snapshot: raise HTTPException(status_code=404, detail="Volume snapshot not found")
    return {key: getattr(snapshot, key) for key in ("id", "project_id", "book_contract_id", "volume_id", "snapshot_version", "summary", "confirmed_facts", "character_states", "relationship_changes", "timeline_end", "location_states", "item_states", "active_threads", "unresolved_foreshadowings", "paid_off_foreshadowings", "forbidden_future_reveals", "next_volume_hooks", "source_fingerprint")}


@router.get("/projects/{project_id}/volumes/{volume_id}/continuity")
def get_volume_continuity(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id); snapshot = _current_snapshot(db, volume)
    return {"volume": _volume_payload(volume), "snapshot": {key: getattr(snapshot, key) for key in ("id", "snapshot_version", "summary", "confirmed_facts", "character_states", "relationship_changes", "timeline_end", "location_states", "item_states", "active_threads", "unresolved_foreshadowings", "paid_off_foreshadowings", "forbidden_future_reveals", "next_volume_hooks", "source_fingerprint")} if snapshot else None}


@router.get("/projects/{project_id}/foreshadowings")
def list_foreshadowings(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [{"id": item.id, "foreshadow_ref": item.foreshadow_ref, "title": item.title, "description": item.description, "source_volume_id": item.source_volume_id, "source_chapter_id": item.source_chapter_id, "status": _enum(item.status), "earliest_payoff_volume": item.earliest_payoff_volume, "target_payoff_volume": item.target_payoff_volume, "allowed_reveal_level": item.allowed_reveal_level, "related_character_ids": item.related_character_ids, "related_fact_ids": item.related_fact_ids, "aliases": item.aliases} for item in db.scalars(select(ForeshadowingLedger).where(ForeshadowingLedger.project_id == project_id).order_by(ForeshadowingLedger.created_at)).all()]


@router.post("/projects/{project_id}/volumes/{volume_id}/foreshadowings")
def seed_foreshadowing(project_id: str, volume_id: str, payload: dict, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    ref = str(payload.get("foreshadow_ref") or "").strip(); title = str(payload.get("title") or "").strip()
    if not ref or not title: raise HTTPException(status_code=422, detail={"code": "FORESHADOW_REF_AND_TITLE_REQUIRED"})
    existing = db.scalar(select(ForeshadowingLedger).where(ForeshadowingLedger.project_id == project_id, ForeshadowingLedger.foreshadow_ref == ref))
    if existing: return {"id": existing.id, "status": _enum(existing.status), "idempotent": True}
    item = ForeshadowingLedger(project_id=project_id, foreshadow_ref=ref, title=title, description=payload.get("description"), source_volume_id=volume.id, status=ForeshadowingStatus.SEEDED, earliest_payoff_volume=payload.get("earliest_payoff_volume"), target_payoff_volume=payload.get("target_payoff_volume"), allowed_reveal_level=payload.get("allowed_reveal_level"), related_character_ids=payload.get("related_character_ids") or [], related_fact_ids=payload.get("related_fact_ids") or [], aliases=payload.get("aliases") or [], fingerprint=str(payload.get("fingerprint") or ref))
    db.add(item); db.commit(); return {"id": item.id, "status": _enum(item.status), "foreshadow_ref": item.foreshadow_ref}


@router.post("/projects/{project_id}/foreshadowings/{foreshadow_id}/status")
def update_foreshadowing_status(project_id: str, foreshadow_id: str, payload: dict, db: Session = Depends(get_db)):
    require_project(db, project_id)
    item = db.get(ForeshadowingLedger, foreshadow_id)
    if not item or item.project_id != project_id:
        raise HTTPException(status_code=404, detail="Foreshadowing not found")
    try:
        next_status = ForeshadowingStatus(str(payload.get("status") or "").upper())
        AuthorGuidedVolumeService().update_foreshadowing(db, item, next_status, volume_id=payload.get("volume_id"), chapter_id=payload.get("chapter_id"))
        db.commit()
        return {"id": item.id, "status": _enum(item.status), "fingerprint": item.fingerprint}
    except (ValueError, AuthorGuidedVolumeError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": getattr(exc, "code", "FORESHADOWING_STATUS_INVALID"), "message": str(exc)}) from exc


@router.post("/projects/{project_id}/volumes/{volume_id}/guidance")
def add_guidance(project_id: str, volume_id: str, payload: AuthorGuidancePayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    guidance = AuthorGuidedVolumeService().record_guidance(db, volume, payload.model_dump())
    db.commit(); return {"id": guidance.id, "analysis": guidance.analysis, "requires_replan": guidance.requires_replan, "affected_scope": guidance.affected_scope, "status": guidance.status}


@router.post("/projects/{project_id}/volumes/{volume_id}/characters", status_code=status.HTTP_201_CREATED)
def add_author_character(project_id: str, volume_id: str, payload: AuthorCharacterPayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED:
        raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    character, guidance = AuthorGuidedVolumeService().add_character(db, project_id, volume, payload.model_dump())
    db.commit()
    return {"character": {"id": character.id, "name": character.name, "profile": character.profile, "goals": character.goals, "narrative_relevance": character.narrative_relevance}, "guidance": {"id": guidance.id, "analysis": guidance.analysis, "requires_replan": guidance.requires_replan}}


@router.patch("/projects/{project_id}/volumes/{volume_id}/contract")
def update_volume_contract(project_id: str, volume_id: str, payload: VolumeContractUpdatePayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    try:
        guidance = AuthorGuidedVolumeService().update_volume_contract(db, volume, payload.model_dump(exclude_none=True))
        db.commit()
        return {"volume": _volume_payload(volume), "guidance": {"id": guidance.id, "analysis": guidance.analysis, "requires_replan": guidance.requires_replan}}
    except AuthorGuidedVolumeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/projects/{project_id}/volumes/{volume_id}/plot-direction")
def update_plot_direction(project_id: str, volume_id: str, payload: PlotDirectionPayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    contract = db.get(BookContract, volume.book_contract_id)
    if not contract:
        raise HTTPException(status_code=409, detail={"code": "BOOK_CONTRACT_MISSING"})
    try:
        guidance = AuthorGuidedVolumeService().update_plot_direction(db, contract, volume, payload.model_dump())
        db.commit()
        return {"guidance_id": guidance.id, "contract": _contract_payload(contract), "analysis": guidance.analysis, "requires_replan": guidance.requires_replan}
    except AuthorGuidedVolumeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/projects/{project_id}/volumes/{volume_id}/completion-proposal")
def completion_proposal(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id); contract = db.get(BookContract, volume.book_contract_id)
    if not contract: raise HTTPException(status_code=409, detail={"code": "BOOK_CONTRACT_MISSING"})
    proposal = AuthorGuidedVolumeService().completion_proposal(db, contract, volume); db.commit()
    return {"id": proposal.id, "status": _enum(proposal.status), "reason": proposal.reason, "unresolved_threads": proposal.unresolved_threads, "unresolved_foreshadowings": proposal.unresolved_foreshadowings, "protagonist_arc_status": proposal.protagonist_arc_status, "main_conflict_status": proposal.main_conflict_status, "ending_requirements": proposal.ending_requirements, "evidence_chapter_ids": proposal.evidence_chapter_ids}


@router.post("/projects/{project_id}/completion-proposals/{proposal_id}/confirm")
def confirm_completion(project_id: str, proposal_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    require_project(db, project_id); proposal = db.get(BookCompletionProposal, proposal_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Completion proposal not found")
    if not payload.author_confirmed or proposal.status != BookCompletionProposalStatus.PROPOSED: raise HTTPException(status_code=409, detail={"code": "AUTHOR_CONFIRMATION_REQUIRED"})
    contract = db.get(BookContract, proposal.book_contract_id) if proposal.book_contract_id else None
    if not contract:
        raise HTTPException(status_code=409, detail={"code": "BOOK_CONTRACT_MISSING"})
    allow_auto_complete = bool((contract.length_policy or {}).get("allow_auto_complete", False))
    if not allow_auto_complete:
        proposal.status = BookCompletionProposalStatus.AUTHOR_CONFIRMED
        db.commit()
        return {"id": proposal.id, "status": _enum(proposal.status), "book_completed": False, "next_action": "作者已确认完结建议；系统保持开放，未启用自动完结。"}
    proposal.status = BookCompletionProposalStatus.COMPLETED
    contract.status = "COMPLETED"
    run = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.run_mode == "AUTHOR_GUIDED_VOLUME", AutoDirectorRun.context["book_contract_id"].as_string() == contract.id).order_by(AutoDirectorRun.updated_at.desc()))
    if run:
        run.status = AutoDirectorRunStatus.COMPLETED
        run.current_stage = AutoDirectorStage.BOOK_COMPLETED
        run.pause_reason = None
        run.next_action = "作者已确认，书籍已按合同条件完成。"
        run.completed_at = datetime.utcnow()
    db.commit()
    return {"id": proposal.id, "status": _enum(proposal.status), "book_completed": True, "next_action": "书籍已按作者配置完成。"}
