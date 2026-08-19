"""PostgreSQL-only Writer locking proofs.

The normal suite intentionally uses isolated SQLite fixtures.  These tests run
only when DATABASE_URL points at PostgreSQL so SQLite cannot masquerade as a
concurrency proof.
"""
import os
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.ai.fake import FakeModelProvider
from app.models import Chapter, ChapterWriterDraft, ExecutionTrace, Project, WriterDraftStatus, WriterPOVMode
from app.writer import WriterChapterSourceBuilder, WriterContextBuilder, WriterProjectionService


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def _context(chapter):
    config = {"target_words": None, "min_words": None, "max_words": None, "explicit": {"target_words": False, "min_words": False, "max_words": False}, "visibility_protocol": "writer-visibility-v2", "secret_policy": "formal-visible-history-v1", "pov_mode": "OBJECTIVE", "pov_character_id": None}
    return {
        "writing_rules": {"protocol": "writer-default-v1"},
        "chapter": {"id": chapter.id, "number": chapter.number, "title": chapter.title, "structure_status": "PROVISIONAL"},
        "formal_history": {"scenes": []},
        "pov_subjective_context": [],
        "entity_labels": {},
        "rendering_contract": {"pov_mode": "OBJECTIVE", "pov_character_id": None, "grounding": "structured references only", "allowed_reveal_ids": [], "no_formal_mutation": True, "visibility_protocol": config["visibility_protocol"], "secret_policy": config["secret_policy"]},
        "source_manifest": {"protocol": "writer-chapter-source-v1", "chapter_id": chapter.id, "chapter_structure_fingerprint": "structure", "structure_status": "PROVISIONAL", "scenes": [], "rendering_config": config},
        "fingerprints": {"chapter_structure": "structure", "chapter_source": "source", "writing_bible": "writer-default-v1"},
        "writing_bible": None,
        "pov_mode": WriterPOVMode.OBJECTIVE,
        "pov_character_id": None,
        "renderable_source_refs": [],
        "writer_context_fingerprint": "context",
    }


def _fixture(db):
    project = Project(name="pg-writer-concurrency")
    db.add(project); db.flush()
    chapter = Chapter(project_id=project.id, number=1, title=None, active=True, structure_status="PROVISIONAL", structure_fingerprint="structure")
    db.add(chapter); db.commit()
    return project.id, chapter.id


def _source_for(db, chapter_id, template):
    source = dict(template)
    source["chapter"] = db.get(Chapter, chapter_id)
    return source


def test_postgres_concurrent_render_allocates_unique_versions(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as setup:
        project_id, chapter_id = _fixture(setup)
        source_chapter = setup.get(Chapter, chapter_id)
    source = {"chapter": source_chapter, "source_scene_ids": [], "scenes": [], "manifest": {"protocol": "writer-chapter-source-v1", "chapter_id": chapter_id, "chapter_structure_fingerprint": "structure", "structure_status": "PROVISIONAL", "scenes": []}, "source_fingerprint": "source", "structure_fingerprint": "structure"}
    monkeypatch.setattr(WriterChapterSourceBuilder, "build", lambda self, db, chapter_id, **kwargs: _source_for(db, chapter_id, source))
    monkeypatch.setattr(WriterContextBuilder, "build", lambda self, db, source, request=None: _context(source["chapter"]))
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(key):
        try:
            with Session() as db:
                barrier.wait()
                draft = WriterProjectionService().render(db, chapter_id, {"pov_mode": "OBJECTIVE", "client_request_id": key}, provider=FakeModelProvider('{"chapter_title":null,"prose":"parallel","scene_coverage":[],"source_refs":[],"pov_character_id":null}'), model="fake")
                db.commit(); results.append(draft.id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"pg-{i}",)) for i in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors
        with Session() as db:
            drafts = db.scalars(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == chapter_id).order_by(ChapterWriterDraft.version)).all()
            assert [item.version for item in drafts] == [1, 2]
    finally:
        with Session() as db:
            chapter = db.get(Chapter, chapter_id)
            if chapter is not None:
                chapter.current_writer_draft_id = None
                db.flush()
            db.query(ExecutionTrace).filter(ExecutionTrace.project_id == project_id).delete()
            db.query(ChapterWriterDraft).filter(ChapterWriterDraft.chapter_id == chapter_id).delete()
            db.query(Chapter).filter(Chapter.id == chapter_id).delete()
            db.query(Project).filter(Project.id == project_id).delete()
            db.commit()
        engine.dispose()


def test_postgres_concurrent_adopt_serializes_current_draft(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as setup:
        project_id, chapter_id = _fixture(setup)
        chapter = setup.get(Chapter, chapter_id)
        source = {"chapter": chapter, "source_scene_ids": [], "scenes": [], "manifest": {"protocol": "writer-chapter-source-v1", "chapter_id": chapter_id, "chapter_structure_fingerprint": "structure", "structure_status": "PROVISIONAL", "scenes": []}, "source_fingerprint": "source", "structure_fingerprint": "structure"}
    monkeypatch.setattr(WriterChapterSourceBuilder, "build", lambda self, db, chapter_id, **kwargs: _source_for(db, chapter_id, source))
    monkeypatch.setattr(WriterContextBuilder, "build", lambda self, db, source, request=None: _context(source["chapter"]))
    with Session() as db:
        service = WriterProjectionService()
        draft1 = service.render(db, chapter_id, {"pov_mode": "OBJECTIVE", "client_request_id": "adopt-1"}, provider=FakeModelProvider('{"chapter_title":null,"prose":"one","scene_coverage":[],"source_refs":[],"pov_character_id":null}'), model="fake")
        draft2 = service.render(db, chapter_id, {"pov_mode": "OBJECTIVE", "client_request_id": "adopt-2"}, provider=FakeModelProvider('{"chapter_title":null,"prose":"two","scene_coverage":[],"source_refs":[],"pov_character_id":null}'), model="fake")
        db.commit(); draft_ids = [draft1.id, draft2.id]
    barrier = threading.Barrier(2)
    errors = []

    def adopt_worker(draft_id):
        try:
            with Session() as db:
                barrier.wait()
                WriterProjectionService().adopt(db, draft_id)
                db.commit()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=adopt_worker, args=(draft_id,)) for draft_id in draft_ids]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors
        with Session() as db:
            chapter = db.get(Chapter, chapter_id)
            adopted = db.scalars(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == chapter_id, ChapterWriterDraft.status == WriterDraftStatus.ADOPTED)).all()
            assert chapter.current_writer_draft_id in {item.id for item in adopted}
            assert len(adopted) == 1
    finally:
        with Session() as db:
            chapter = db.get(Chapter, chapter_id)
            if chapter is not None:
                chapter.current_writer_draft_id = None
                db.flush()
            db.query(ExecutionTrace).filter(ExecutionTrace.project_id == project_id).delete()
            db.query(ChapterWriterDraft).filter(ChapterWriterDraft.chapter_id == chapter_id).delete()
            db.query(Chapter).filter(Chapter.id == chapter_id).delete()
            db.query(Project).filter(Project.id == project_id).delete()
            db.commit()
        engine.dispose()
