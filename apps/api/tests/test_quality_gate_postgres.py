"""PostgreSQL-only quality locking proofs; SQLite is deliberately skipped."""
import os
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.ai.fake import FakeModelProvider
from app.models import (
    Chapter, ChapterQualityAssessment, ChapterQualityFinding, ChapterWriterDraft,
    ExecutionTrace, Project, WriterDraftOrigin, WriterDraftStatus, WriterPOVMode,
)
from app.quality import QualityContextBuilder, QualityGateService


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def _critic():
    return '{"decision":"PASS","scores":{"factual_grounding":95,"pov_compliance":95,"reveal_safety":95,"style_naturalness":95,"repetition":95,"pacing":95,"voice_consistency":95,"overall":95},"findings":[]}'


def _fixture(db):
    project = Project(name="pg-quality-concurrency", min_chapter_words=None, max_chapter_words=None, autonomy_settings={"quality_gate": {"require_critic": True}})
    db.add(project); db.flush()
    chapter = Chapter(project_id=project.id, number=1, title="Quality", source_scene_ids=[], content=None, active=True, structure_status="PROVISIONAL", structure_fingerprint="structure")
    db.add(chapter); db.flush()
    draft = ChapterWriterDraft(project_id=project.id, chapter_id=chapter.id, version=1, status=WriterDraftStatus.ADOPTED, origin=WriterDraftOrigin.WRITER, request_fingerprint="writer-request", chapter_structure_fingerprint="structure", chapter_source_fingerprint="source", writer_context_fingerprint="writer-context", source_structure_status="PROVISIONAL", source_scene_ids=[], source_manifest={"rendering_config": {"pov_mode": "OBJECTIVE", "pov_character_id": None, "explicit": {}}}, writing_bible_fingerprint="writer-default-v1", pov_mode=WriterPOVMode.OBJECTIVE, content="The door opens.", content_fingerprint="writer-content", word_count=3, scene_coverage=[], source_refs=[], validation_report={"valid": True})
    db.add(draft); db.flush()
    chapter.current_writer_draft_id = draft.id; chapter.writer_content_fingerprint = draft.content_fingerprint; chapter.writer_context_fingerprint = draft.writer_context_fingerprint; chapter.content = draft.content; chapter.word_count = draft.word_count
    db.commit(); return project.id, chapter.id, draft.id


def _context(self, db, chapter_id, request=None, *, draft=None, critic_provider=None, critic_model=None, require_current=True):
    chapter = db.get(Chapter, chapter_id)
    draft = draft or db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
    config = {"require_critic": True, "min_overall_score": 70, "max_major_findings": 0, "allow_minor_findings": True, "auto_repair_enabled": False, "max_repair_attempts": 1}
    return {"chapter": chapter, "writer_draft": draft, "prose": draft.content, "writer_safe_context": {"source_manifest": {"scenes": []}, "renderable_source_refs": []}, "writing_rules": {"protocol": "default"}, "anti_ai_rules": {"disabled_expressions": [], "warning_expressions": [], "frequency_limits": {}, "writing_principles": [], "future_risk_labels": []}, "anti_ai_bible": {"id": None, "version": None, "fingerprint": "anti-ai-default", "rules": {}}, "quality_contract": {"formal_mutation": False}, "source_fingerprints": {}, "quality_config": config, "quality_config_fingerprint": "quality-config", "quality_context_fingerprint": "quality-context", "renderable_source_refs": []}


def _cleanup(Session, project_id, chapter_id):
    with Session() as db:
        chapter = db.get(Chapter, chapter_id)
        if chapter:
            chapter.current_quality_assessment_id = None; chapter.current_writer_draft_id = None; db.flush()
        db.query(ChapterWriterDraft).filter(ChapterWriterDraft.chapter_id == chapter_id).update({ChapterWriterDraft.source_quality_assessment_id: None})
        db.query(ChapterQualityFinding).filter(ChapterQualityFinding.assessment_id.in_(select(ChapterQualityAssessment.id).where(ChapterQualityAssessment.chapter_id == chapter_id))).delete(synchronize_session=False)
        db.query(ChapterQualityAssessment).filter(ChapterQualityAssessment.chapter_id == chapter_id).delete()
        db.query(ExecutionTrace).filter(ExecutionTrace.project_id == project_id).delete()
        db.query(ChapterWriterDraft).filter(ChapterWriterDraft.chapter_id == chapter_id).delete()
        db.query(Chapter).filter(Chapter.id == chapter_id).delete()
        db.query(Project).filter(Project.id == project_id).delete(); db.commit()


