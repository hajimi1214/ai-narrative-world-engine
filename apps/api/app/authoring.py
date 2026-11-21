"""Author edits and manuscript scopes, without changing world history."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import (
    AutoDirectorRun, AutoDirectorRunStatus, Chapter, ChapterPlanningWindow,
    ChapterQualityAssessment, ChapterWriterDraft, QualityAssessmentStatus, VolumeContract, VolumeContractStatus,
    WriterDraftOrigin, WriterDraftStatus,
)


def volume_chapter_ranges(db: Session, volume: VolumeContract) -> list[tuple[int, int]]:
    """Actual bounds and planning windows are authoritative; estimates are fallback."""
    windows = db.scalars(select(ChapterPlanningWindow).where(
        ChapterPlanningWindow.project_id == volume.project_id,
        ChapterPlanningWindow.volume_id == volume.id,
    )).all()
    ranges = [(item.start_chapter_number, item.end_chapter_number) for item in windows]
    if volume.actual_chapter_start is not None and volume.actual_chapter_end is not None:
        ranges.append((volume.actual_chapter_start, volume.actual_chapter_end))
    if not ranges and volume.estimated_chapter_start is not None and volume.estimated_chapter_end is not None:
        ranges.append((volume.estimated_chapter_start, volume.estimated_chapter_end))
    return ranges


def ensure_chapter_unsealed(db: Session, chapter: Chapter) -> None:
    from .writer import WriterDomainError

    for volume in db.scalars(select(VolumeContract).where(
        VolumeContract.project_id == chapter.project_id,
        VolumeContract.status == VolumeContractStatus.SEALED,
    )).all():
        if any(start <= chapter.number <= end for start, end in volume_chapter_ranges(db, volume)):
            raise WriterDomainError("VOLUME_SEALED", {"volume_id": volume.id})


class AuthorDraftService:
    def save(self, db: Session, chapter_id: str, request: dict[str, Any]) -> ChapterWriterDraft:
        from .writer import (
            WriterChapterSourceBuilder, WriterContextBuilder, WriterDomainError,
            WriterProjectionService, WriterWordCounter, _fp, _value,
        )

        content = str(request["content"]).replace("\r\n", "\n").replace("\r", "\n")
        if not content.strip():
            raise WriterDomainError("AUTHOR_CONTENT_EMPTY")
        request_fp = _fp({**request, "content": content}, "author-edit-request-v1")
        key = request.get("client_request_id")
        if key:
            existing = db.scalar(select(ChapterWriterDraft).where(
                ChapterWriterDraft.chapter_id == chapter_id,
                ChapterWriterDraft.client_request_id == key,
            ))
            if existing:
                if existing.request_fingerprint != request_fp:
                    raise WriterDomainError("WRITER_REQUEST_MISMATCH")
                return existing

        # Compare-and-lock before allocating a version. This also obtains a
        # SQLite write lock; SELECT FOR UPDATE alone has no effect on SQLite.
        expected = request["expected_current_draft_id"]
        matched = db.execute(update(Chapter).where(
            Chapter.id == chapter_id,
            Chapter.current_writer_draft_id == expected,
        ).values(current_writer_draft_id=expected), execution_options={"synchronize_session": False})
        if matched.rowcount != 1:
            raise WriterDomainError("AUTHOR_EDIT_CONFLICT")
        chapter = db.scalar(select(Chapter).where(Chapter.id == chapter_id).execution_options(populate_existing=True))
        if not chapter:
            raise WriterDomainError("CHAPTER_NOT_FOUND")
        ensure_chapter_unsealed(db, chapter)
        running = db.scalar(select(AutoDirectorRun.id).where(
            AutoDirectorRun.project_id == chapter.project_id,
            AutoDirectorRun.status.in_([AutoDirectorRunStatus.CREATED, AutoDirectorRunStatus.RUNNING]),
        ).limit(1))
        if running:
            raise WriterDomainError("AUTHOR_EDIT_REQUIRES_PAUSE")
        parent = db.get(ChapterWriterDraft, request["base_draft_id"])
        if not parent or parent.chapter_id != chapter.id or parent.project_id != chapter.project_id:
            raise WriterDomainError("WRITER_DRAFT_NOT_FOUND")
        if not parent.content or _value(parent.status) in {"GENERATING", "FAILED", "REJECTED", "STALE"}:
            raise WriterDomainError("WRITER_DRAFT_NOT_VALIDATED")

        source = WriterChapterSourceBuilder().build(db, chapter.id)
        config = dict((parent.source_manifest or {}).get("rendering_config") or {})
        render_request = {"pov_mode": config.get("pov_mode", _value(parent.pov_mode)), "pov_character_id": config.get("pov_character_id", parent.pov_character_id)}
        for field in ("target_words", "min_words", "max_words"):
            if config.get("explicit", {}).get(field):
                render_request[field] = config.get(field)
        context = WriterContextBuilder().build(db, source, render_request)
        now = datetime.utcnow()
        report = {
            "valid": True, "issues": [], "author_edit": True,
            "requires_quality_assessment": True,
            # These are inherited scene/task claims, never a new semantic
            # approval. The edited text must receive its own quality report.
            "task_coverage": (parent.validation_report or {}).get("task_coverage", []),
            "task_forbidden_hits": [],
        }
        version = (db.scalar(select(func.max(ChapterWriterDraft.version)).where(ChapterWriterDraft.chapter_id == chapter.id)) or 0) + 1
        draft = ChapterWriterDraft(
            project_id=chapter.project_id, chapter_id=chapter.id, version=version,
            origin=WriterDraftOrigin.AUTHOR_EDIT, status=WriterDraftStatus.VALIDATED,
            client_request_id=key, request_fingerprint=request_fp,
            chapter_structure_fingerprint=source["structure_fingerprint"],
            chapter_source_fingerprint=source["source_fingerprint"],
            writer_context_fingerprint=context["writer_context_fingerprint"],
            source_structure_status=_value(chapter.structure_status),
            source_scene_ids=source["source_scene_ids"],
            source_manifest={**context["source_manifest"], "planning_task": context.get("planning_task")},
            writing_bible_id=context["writing_bible"].id if context.get("writing_bible") else None,
            writing_bible_version=context["writing_bible"].version if context.get("writing_bible") else None,
            writing_bible_fingerprint=context["fingerprints"]["writing_bible"],
            pov_mode=context["pov_mode"], pov_character_id=context["pov_character_id"],
            provider="author", title_candidate=chapter.title or parent.title_candidate,
            content=content, content_fingerprint=_fp(content, "writer-content-v1"),
            word_count=WriterWordCounter().count(content),
            scene_coverage=source["source_scene_ids"], source_refs=[],
            validation_report=report, parent_draft_id=parent.id, completed_at=now,
        )
        db.add(draft)
        db.flush()
        # Adopt here means update the tracked working text. It resets formal
        # quality approval; only a fresh assessment can approve this version.
        WriterProjectionService().adopt(db, draft.id)
        for assessment in db.scalars(select(ChapterQualityAssessment).where(
            ChapterQualityAssessment.chapter_id == chapter.id,
            ChapterQualityAssessment.active.is_(True),
        )).all():
            assessment.active = False
            if _value(assessment.status) != "FAILED":
                assessment.status = QualityAssessmentStatus.STALE
                assessment.stale_at = now
        chapter.quality_status = "STALE"
        chapter.current_quality_assessment_id = None
        chapter.quality_content_fingerprint = None
        chapter.quality_approved_at = None
        chapter.quality_report = {}
        db.flush()
        return draft
