"""PostgreSQL-only serialization coverage for the D2 structure projection."""
import os
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import NarrativeStructureSceneFeature, Project, Scene, SceneStatus
from app.narrative_structure import NarrativeStructureAudit, NarrativeStructureService
from app.narrative_structure_projection import (
    NarrativeStructureProjectionAudit, NarrativeStructureProjectionService,
)
from app.scaling import ProjectHistoryProjectionService


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"), reason="requires real PostgreSQL"
)


def _session():
    return sessionmaker(bind=create_engine(DATABASE_URL, pool_pre_ping=True), expire_on_commit=False)


def _fixture(db):
    project = Project(name="PG D2")
    db.add(project); db.flush()
    for sequence in range(1, 4):
        db.add(Scene(
            project_id=project.id, sequence=sequence, status=SceneStatus.OCCURRED,
            history_status="ACTIVE", participants=[], story_threads=["thread"], location="location",
            facts=[], result={},
        ))
    db.flush()
    NarrativeStructureService().sync(db, project.id)
    db.commit()
    return project.id


def test_postgres_structure_projection_rebuild_is_serialized():
    Session = _session()
    with Session() as db:
        project_id = _fixture(db)
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            with Session() as db:
                barrier.wait(timeout=10)
                NarrativeStructureProjectionService().rebuild(db, project_id)
                db.commit()
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    with Session() as db:
        rows = db.scalars(select(NarrativeStructureSceneFeature).where(
            NarrativeStructureSceneFeature.project_id == project_id,
            NarrativeStructureSceneFeature.active.is_(True),
        ).order_by(NarrativeStructureSceneFeature.sequence)).all()
        assert [row.sequence for row in rows] == [1, 2, 3]
        NarrativeStructureProjectionAudit().audit(db, project_id)
        NarrativeStructureAudit().audit(db, project_id)


def test_postgres_structure_append_and_rebuild_are_serialized():
    Session = _session()
    with Session() as db:
        project_id = _fixture(db)
        project = db.get(Project, project_id)
        scene = Scene(
            project_id=project_id, sequence=4, status=SceneStatus.OCCURRED,
            history_status="ACTIVE", participants=[], story_threads=["thread"],
            location="location", facts=[], result={},
        )
        db.add(scene); db.flush()
        ProjectHistoryProjectionService().sync_after_scene_commit(db, project_id, scene.id)
        scene_id = scene.id
        db.commit()
    barrier = threading.Barrier(2)
    errors = []

    def append_worker():
        try:
            with Session() as db:
                barrier.wait(timeout=10)
                NarrativeStructureProjectionService().sync_after_scene_commit(db, project_id, scene_id)
                db.commit()
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    def rebuild_worker():
        try:
            with Session() as db:
                barrier.wait(timeout=10)
                NarrativeStructureProjectionService().rebuild(db, project_id)
                db.commit()
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=append_worker), threading.Thread(target=rebuild_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    with Session() as db:
        rows = db.scalars(select(NarrativeStructureSceneFeature).where(
            NarrativeStructureSceneFeature.project_id == project_id,
            NarrativeStructureSceneFeature.active.is_(True),
        ).order_by(NarrativeStructureSceneFeature.sequence)).all()
        assert [row.sequence for row in rows] == [1, 2, 3, 4]
        NarrativeStructureProjectionAudit().audit(db, project_id)
        NarrativeStructureAudit().audit(db, project_id)
