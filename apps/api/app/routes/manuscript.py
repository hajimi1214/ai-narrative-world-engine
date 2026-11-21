"""Download current manuscript text, never unadopted generation candidates."""
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..authoring import volume_chapter_ranges
from ..models import Chapter, ChapterQualityAssessment, VolumeContract
from ..quality import QualityAssessmentAudit, QualityAssessmentFreshnessChecker, QualityDomainError
from .common import get_db, require_project

router = APIRouter(tags=["manuscript"])


@router.get("/projects/{project_id}/manuscript")
def export_manuscript(
    project_id: str,
    format: Literal["txt", "md"] = "txt",
    volume_id: str | None = None,
    approved_only: bool = False,
    db: Session = Depends(get_db),
):
    project = require_project(db, project_id)
    volume = db.get(VolumeContract, volume_id) if volume_id else None
    if volume_id and (not volume or volume.project_id != project_id):
        raise HTTPException(status_code=404, detail="Volume not found")
    chapters = db.scalars(select(Chapter).where(
        Chapter.project_id == project_id, Chapter.active.is_(True),
        Chapter.content.is_not(None),
    ).order_by(Chapter.number, Chapter.id)).all()
    ranges = volume_chapter_ranges(db, volume) if volume else None
    selected = []
    for chapter in chapters:
        if not chapter.content.strip() or (ranges is not None and not any(start <= chapter.number <= end for start, end in ranges)):
            continue
        if approved_only:
            assessment = db.get(ChapterQualityAssessment, chapter.current_quality_assessment_id) if chapter.current_quality_assessment_id else None
            if assessment and (assessment.project_id != project_id or assessment.chapter_id != chapter.id):
                continue
            if chapter.quality_status != "PASS" or not assessment or str(getattr(assessment.status, "value", assessment.status)) != "PASS" or not assessment.active:
                continue
            if not QualityAssessmentFreshnessChecker().check(db, assessment, require_current=True)["fresh"]:
                continue
            try:
                QualityAssessmentAudit().audit(db, assessment.id)
            except QualityDomainError:
                continue
        selected.append(chapter)
    if not selected:
        raise HTTPException(status_code=409, detail={"code": "MANUSCRIPT_EMPTY", "message": "当前范围没有可导出的正文，请先保存正文或调整批准稿筛选。"})
    title = project.name + (f" · {volume.title or f'第 {volume.volume_number} 卷'}" if volume else "")
    heading = "# " if format == "md" else ""
    parts = [f"{heading}{title}"]
    for chapter in selected:
        label = chapter.title or f"第 {chapter.number} 章"
        if chapter.title and not chapter.title.startswith(f"第{chapter.number}章") and not chapter.title.startswith(f"第 {chapter.number} 章"):
            label = f"第 {chapter.number} 章 {chapter.title}"
        parts.append(f"{'## ' if format == 'md' else ''}{label}\n\n{chapter.content.strip()}")
    body = "\n\n".join(parts) + "\n"
    filename = title.replace("\r", " ").replace("\n", " ")[:120] + f".{format}"
    return Response(
        content=body.encode("utf-8-sig"), media_type="text/markdown" if format == "md" else "text/plain",
        headers={
            "Content-Disposition": f"attachment; filename=manuscript.{format}; filename*=UTF-8''{quote(filename, safe='')}",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Manuscript-Chapters": str(len(selected)),
        },
    )
