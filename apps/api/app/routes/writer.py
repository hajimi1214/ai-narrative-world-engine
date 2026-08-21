"""Writer projection routes; prose remains a projection, never world truth."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.errors import ModelProviderError
from ..model_router import ModelRouter
from ..models import Chapter, ChapterWriterDraft, WriterDraftStatus
from ..settings import get_settings
from ..writer import WriterDomainError, WriterProjectionService
from .common import Payload, get_db, record_dict, require_project, routed_provider, serialize

router = APIRouter(tags=["writer"])


def writer_draft_payload(draft: ChapterWriterDraft, *, include_content: bool = False) -> dict:
    value = {
        "id": draft.id, "project_id": draft.project_id, "chapter_id": draft.chapter_id,
        "version": draft.version, "status": serialize(draft.status), "origin": serialize(draft.origin),
        "source_quality_assessment_id": draft.source_quality_assessment_id,
        "client_request_id": draft.client_request_id, "request_fingerprint": draft.request_fingerprint,
        "chapter_structure_fingerprint": draft.chapter_structure_fingerprint,
        "chapter_source_fingerprint": draft.chapter_source_fingerprint,
        "writer_context_fingerprint": draft.writer_context_fingerprint,
        "source_structure_status": draft.source_structure_status, "source_scene_ids": draft.source_scene_ids,
        "writing_bible_id": draft.writing_bible_id, "writing_bible_version": draft.writing_bible_version,
        "writing_bible_fingerprint": draft.writing_bible_fingerprint,
        "pov_mode": serialize(draft.pov_mode), "pov_character_id": draft.pov_character_id,
        "provider": draft.provider, "model": draft.model, "model_request_id": draft.model_request_id,
        "prompt_fingerprint": draft.prompt_fingerprint, "title_candidate": draft.title_candidate,
        "content_fingerprint": draft.content_fingerprint, "word_count": draft.word_count,
        "scene_coverage": draft.scene_coverage, "source_refs": draft.source_refs,
        "validation_report": draft.validation_report, "parent_draft_id": draft.parent_draft_id,
        "supersedes_draft_id": draft.supersedes_draft_id, "created_at": serialize(draft.created_at),
        "completed_at": serialize(draft.completed_at), "adopted_at": serialize(draft.adopted_at),
        "stale_at": serialize(draft.stale_at),
    }
    if include_content:
        value.update({"content": draft.content, "prose": draft.content, "chapter_title": draft.title_candidate})
    return value


def _chapter_or_404(db: Session, project_id: str, chapter_id: str) -> Chapter:
    require_project(db, project_id)
    chapter = db.get(Chapter, chapter_id)
    if not chapter or chapter.project_id != project_id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


def _draft_or_404(db: Session, project_id: str, draft_id: str, chapter_id: str | None = None) -> ChapterWriterDraft:
    draft = db.get(ChapterWriterDraft, draft_id)
    if not draft or draft.project_id != project_id or (chapter_id and draft.chapter_id != chapter_id):
        raise HTTPException(status_code=404, detail="Writer draft not found")
    return draft


@router.post("/projects/{project_id}/chapters/{chapter_id}/writer/preview")
def writer_preview(project_id: str, chapter_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    _chapter_or_404(db, project_id, chapter_id)
    try:
        return WriterProjectionService().preview(db, chapter_id, payload.model_dump() if payload else {})
    except WriterDomainError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc


@router.post("/projects/{project_id}/chapters/{chapter_id}/writer/render")
def writer_render(project_id: str, chapter_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    _chapter_or_404(db, project_id, chapter_id)
    values = payload.model_dump() if payload else {}
    if values.get("idempotency_key") and not values.get("client_request_id"):
        values["client_request_id"] = values["idempotency_key"]
    try:
        settings = get_settings(); route = ModelRouter().resolve(db, project_id, settings, "WRITER")
        try:
            provider = routed_provider(settings, route, db, project_id)
        except ModelProviderError as provider_error:
            deferred_error = provider_error
            class DeferredWriterProvider:
                name = route.provider

                def generate(self, messages, model):
                    raise deferred_error
            provider = DeferredWriterProvider()
        draft = WriterProjectionService().render(db, chapter_id, values, provider=provider, model=route.model, settings=settings)
        db.commit(); db.refresh(draft)
    except WriterDomainError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc
    if draft.status in {WriterDraftStatus.FAILED, WriterDraftStatus.REJECTED}:
        code = (draft.validation_report or {}).get("issues", [{}])[0].get("code", "WRITER_RENDER_FAILED")
        raise HTTPException(status_code=409, detail={"code": code, "draft_id": draft.id})
    return writer_draft_payload(draft, include_content=True)


@router.get("/projects/{project_id}/chapters/{chapter_id}/writer/drafts")
def writer_drafts(project_id: str, chapter_id: str, db: Session = Depends(get_db)):
    _chapter_or_404(db, project_id, chapter_id)
    rows = db.scalars(select(ChapterWriterDraft).where(
        ChapterWriterDraft.project_id == project_id, ChapterWriterDraft.chapter_id == chapter_id,
    ).order_by(ChapterWriterDraft.version.desc(), ChapterWriterDraft.id.desc())).all()
    return [writer_draft_payload(item) for item in rows]


@router.get("/projects/{project_id}/writer-drafts/{draft_id}")
def writer_draft(project_id: str, draft_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return writer_draft_payload(_draft_or_404(db, project_id, draft_id), include_content=True)


@router.get("/projects/{project_id}/chapters/{chapter_id}/writer/drafts/{draft_id}")
def writer_draft_nested(project_id: str, chapter_id: str, draft_id: str, db: Session = Depends(get_db)):
    return writer_draft_payload(_draft_or_404(db, project_id, draft_id, chapter_id), include_content=True)


def _adopt(project_id: str, draft_id: str, payload: Payload | None, db: Session):
    require_project(db, project_id)
    _draft_or_404(db, project_id, draft_id)
    values = payload.model_dump() if payload else {}
    try:
        chapter = WriterProjectionService().adopt(
            db, draft_id, force_replace_untracked=bool(values.get("force_replace_untracked", False)),
            replace_title=bool(values.get("replace_title", False)),
        )
        db.commit(); db.refresh(chapter)
        return record_dict(chapter)
    except WriterDomainError as exc:
        if exc.code in {"WRITER_DRAFT_STALE", "WRITER_SOURCE_CHANGED", "WRITER_STYLE_SOURCE_CHANGED"}:
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc


@router.post("/projects/{project_id}/writer-drafts/{draft_id}/adopt")
def writer_adopt(project_id: str, draft_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    return _adopt(project_id, draft_id, payload, db)


@router.post("/projects/{project_id}/chapters/{chapter_id}/writer/drafts/{draft_id}/adopt")
def writer_adopt_nested(project_id: str, chapter_id: str, draft_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    _draft_or_404(db, project_id, draft_id, chapter_id)
    return _adopt(project_id, draft_id, payload, db)