def test_postgres_concurrent_assess_is_idempotent(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db: project_id, chapter_id, _ = _fixture(db)
    monkeypatch.setattr(QualityContextBuilder, "build", _context)
    barrier = threading.Barrier(2); ids = []; errors = []

    def worker():
        try:
            with Session() as db:
                barrier.wait()
                item = QualityGateService().assess(db, chapter_id, {"client_request_id": "same"}, provider=FakeModelProvider(_critic()), model="critic")
                db.commit(); ids.append(item.id)
        except Exception as exc: errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors and len(set(ids)) == 1
        with Session() as db:
            rows = db.scalars(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter_id)).all()
            assert len(rows) == 1 and rows[0].active
    finally:
        _cleanup(Session, project_id, chapter_id); engine.dispose()


def test_postgres_concurrent_approval_serializes_current(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project_id, chapter_id, _ = _fixture(db)
    monkeypatch.setattr(QualityContextBuilder, "build", _context)
    with Session() as db:
        assessment = QualityGateService().assess(db, chapter_id, {}, provider=FakeModelProvider(_critic()), model="critic"); db.commit(); assessment_id = assessment.id
    barrier = threading.Barrier(2); errors = []

    def worker():
        try:
            with Session() as db:
                barrier.wait(); QualityGateService().approve(db, assessment_id); db.commit()
        except Exception as exc: errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors
        with Session() as db:
            chapter = db.get(Chapter, chapter_id); assessment = db.get(ChapterQualityAssessment, assessment_id)
            assert chapter.current_quality_assessment_id == assessment.id and chapter.status == "QUALITY_APPROVED" and assessment.active
    finally:
        _cleanup(Session, project_id, chapter_id); engine.dispose()


def test_postgres_one_active_assessment_per_chapter(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db: project_id, chapter_id, _ = _fixture(db)
    monkeypatch.setattr(QualityContextBuilder, "build", _context)
    try:
        with Session() as db:
            first = QualityGateService().assess(db, chapter_id, {}, provider=FakeModelProvider(_critic()), model="critic"); db.commit()
            duplicate = ChapterQualityAssessment(project_id=first.project_id, chapter_id=first.chapter_id, writer_draft_id=first.writer_draft_id, version=2, status="PASS", active=True, request_fingerprint="other", content_fingerprint=first.content_fingerprint, writer_context_fingerprint=first.writer_context_fingerprint, chapter_source_fingerprint=first.chapter_source_fingerprint, anti_ai_bible_fingerprint=first.anti_ai_bible_fingerprint, writing_bible_fingerprint=first.writing_bible_fingerprint, quality_config=first.quality_config, quality_config_fingerprint=first.quality_config_fingerprint, quality_context_fingerprint="other-context", deterministic_report={}, critic_report={}, decision_reason_codes=[])
            db.add(duplicate)
            with pytest.raises(IntegrityError): db.flush()
    finally:
        _cleanup(Session, project_id, chapter_id); engine.dispose()


def test_postgres_assessment_version_is_unique(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db: project_id, chapter_id, _ = _fixture(db)
    monkeypatch.setattr(QualityContextBuilder, "build", _context)
    try:
        with Session() as db:
            first = QualityGateService().assess(db, chapter_id, {}, provider=FakeModelProvider(_critic()), model="critic"); db.commit()
            duplicate = ChapterQualityAssessment(project_id=first.project_id, chapter_id=first.chapter_id, writer_draft_id=first.writer_draft_id, version=first.version, status="PASS", active=False, request_fingerprint="other", content_fingerprint=first.content_fingerprint, writer_context_fingerprint=first.writer_context_fingerprint, chapter_source_fingerprint=first.chapter_source_fingerprint, anti_ai_bible_fingerprint=first.anti_ai_bible_fingerprint, writing_bible_fingerprint=first.writing_bible_fingerprint, quality_config=first.quality_config, quality_config_fingerprint=first.quality_config_fingerprint, quality_context_fingerprint="other-context", deterministic_report={}, critic_report={}, decision_reason_codes=[])
            db.add(duplicate)
            with pytest.raises(IntegrityError): db.flush()
    finally:
        _cleanup(Session, project_id, chapter_id); engine.dispose()
