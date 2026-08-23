from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api_types import AuthorCharacterPayload, AuthorGuidancePayload, AuthorGuidedRunCreatePayload, PlotDirectionPayload, VolumeActionPayload, VolumeContractUpdatePayload
from ..author_guided_volume import AuthorGuidedVolumeError, AuthorGuidedVolumeService, _contract_payload, _enum, _volume_payload, _window_payload
from ..models import AuthorGuidance, AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep, BookCompletionProposal, BookContract, ChapterPlanningWindow, Character, ForeshadowingLedger, ForeshadowingStatus, Project, VolumeContract, VolumeContractStatus, VolumeContinuitySnapshot
from .common import get_db, require_project

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
    return {"id": run.id, "project_id": run.project_id, "status": _enum(run.status), "run_mode": run.run_mode, "current_stage": _enum(run.current_stage), "pause_reason": run.pause_reason, "next_action": run.next_action, "idempotency_key": run.idempotency_key, "context": run.context or {}, "settings": run.settings or {}, "token_usage": run.token_usage or {}, "usage_summary": usage_summary}


def _volume_or_404(db: Session, project_id: str, volume_id: str) -> VolumeContract:
    require_project(db, project_id)
    volume = db.get(VolumeContract, volume_id)
    if not volume or volume.project_id != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")
    return volume


def _wake_volume_run(db: Session, project_id: str, volume_id: str) -> AutoDirectorRun | None:
    run = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.run_mode == "AUTHOR_GUIDED_VOLUME", AutoDirectorRun.status.in_([AutoDirectorRunStatus.PAUSED, AutoDirectorRunStatus.FAILED]), AutoDirectorRun.context["volume_id"].as_string() == volume_id).order_by(AutoDirectorRun.updated_at.desc()))
    if run:
        run.status = AutoDirectorRunStatus.RUNNING
        run.current_stage = AutoDirectorStage.VOLUME_ACTIVE
        run.pause_reason = None
        run.context = {**(run.context or {}), "execute_window": True}
        run.next_action = "正在继续当前卷窗口。"
    return run


@router.post("/projects/{project_id}/author-guided-volume/runs", status_code=status.HTTP_201_CREATED)
def create_author_guided_run(project_id: str, payload: AuthorGuidedRunCreatePayload, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    data = payload.model_dump()
    key = payload.idempotency_key or f"author-volume-{project_id}"
    existing = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project_id, AutoDirectorRun.idempotency_key == key))
    if existing:
        return _run_payload(db, existing)
    contract = AuthorGuidedVolumeService().ensure_contract(db, project, data)
    run = AutoDirectorRun(project_id=project_id, idempotency_key=key, status=AutoDirectorRunStatus.RUNNING, current_stage=AutoDirectorStage.BOOK_CONTRACT, run_mode="AUTHOR_GUIDED_VOLUME", settings=data, context={"request": data, "book_contract_id": contract.id, "current_volume_number": 1})
    db.add(run); db.commit(); db.refresh(run)
    return _run_payload(db, run)


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
def continue_author_guided_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id); run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME": raise HTTPException(status_code=404, detail="Author-guided run not found")
    run.status = AutoDirectorRunStatus.RUNNING; run.current_stage = AutoDirectorStage.VOLUME_ACTIVE; run.pause_reason = None; run.next_action = "正在继续当前卷窗口。"
    run.context = {**(run.context or {}), "execute_window": True}
    db.commit(); return _run_payload(db, run)


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/retry")
def retry_author_guided_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME":
        raise HTTPException(status_code=404, detail="Author-guided run not found")
    if run.status == AutoDirectorRunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail={"code": "RUN_ALREADY_COMPLETED"})
    steps = db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id)).all()
    failures = sum(1 for step in steps if step.status in {"FAILED", "BLOCKED"})
    if failures >= int((run.settings or {}).get("max_retries", 2) or 2):
        run.status = AutoDirectorRunStatus.BLOCKED; run.current_stage = AutoDirectorStage.BLOCKED; run.pause_reason = "MAX_RETRIES_REACHED"; run.next_action = "请接管运行或重新规划。"
    else:
        run.status = AutoDirectorRunStatus.RUNNING; run.context = {**(run.context or {}), "execute_window": True, "stop_requested": False}; run.pause_reason = None; run.next_action = "已加入作者卷级队列，等待 worker 重试。"
    db.commit(); return _run_payload(db, run)


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/takeover")
def takeover_author_guided_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME":
        raise HTTPException(status_code=404, detail="Author-guided run not found")
    run.context = {**(run.context or {}), "stop_requested": True, "resume_stage": _enum(run.current_stage)}
    run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = "AUTHOR_TAKEOVER"; run.next_action = "作者已接管，可从现有检查点进入手动工作台。"
    db.commit(); return _run_payload(db, run)


