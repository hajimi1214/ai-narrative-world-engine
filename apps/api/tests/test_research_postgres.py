"""PostgreSQL-only research revision and ingestion locking proofs."""
import os
import threading

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Project, ResearchChunk, ResearchDocument, ResearchDocumentRevision
from app.research import ResearchIngestionService


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def _project(db):
    project = Project(name="pg-research", research_settings={})
    db.add(project); db.commit(); return project.id


def _cleanup(Session, project_id):
    with Session() as db:
        db.execute(delete(ResearchChunk).where(ResearchChunk.project_id == project_id))
        db.execute(delete(ResearchDocumentRevision).where(ResearchDocumentRevision.project_id == project_id))
        db.execute(delete(ResearchDocument).where(ResearchDocument.project_id == project_id))
        db.execute(delete(Project).where(Project.id == project_id)); db.commit()


def test_postgres_concurrent_ingest_same_request_is_idempotent():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project_id = _project(db)
    barrier = threading.Barrier(2); ids, errors = [], []
    def worker():
        try:
            with Session() as db:
                barrier.wait()
                result = ResearchIngestionService().ingest(db, project_id, title="Concurrent", content="steam engine", client_request_id="same")
                db.commit(); ids.append((result.document.id, result.revision.id))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors and len(set(ids)) == 1
        with Session() as db:
            revisions = db.scalars(select(ResearchDocumentRevision).where(ResearchDocumentRevision.project_id == project_id)).all()
            assert len(revisions) == 1 and revisions[0].active
    finally:
        _cleanup(Session, project_id); engine.dispose()


def test_postgres_concurrent_revision_keeps_single_active_version():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project_id = _project(db)
        first = ResearchIngestionService().ingest(db, project_id, title="Doc", content="old")
        db.commit(); document_id = first.document.id
    barrier = threading.Barrier(2); ids, errors = [], []
    def worker():
        try:
            with Session() as db:
                barrier.wait()
                result = ResearchIngestionService().add_revision(db, document_id, content="new")
                db.commit(); ids.append(result.revision.id)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors and len(set(ids)) == 1
        with Session() as db:
            rows = db.scalars(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document_id).order_by(ResearchDocumentRevision.version)).all()
            assert [row.version for row in rows] == [1, 2] and [row.active for row in rows] == [False, True]
    finally:
        _cleanup(Session, project_id); engine.dispose()


def test_postgres_partial_active_revision_unique():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project_id = _project(db)
        first = ResearchIngestionService().ingest(db, project_id, title="Doc", content="old")
        db.commit()
    try:
        with Session() as db:
            duplicate = ResearchDocumentRevision(
                project_id=project_id,
                document_id=first.document.id,
                version=2,
                active=True,
                content="duplicate",
                content_fingerprint="duplicate",
                normalized_fingerprint="duplicate",
                ingestion_config=first.revision.ingestion_config,
                ingestion_config_fingerprint=first.revision.ingestion_config_fingerprint,
            )
            db.add(duplicate)
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()
    finally:
        _cleanup(Session, project_id); engine.dispose()
