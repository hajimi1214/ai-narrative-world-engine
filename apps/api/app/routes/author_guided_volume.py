from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api_types import AuthorGuidancePayload, AuthorGuidedRunCreatePayload, VolumeActionPayload
from ..author_guided_volume import AuthorGuidedVolumeError, AuthorGuidedVolumeService, _contract_payload, _enum, _volume_payload, _window_payload
from ..models import AuthorGuidance, AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, BookCompletionProposal, BookContract, ChapterPlanningWindow, Project, VolumeContract, VolumeContractStatus, VolumeContinuitySnapshot
from .common import get_db, require_project

router = APIRouter(tags=["author-guided-volume"])


def _run_payload(db: Session, run: AutoDirectorRun) -> dict:
    return {"id": run.id, "project_id": run.project_id, "status": _enum(run.status), "run_mode": run.run_mode, "current_stage": _enum(run.current_stage), "pause_reason": run.pause_reason, "next_action": run.next_action, "idempotency_key": run.idempotency_key, "context": run.context or {}, "settings": run.settings or {}, "token_usage": run.token_usage or {}}


def _volume_or_404(db: Session, project_id: str, volume_id: str) -> VolumeContract:
    require_project(db, project_id)
    volume = db.get(VolumeContract, volume_id)
    if not volume or volume.project_id != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")
    return volume


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


@router.get("/projects/{project_id}/volumes")
def list_volumes(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    service = AuthorGuidedVolumeService()
    volumes = db.scalars(select(VolumeContract).where(VolumeContract.project_id == project_id).order_by(VolumeContract.volume_number)).all()
    return [{**_volume_payload(volume), "progress": service.progress(db, volume), "windows": [_window_payload(item) for item in db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id).order_by(ChapterPlanningWindow.created_at)).all()]} for volume in volumes]


@router.post("/projects/{project_id}/volumes/{volume_id}/start")
def start_volume(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    volume.status = VolumeContractStatus.ACTIVE
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume)
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window)}


@router.post("/projects/{project_id}/volumes/{volume_id}/continue")
def continue_volume(project_id: str, volume_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    window = AuthorGuidedVolumeService().ensure_window(db, db.get(Project, project_id), volume, author_note=(payload or {}).get("author_note"))
    db.commit(); return {"volume": _volume_payload(volume), "window": _window_payload(window)}


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


@router.post("/projects/{project_id}/volumes/{volume_id}/guidance")
def add_guidance(project_id: str, volume_id: str, payload: AuthorGuidancePayload, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id)
    if volume.status == VolumeContractStatus.SEALED: raise HTTPException(status_code=409, detail={"code": "VOLUME_SEALED"})
    guidance = AuthorGuidance(project_id=project_id, volume_id=volume.id, author_note=payload.author_note, author_locked_constraints=payload.author_locked_constraints, author_override_reason=payload.author_override_reason, affected_scope=payload.affected_scope, requires_replan=payload.requires_replan, analysis={"scope": payload.affected_scope, "written_content_protected": True})
    db.add(guidance); db.commit(); return {"id": guidance.id, "analysis": guidance.analysis, "requires_replan": guidance.requires_replan, "affected_scope": guidance.affected_scope}


@router.get("/projects/{project_id}/volumes/{volume_id}/completion-proposal")
def completion_proposal(project_id: str, volume_id: str, db: Session = Depends(get_db)):
    volume = _volume_or_404(db, project_id, volume_id); contract = db.get(BookContract, volume.book_contract_id)
    if not contract: raise HTTPException(status_code=409, detail={"code": "BOOK_CONTRACT_MISSING"})
    proposal = AuthorGuidedVolumeService().completion_proposal(db, contract, volume); db.commit()
    return {"id": proposal.id, "status": _enum(proposal.status), "reason": proposal.reason, "unresolved_threads": proposal.unresolved_threads, "unresolved_foreshadowings": proposal.unresolved_foreshadowings, "protagonist_arc_status": proposal.protagonist_arc_status, "main_conflict_status": proposal.main_conflict_status, "ending_requirements": proposal.ending_requirements}