@router.post("/projects/{project_id}/author-guided-volume/runs/{run_id}/pause")
def pause_author_guided_run(project_id: str, run_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    require_project(db, project_id); run = db.get(AutoDirectorRun, run_id)
    if not run or run.project_id != project_id or run.run_mode != "AUTHOR_GUIDED_VOLUME": raise HTTPException(status_code=404, detail="Author-guided run not found")
    run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.PAUSED; run.pause_reason = payload.reason or "AUTHOR_PAUSED"; run.next_action = "作者可以继续当前卷或接管修改。"
    db.commit(); return _run_payload(db, run)


@router.get("/projects/{project_id}/volumes")
def list_volumes(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    service = AuthorGuidedVolumeService()
    volumes = db.scalars(select(VolumeContract).where(VolumeContract.project_id == project_id).order_by(VolumeContract.volume_number)).all()
    return [{**_volume_payload(volume), "progress": service.progress(db, volume), "windows": [_window_payload(item) for item in db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id).order_by(ChapterPlanningWindow.created_at)).all()]} for volume in volumes]


@router.post("/projects/{project_id}/volumes/{volume_id}/next")
def prepare_next_volume(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    current = _volume_or_404(db, project_id, volume_id)
    if current.status != VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "CURRENT_VOLUME_NOT_SEALED"})
    contract = db.get(BookContract, current.book_contract_id); project = db.get(Project, project_id)
    next_volume = AuthorGuidedVolumeService().ensure_volume(db, project, contract, {"title": f"第 {current.volume_number + 1} 卷", "opening_state": {"source_volume_snapshot_id": current.sealed_snapshot_id}}, current.volume_number + 1)
    window = AuthorGuidedVolumeService().ensure_window(db, project, next_volume)
    db.commit(); return {"volume": _volume_payload(next_volume), "window": _window_payload(window), "source_snapshot_id": current.sealed_snapshot_id}


@router.post("/projects/{project_id}/volumes/{volume_id}/start")
def start_volume(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    volume.status = VolumeContractStatus.ACTIVE
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume)
    _wake_volume_run(db, project_id, volume.id)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window)}


@router.post("/projects/{project_id}/volumes/{volume_id}/continue")
def continue_volume(project_id: str, volume_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume, author_note=(payload or {}).get("author_note"))
    _wake_volume_run(db, project_id, volume.id)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window)}


@router.post("/projects/{project_id}/volumes/{volume_id}/extend")
def extend_volume(project_id: str, volume_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    volume.status = VolumeContractStatus.EXTENDING
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume, author_note=(payload or {}).get("author_note"), size=(payload or {}).get("window_size"))
    _wake_volume_run(db, project_id, volume.id)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window), "extended": True}


@router.post("/projects/{project_id}/volumes/{volume_id}/pause")
def pause_volume(project_id: str, volume_id: str, payload: VolumeActionPayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    for window in db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id, ChapterPlanningWindow.status == "ACTIVE")).all(): window.status = "PLANNING"; window.author_note = payload.reason
    db.commit(); return {"volume": _volume_payload(volume), "paused": True, "reason": payload.reason}


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


@router.get("/projects/{project_id}/volumes/{volume_id}/snapshot")
def get_volume_snapshot(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id); snapshot = db.scalar(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.volume_id == volume.id))
    if not snapshot: raise HTTPException(status_code=404, detail="Volume snapshot not found")
    return {key: getattr(snapshot, key) for key in ("id", "project_id", "book_contract_id", "volume_id", "snapshot_version", "summary", "confirmed_facts", "character_states", "relationship_changes", "timeline_end", "location_states", "item_states", "active_threads", "unresolved_foreshadowings", "paid_off_foreshadowings", "forbidden_future_reveals", "next_volume_hooks", "source_fingerprint")}


@router.get("/projects/{project_id}/volumes/{volume_id}/continuity")
def get_volume_continuity(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id); snapshot = db.scalar(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.volume_id == volume.id))
    return {"volume": _volume_payload(volume), "snapshot": {"id": snapshot.id, "summary": snapshot.summary, "confirmed_facts": snapshot.confirmed_facts, "character_states": snapshot.character_states, "active_threads": snapshot.active_threads, "unresolved_foreshadowings": snapshot.unresolved_foreshadowings, "forbidden_future_reveals": snapshot.forbidden_future_reveals, "next_volume_hooks": snapshot.next_volume_hooks} if snapshot else None}


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
    if not payload.author_confirmed or proposal.status != "PROPOSED": raise HTTPException(status_code=409, detail={"code": "AUTHOR_CONFIRMATION_REQUIRED"})
    proposal.status = "AUTHOR_CONFIRMED"; db.commit()
    return {"id": proposal.id, "status": _enum(proposal.status), "book_completed": False, "next_action": "作者仍需明确完成书籍，系统不会因章节数量自动结束。"}
