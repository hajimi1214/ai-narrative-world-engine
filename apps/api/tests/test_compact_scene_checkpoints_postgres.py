"""Real PostgreSQL proofs for compact checkpoint storage and head serialization."""
from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.models import Project, ProjectWorldSnapshotHead, SnapshotType, WorldSnapshot
from app.snapshot_storage import (
    CompactSnapshotService,
    ProjectWorldSnapshotHeadService,
    SnapshotDeltaCodec,
    SnapshotPayloadResolver,
    snapshot_fingerprint,
)


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires real PostgreSQL")


def _session():
    return sessionmaker(bind=create_engine(DATABASE_URL, pool_pre_ping=True), expire_on_commit=False)


def _payload(project_id, value=0):
    return {
        "project": {"id": project_id, "status": "DRAFT", "current_world_time": None},
        "canon_facts": [], "world_entities": [{"id": "entity", "project_id": project_id, "profile": {"value": value}}],
        "characters": [], "character_knowledge": [], "character_memories": [], "reveal_constraints": [],
        "story_threads": [], "story_arcs": [], "scenes": [], "chapters": [],
    }


def _cleanup(Session, project_id):
    with Session() as db:
        db.execute(delete(ProjectWorldSnapshotHead).where(ProjectWorldSnapshotHead.project_id == project_id))
        db.execute(delete(WorldSnapshot).where(WorldSnapshot.project_id == project_id))
        db.execute(delete(Project).where(Project.id == project_id))
        db.commit()


def test_postgres_compact_chain_materializes_and_detects_tamper():
    Session = _session()
    with Session() as db:
        project = Project(name="pg compact chain")
        db.add(project); db.flush()
        payload = _payload(project.id)
        service = CompactSnapshotService()
        anchor = service.anchor(db, project.id, SnapshotType.BASELINE, payload)
        delta_payload = _payload(project.id, 2)
        delta = service.delta(db, project.id, SnapshotType.POST_SCENE_STATE, anchor, SnapshotDeltaCodec().diff(payload, delta_payload), snapshot_fingerprint(delta_payload))
        reference = service.reference(db, project.id, SnapshotType.PRE_SCENE_STATE, delta)
        final_payload = _payload(project.id, 3)
        final = service.delta(db, project.id, SnapshotType.POST_SCENE_STATE, reference, SnapshotDeltaCodec().diff(delta_payload, final_payload), snapshot_fingerprint(final_payload))
        ProjectWorldSnapshotHeadService().update(db, project.id, final, source_type="PG_TEST")
        db.commit()
        project_id, final_id = project.id, final.id
    try:
        with Session() as db:
            final = db.get(WorldSnapshot, final_id)
            assert SnapshotPayloadResolver().materialize(db, final) == final_payload
            ProjectWorldSnapshotHeadService().audit(db, project_id)
            final.storage_fingerprint = "tampered"
            db.commit()
        with Session() as db:
            with pytest.raises(ValueError, match="SNAPSHOT_CHAIN_INVALID"):
                SnapshotPayloadResolver().validate_chain(db, db.get(WorldSnapshot, final_id))
    finally:
        _cleanup(Session, project_id)


def test_postgres_concurrent_head_update_leaves_one_auditable_head():
    Session = _session()
    with Session() as db:
        project = Project(name="pg compact head")
        db.add(project); db.flush()
        service = CompactSnapshotService()
        first = service.anchor(db, project.id, SnapshotType.BASELINE, _payload(project.id, 1))
        second = service.anchor(db, project.id, SnapshotType.BASELINE, _payload(project.id, 2))
        db.commit()
        project_id, snapshot_ids = project.id, {first.id, second.id}
    barrier, errors = threading.Barrier(2), []

    def update(snapshot_id):
        try:
            with Session() as db:
                barrier.wait(timeout=10)
                ProjectWorldSnapshotHeadService().update(db, project_id, db.get(WorldSnapshot, snapshot_id), source_type="PG_CONCURRENT")
                db.commit()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=update, args=(snapshot_id,)) for snapshot_id in sorted(snapshot_ids)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=30)
    try:
        assert not errors
        with Session() as db:
            heads = db.scalars(select(ProjectWorldSnapshotHead).where(ProjectWorldSnapshotHead.project_id == project_id)).all()
            assert len(heads) == 1 and heads[0].snapshot_id in snapshot_ids
            ProjectWorldSnapshotHeadService().audit(db, project_id)
    finally:
        _cleanup(Session, project_id)
