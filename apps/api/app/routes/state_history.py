"""State delta, committed-scene history, and causal provenance routes."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..causal_ledger import CausalLedgerBackfillService, CausalProvenanceQuery
from ..historical import CurrentSceneCheckpointResolver
from ..models import AutonomousWorldRun, Scene, SceneStateCheckpoint, StateDeltaBatch, StateDeltaItem, TimelineEvent
from ..scene_commit import SceneCommitService
from ..state_delta import StateDeltaCandidateBuilder
from ..state_delta_validation import StateDeltaValidator
from .common import get_db, record_dict, require_project

router = APIRouter(tags=["state-history"])


class StateDeltaDerivePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_resolution_id: str


def ensure_autonomous_run_idle(db: Session, project_id: str) -> None:
    if db.scalar(select(AutonomousWorldRun.id).where(
        AutonomousWorldRun.project_id == project_id,
        AutonomousWorldRun.active.is_(True),
    )):
        raise HTTPException(status_code=409, detail={"code": "AUTONOMY_RUN_ACTIVE"})


def state_delta_batch_payload(db: Session, batch: StateDeltaBatch) -> dict:
    items = db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id).order_by(StateDeltaItem.ordinal, StateDeltaItem.id)).all()
    return record_dict(batch) | {"items": [record_dict(item) for item in items]}


def scene_checkpoint_payload(checkpoint: SceneStateCheckpoint) -> dict:
    """Return metadata only; snapshots may include secret canon or cognition."""
    return {
        "id": checkpoint.id, "project_id": checkpoint.project_id, "scene_id": checkpoint.scene_id,
        "sequence": checkpoint.sequence, "version": checkpoint.version, "active": checkpoint.active,
        "origin": getattr(checkpoint.origin, "value", checkpoint.origin),
        "capture_protocol_version": checkpoint.capture_protocol_version,
        "pre_snapshot_id": checkpoint.pre_snapshot_id, "post_snapshot_id": checkpoint.post_snapshot_id,
        "pre_state_fingerprint": checkpoint.pre_state_fingerprint,
        "post_state_fingerprint": checkpoint.post_state_fingerprint,
        "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint,
        "source_scene_commit_id": checkpoint.source_scene_commit_id,
        "source_replay_session_id": checkpoint.source_replay_session_id,
        "supersedes_checkpoint_id": checkpoint.supersedes_checkpoint_id,
        "created_at": checkpoint.created_at,
    }


@router.post("/projects/{project_id}/state-delta-batches/derive", status_code=status.HTTP_201_CREATED)
def derive_state_delta_batch(project_id: str, payload: StateDeltaDerivePayload, db: Session = Depends(get_db)):
    ensure_autonomous_run_idle(db, project_id)
    require_project(db, project_id)
    try:
        batch, _items, existing = StateDeltaCandidateBuilder().derive(db, project_id, payload.source_resolution_id)
        if not existing:
            db.commit(); db.refresh(batch)
        return state_delta_batch_payload(db, batch) | {"idempotent": existing}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


@router.get("/projects/{project_id}/state-delta-batches")
def list_state_delta_batches(project_id: str, source_resolution_id: str | None = None, status_filter: str | None = Query(None, alias="status"), db: Session = Depends(get_db)):
    require_project(db, project_id)
    query = select(StateDeltaBatch).where(StateDeltaBatch.project_id == project_id)
    if source_resolution_id:
        query = query.where(StateDeltaBatch.source_resolution_id == source_resolution_id)
    if status_filter:
        query = query.where(StateDeltaBatch.status == status_filter)
    rows = db.scalars(query.order_by(StateDeltaBatch.created_at.desc(), StateDeltaBatch.id.desc())).all()
    return [state_delta_batch_payload(db, row) for row in rows]


@router.get("/projects/{project_id}/state-delta-batches/{batch_id}")
def get_state_delta_batch(project_id: str, batch_id: str, db: Session = Depends(get_db)):
    batch = db.get(StateDeltaBatch, batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="State Delta Batch not found")
    return state_delta_batch_payload(db, batch)


@router.post("/projects/{project_id}/state-delta-batches/{batch_id}/validate")
def validate_state_delta_batch(project_id: str, batch_id: str, db: Session = Depends(get_db)):
    ensure_autonomous_run_idle(db, project_id)
    require_project(db, project_id)
    batch = db.get(StateDeltaBatch, batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="State Delta Batch not found")
    try:
        result = StateDeltaValidator().validate(db, project_id, batch_id)
        if not result.idempotent:
            db.commit(); db.refresh(result.batch)
        return state_delta_batch_payload(db, result.batch) | {"idempotent": result.idempotent}
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


@router.post("/projects/{project_id}/performances/{performance_id}/commit-scene")
def commit_scene(project_id: str, performance_id: str, db: Session = Depends(get_db)):
    ensure_autonomous_run_idle(db, project_id)
    try:
        result = SceneCommitService().commit(db, project_id, performance_id)
        if not result.idempotent:
            db.commit(); db.refresh(result.commit)
        return {
            "scene": record_dict(result.scene), "scene_commit": record_dict(result.commit),
            "delta_batches": [state_delta_batch_payload(db, batch) for batch in result.batches],
            "checkpoint": record_dict(result.checkpoint), "idempotent": result.idempotent,
        }
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "SCENE_COMMIT_INTEGRITY_ERROR"}) from exc
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "SCENE_COMMIT_FAILED"}) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


def _scene_or_404(db: Session, project_id: str, scene_id: str) -> Scene:
    scene = db.get(Scene, scene_id)
    if not scene or scene.project_id != project_id:
        raise HTTPException(status_code=404, detail="Scene not found")
    return scene


@router.get("/projects/{project_id}/scenes/{scene_id}/checkpoint")
def get_current_scene_checkpoint(project_id: str, scene_id: str, db: Session = Depends(get_db)):
    _scene_or_404(db, project_id, scene_id)
    try:
        return scene_checkpoint_payload(CurrentSceneCheckpointResolver().current(db, project_id, scene_id))
    except ValueError as exc:
        raise HTTPException(status_code=404 if str(exc) == "SCENE_CHECKPOINT_MISSING" else 409, detail={"code": str(exc)}) from exc


@router.get("/projects/{project_id}/scenes/{scene_id}/checkpoints")
def list_scene_checkpoints(project_id: str, scene_id: str, db: Session = Depends(get_db)):
    _scene_or_404(db, project_id, scene_id)
    return [scene_checkpoint_payload(row) for row in CurrentSceneCheckpointResolver().history(db, project_id, scene_id)]


@router.get("/projects/{project_id}/timeline")
def list_timeline(project_id: str, sequence_from: int | None = None, sequence_to: int | None = None, active_only: bool = True, event_type: str | None = None, db: Session = Depends(get_db)):
    require_project(db, project_id)
    query = select(TimelineEvent).where(TimelineEvent.project_id == project_id)
    if active_only:
        query = query.where(TimelineEvent.active.is_(True))
    if sequence_from is not None:
        query = query.where(TimelineEvent.sequence >= sequence_from)
    if sequence_to is not None:
        query = query.where(TimelineEvent.sequence <= sequence_to)
    if event_type:
        query = query.where(TimelineEvent.event_type == event_type)
    reader = CausalProvenanceQuery()
    return [reader.event_payload(row) for row in db.scalars(query.order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.event_type, TimelineEvent.id)).all()]


@router.get("/projects/{project_id}/causal-ledger/state-history")
def causal_state_history(project_id: str, target_type: str, target_id: str, path: str | None = None, include_superseded: bool = False, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return CausalProvenanceQuery().state_history(db, project_id, target_type, target_id, path, include_superseded)


def _trace_or_404(fn, db: Session, project_id: str, *args):
    require_project(db, project_id)
    try:
        return fn(db, project_id, *args)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc


@router.get("/projects/{project_id}/causal-ledger/why-state")
def causal_why_state(project_id: str, target_type: str, target_id: str, path: str, db: Session = Depends(get_db)):
    return _trace_or_404(CausalProvenanceQuery().why_state, db, project_id, target_type, target_id, path)


@router.get("/projects/{project_id}/causal-ledger/decisions/{decision_id}")
def causal_trace_decision(project_id: str, decision_id: str, db: Session = Depends(get_db)):
    return _trace_or_404(CausalProvenanceQuery().trace_decision, db, project_id, decision_id)


@router.get("/projects/{project_id}/causal-ledger/knowledge/{knowledge_id}")
def causal_trace_knowledge(project_id: str, knowledge_id: str, db: Session = Depends(get_db)):
    return _trace_or_404(CausalProvenanceQuery().trace_knowledge, db, project_id, knowledge_id)


@router.get("/projects/{project_id}/causal-ledger/resources/{resource_type}/{resource_id}")
def causal_resource_links(project_id: str, resource_type: str, resource_id: str, db: Session = Depends(get_db)):
    return _trace_or_404(CausalProvenanceQuery().resource_links, db, project_id, resource_type, resource_id)


@router.post("/projects/{project_id}/causal-ledger/backfill")
def backfill_causal_ledger(project_id: str, db: Session = Depends(get_db)):
    try:
        CausalLedgerBackfillService().backfill(db, project_id)
        db.commit()
        return {"project_id": project_id, "status": "INDEXED"}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
