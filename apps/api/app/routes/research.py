"""Research Library ingestion and read-only packet routes."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..research import KnowledgePacketBuilder, ResearchCorpusFingerprintBuilder, ResearchDomainError, ResearchIngestionService
from ..models import ResearchDocument, ResearchDocumentRevision
from .common import Payload, get_db, require_project, serialize

router = APIRouter(tags=["research"])


def research_revision_payload(revision: ResearchDocumentRevision, *, include_content: bool = False) -> dict:
    value = {
        "id": revision.id, "project_id": revision.project_id, "document_id": revision.document_id,
        "version": revision.version, "active": revision.active,
        "content_fingerprint": revision.content_fingerprint,
        "normalized_fingerprint": revision.normalized_fingerprint,
        "ingestion_config": revision.ingestion_config,
        "ingestion_config_fingerprint": revision.ingestion_config_fingerprint,
        "supersedes_revision_id": revision.supersedes_revision_id,
        "created_at": serialize(revision.created_at),
    }
    if include_content:
        value["content"] = revision.content
    return value


def research_document_payload(db: Session, document: ResearchDocument, *, include_revisions: bool = False) -> dict:
    active = db.scalar(select(ResearchDocumentRevision).where(
        ResearchDocumentRevision.document_id == document.id,
        ResearchDocumentRevision.active.is_(True),
    ))
    value = {
        "id": document.id, "project_id": document.project_id, "title": document.title,
        "source_tier": serialize(document.source_tier), "source_kind": serialize(document.source_kind),
        "source_uri": document.source_uri, "source_metadata": document.source_metadata,
        "active": document.active, "archived_at": serialize(document.archived_at),
        "created_at": serialize(document.created_at), "updated_at": serialize(document.updated_at),
        "active_revision_id": active.id if active else None,
        "corpus_fingerprint": ResearchCorpusFingerprintBuilder().build(db, document.project_id),
    }
    if include_revisions:
        value["revisions"] = [research_revision_payload(item) for item in db.scalars(
            select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document.id)
            .order_by(ResearchDocumentRevision.version.desc())
        ).all()]
    return value


def _domain_error(exc: ResearchDomainError, *, create: bool = False) -> HTTPException:
    bad_request = {
        "RESEARCH_CONFIG_INVALID", "RESEARCH_SOURCE_URI_INVALID", "RESEARCH_METADATA_INVALID",
        "RESEARCH_TITLE_REQUIRED", "RESEARCH_CONTENT_EMPTY", "RESEARCH_QUERY_EMPTY", "RESEARCH_FILTER_INVALID",
    }
    return HTTPException(status_code=400 if exc.code in bad_request else 409, detail={"code": exc.code, **(exc.detail or {})})


@router.post("/projects/{project_id}/research/documents", status_code=status.HTTP_201_CREATED)
def create_research_document(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    values = payload.model_dump()
    try:
        result = ResearchIngestionService().ingest(
            db, project_id, title=values.get("title"), content=values.get("content"),
            source_tier=values.get("source_tier", "PROJECT_RESEARCH"),
            source_kind=values.get("source_kind", "MANUAL_TEXT"), source_uri=values.get("source_uri"),
            source_metadata=values.get("source_metadata"),
            client_request_id=values.get("client_request_id") or values.get("idempotency_key"), request=values,
        )
        db.commit(); db.refresh(result.document); db.refresh(result.revision)
        return {"document": research_document_payload(db, result.document), "revision": research_revision_payload(result.revision, include_content=True), "chunk_count": len(result.chunks), "idempotent": result.idempotent}
    except ResearchDomainError as exc:
        db.rollback()
        raise _domain_error(exc, create=True) from exc


@router.get("/projects/{project_id}/research/documents")
def list_research_documents(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    rows = db.scalars(select(ResearchDocument).where(ResearchDocument.project_id == project_id).order_by(ResearchDocument.created_at, ResearchDocument.id)).all()
    return [research_document_payload(db, item) for item in rows]


def _document_or_404(db: Session, project_id: str, document_id: str) -> ResearchDocument:
    document = db.get(ResearchDocument, document_id)
    if not document or document.project_id != project_id:
        raise HTTPException(status_code=404, detail="Research document not found")
    return document


@router.get("/projects/{project_id}/research/documents/{document_id}")
def get_research_document(project_id: str, document_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return research_document_payload(db, _document_or_404(db, project_id, document_id), include_revisions=True)


@router.get("/projects/{project_id}/research/documents/{document_id}/revisions")
def list_research_revisions(project_id: str, document_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    document = _document_or_404(db, project_id, document_id)
    rows = db.scalars(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document.id).order_by(ResearchDocumentRevision.version.desc())).all()
    return [research_revision_payload(item) for item in rows]


@router.post("/projects/{project_id}/research/documents/{document_id}/revisions", status_code=status.HTTP_201_CREATED)
def create_research_revision(project_id: str, document_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    _document_or_404(db, project_id, document_id)
    values = payload.model_dump()
    try:
        result = ResearchIngestionService().add_revision(db, document_id, content=values.get("content"), request=values, source_metadata=values.get("source_metadata"))
        db.commit(); db.refresh(result.revision)
        return {"document_id": document_id, "revision": research_revision_payload(result.revision, include_content=True), "chunk_count": len(result.chunks), "idempotent": result.idempotent}
    except ResearchDomainError as exc:
        db.rollback()
        raise _domain_error(exc) from exc


@router.post("/projects/{project_id}/research/documents/{document_id}/archive")
def archive_research_document(project_id: str, document_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    _document_or_404(db, project_id, document_id)
    try:
        document = ResearchIngestionService().archive(db, document_id)
        db.commit(); db.refresh(document)
        return research_document_payload(db, document)
    except ResearchDomainError as exc:
        db.rollback()
        raise _domain_error(exc) from exc


def _packet(db: Session, project_id: str, payload: Payload, mode: str):
    require_project(db, project_id)
    values = payload.model_dump()
    try:
        return KnowledgePacketBuilder().build(db, project_id, values.get("query", ""), mode=mode, filters=values.get("filters"), request=values)
    except ResearchDomainError as exc:
        raise _domain_error(exc) from exc


@router.post("/projects/{project_id}/research/search")
def search_research(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    packet = _packet(db, project_id, payload, "AUTHOR")
    return packet.as_dict() | {"total_chars": sum(len(item["content"]) for item in packet.hits)}


@router.post("/projects/{project_id}/knowledge/preview")
def preview_knowledge(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    values = payload.model_dump()
    return _packet(db, project_id, payload, values.get("mode", "AUTHOR")).as_dict()
