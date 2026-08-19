"""Real PostgreSQL coverage for Phase 16A projection serialization."""
import os
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    Project, Scene, SceneHistoryFeature, SceneStateCheckpoint, SceneStatus,
    SnapshotType, StoryThread, ThreadStatus, WorldSnapshot,
)
from app.historical import snapshot_fingerprint
from app.scaling import ProjectHistoryProjectionService, ProjectHistoryProjectionAudit


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires real PostgreSQL")


def _session():
    return sessionmaker(bind=create_engine(DATABASE_URL, pool_pre_ping=True), expire_on_commit=False)


def _fixture(db):
    project = Project(name="PG Scaling")
    db.add(project); db.flush()
    thread = StoryThread(project_id=project.id, title="Thread", type="MYSTERY", weight=1, progress=0.2, status=ThreadStatus.OPEN)
    db.add(thread); db.flush()
    scene = Scene(project_id=project.id, sequence=1, status=SceneStatus.OCCURRED, history_status="ACTIVE", participants=[], story_threads=[thread.id], location="loc")
    db.add(scene); db.flush()
    payload = {"project": {"id": project.id}, "scenes": [{"id": scene.id, "sequence": 1, "status": "OCCURRED", "history_status": "ACTIVE"}]}
    pre = WorldSnapshot(project_id=project.id, snapshot_type=SnapshotType.PRE_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    post = WorldSnapshot(project_id=project.id, snapshot_type=SnapshotType.POST_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    db.add_all([pre, post]); db.flush()
    db.add(SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=1, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2, version=1, active=True, origin="LEGACY", checkpoint_fingerprint="pg-cp"))
    db.commit()
    return project.id, scene.id


def test_postgres_projection_rebuild_and_audit():
    Session = _session()
    with Session() as db:
        project_id, _ = _fixture(db)
        ProjectHistoryProjectionService().rebuild(db, project_id)
        db.commit()
        ProjectHistoryProjectionAudit().audit(db, project_id)


def test_postgres_concurrent_rebuild_is_serialized():
    Session = _session()
    with Session() as db:
        project_id, _ = _fixture(db)
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            with Session() as db:
                barrier.wait(timeout=10)
                ProjectHistoryProjectionService().rebuild(db, project_id)
                db.commit()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=30)
    assert not errors
    with Session() as db:
        projection = ProjectHistoryProjectionService()._projection(db, project_id)
        assert projection and projection.built_through_sequence == 1
        assert db.scalar(select(__import__("sqlalchemy").func.count(SceneHistoryFeature.id)).where(SceneHistoryFeature.project_id == project_id)) == 1
        ProjectHistoryProjectionAudit().audit(db, project_id)
