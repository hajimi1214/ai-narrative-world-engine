"""Operational status and rebuild routes for derived projections."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..formal_state import FormalStateIdentityService
from ..models import NarrativeStructureRevision
from ..narrative_structure import NarrativeStructureService
from ..narrative_structure_projection import NarrativeStructureProjectionService
from ..retcon_apply import has_pending_replay
from ..retrieval_index import CognitionRetrievalProjectionService, MemoryANNIndexStatusService, ResearchLexicalIndexService
from ..scaling import ProjectHistoryProjectionService
from .common import Payload, get_db, require_project

router = APIRouter(tags=["scaling"])


@router.get("/projects/{project_id}/scaling/status")
def scaling_status(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return ProjectHistoryProjectionService().status(db, project_id) | {
        "cognition": CognitionRetrievalProjectionService().status(db, project_id),
        "research": ResearchLexicalIndexService().status(db, project_id),
        "formal_state": FormalStateIdentityService().status(db, project_id),
        "narrative_structure": NarrativeStructureProjectionService().status(db, project_id),
    }


@router.get("/projects/{project_id}/scaling/formal-state/status")
def formal_state_status(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return FormalStateIdentityService().status(db, project_id)


@router.post("/projects/{project_id}/scaling/formal-state/rebuild")
def rebuild_formal_state(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    try:
        identity = FormalStateIdentityService().rebuild(db, project_id)
        db.commit()
        return FormalStateIdentityService().status(db, project_id) | {"identity_id": identity.id}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "FORMAL_STATE_REBUILD_FAILED"}) from exc


@router.get("/projects/{project_id}/scaling/retrieval/status")
def retrieval_status(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return {
        "cognition": CognitionRetrievalProjectionService().status(db, project_id),
        "research": ResearchLexicalIndexService().status(db, project_id),
        "ann": MemoryANNIndexStatusService().status(db, project_id),
    }


def _rebuild(db: Session, project_id: str, service, error_code: str):
    require_project(db, project_id)
    try:
        projection = service.rebuild(db, project_id)
        db.commit()
        return service.status(db, project_id) | {"projection_id": projection.id}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": error_code}) from exc


@router.post("/projects/{project_id}/scaling/history-index/rebuild")
def rebuild_history_index(project_id: str, db: Session = Depends(get_db)):
    return _rebuild(db, project_id, ProjectHistoryProjectionService(), "HISTORY_PROJECTION_REBUILD_FAILED")


@router.post("/projects/{project_id}/scaling/narrative-structure/rebuild")
def rebuild_narrative_structure_projection(project_id: str, db: Session = Depends(get_db)):
    return _rebuild(db, project_id, NarrativeStructureProjectionService(), "NARRATIVE_STRUCTURE_PROJECTION_REBUILD_FAILED")


@router.post("/projects/{project_id}/scaling/cognition-index/rebuild")
def rebuild_cognition_index(project_id: str, db: Session = Depends(get_db)):
    return _rebuild(db, project_id, CognitionRetrievalProjectionService(), "COGNITION_RETRIEVAL_INDEX_REBUILD_FAILED")


@router.post("/projects/{project_id}/scaling/research-index/rebuild")
def rebuild_research_index(project_id: str, db: Session = Depends(get_db)):
    return _rebuild(db, project_id, ResearchLexicalIndexService(), "RESEARCH_LEXICAL_INDEX_REBUILD_FAILED")


def _structure_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    return HTTPException(status_code=422 if code == "INVALID_NARRATIVE_STRUCTURE_CONFIG" else 409, detail={"code": code})


@router.post("/projects/{project_id}/narrative-structure/preview")
def preview_narrative_structure(project_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    require_project(db, project_id)
    values = payload.model_dump(exclude_unset=True) if payload else {}
    try:
        result = NarrativeStructureService().preview(db, project_id, values.get("config"))
    except ValueError as exc:
        raise _structure_error(exc) from exc
    pending = has_pending_replay(db, project_id)
    return result | {"pending_replay": pending, "warning": "RETCON_REPLAY_REQUIRED" if pending else None}


@router.post("/projects/{project_id}/narrative-structure/sync")
def sync_narrative_structure(project_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    require_project(db, project_id)
    values = payload.model_dump(exclude_unset=True) if payload else {}
    try:
        revision, idempotent = NarrativeStructureService().sync(db, project_id, values.get("config"), values.get("expected_source_fingerprint"))
        db.commit(); db.refresh(revision)
        return NarrativeStructureService().payload(db, revision) | {"idempotent": idempotent, "stale": False, "source_fingerprint": revision.source_history_fingerprint}
    except ValueError as exc:
        db.rollback()
        raise _structure_error(exc) from exc


@router.get("/projects/{project_id}/narrative-structure")
def get_narrative_structure(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return NarrativeStructureService().current(db, project_id)


@router.get("/projects/{project_id}/narrative-structure/revisions")
def list_narrative_structure_revisions(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    rows = db.scalars(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project_id).order_by(NarrativeStructureRevision.created_at.desc(), NarrativeStructureRevision.id.desc())).all()
    return [NarrativeStructureService.revision_metadata(item) for item in rows]
