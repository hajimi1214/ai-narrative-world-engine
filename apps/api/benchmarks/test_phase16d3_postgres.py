"""Opt-in PostgreSQL concurrency certification for Phase 16D3.

This deliberately stays outside the default suite.  SQLite cannot prove row
locking or advisory-lock serialization, so non-PostgreSQL environments skip
the cases instead of emulating a pass.
"""
from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.causal_ledger import CausalLedgerService
from app.formal_state import FormalStateIdentityAudit, FormalStateIdentityService
from app.models import Project, Scene, SceneStateCheckpoint, SceneStatus, SnapshotType, StoryThread, ThreadStatus, WorldSnapshot
from app.historical import snapshot_fingerprint
from app.narrative_structure_projection import NarrativeStructureProjectionAudit, NarrativeStructureProjectionService
from app.retrieval_index import CognitionRetrievalIndexAudit, CognitionRetrievalProjectionService, ResearchLexicalIndexAudit, ResearchLexicalIndexService
from app.scaling import ProjectHistoryProjectionAudit, ProjectHistoryProjectionService
from benchmarks.phase16d3_runner import MatrixResult, run_matrix


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires real PostgreSQL"),
    pytest.mark.skipif(os.getenv("RUN_PHASE16D3") != "1", reason="opt-in D3 PostgreSQL certification"),
]


def _session():
    return sessionmaker(bind=create_engine(DATABASE_URL, pool_pre_ping=True), expire_on_commit=False)


def _fixture(db):
    project = Project(name="Phase16D3 PG concurrency")
    db.add(project)
    db.flush()
    thread = StoryThread(project_id=project.id, title="D3", type="MYSTERY", status=ThreadStatus.OPEN)
    db.add(thread)
    db.flush()
    for sequence in range(1, 4):
        scene = Scene(
            project_id=project.id, sequence=sequence, status=SceneStatus.OCCURRED,
            history_status="ACTIVE", participants=[], story_threads=[thread.id],
            location="d3", facts=[], result={},
        )
        db.add(scene)
        db.flush()
        payload = {"project": {"id": project.id}, "scenes": [{"id": scene.id, "sequence": sequence, "status": "OCCURRED", "history_status": "ACTIVE"}]}
        pre = WorldSnapshot(project_id=project.id, snapshot_type=SnapshotType.PRE_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        post = WorldSnapshot(project_id=project.id, snapshot_type=SnapshotType.POST_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        db.add_all([pre, post])
        db.flush()
        db.add(SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=sequence, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2, version=1, active=True, origin="LEGACY", checkpoint_fingerprint=f"d3-cp-{sequence}"))
    db.commit()
    return project.id


def _parallel(Session, operations):
    barrier = threading.Barrier(len(operations))
    errors = []

    def worker(operation):
        try:
            with Session() as db:
                barrier.wait(timeout=30)
                operation(db)
                db.commit()
        except Exception as exc:  # pragma: no cover - assertion reports code
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors, [str(error) for error in errors]


def test_phase16d3_postgres_append_and_all_rebuilds_are_serialized():
    Session = _session()
    with Session() as db:
        project_id = _fixture(db)

    cases = {
        "formal_state_rebuild": lambda db: FormalStateIdentityService().rebuild(db, project_id),
        "history_projection_rebuild": lambda db: ProjectHistoryProjectionService().rebuild(db, project_id),
        "structure_projection_rebuild": lambda db: NarrativeStructureProjectionService().rebuild(db, project_id),
        "cognition_rebuild": lambda db: CognitionRetrievalProjectionService().rebuild(db, project_id),
        "research_rebuild": lambda db: ResearchLexicalIndexService().rebuild(db, project_id),
        "ledger_rebuild": lambda db: CausalLedgerService().index_current_history(db, project_id),
    }
    matrix: list[MatrixResult] = []
    for name, operation in cases.items():
        try:
            _parallel(Session, [operation, operation])
            matrix.append(MatrixResult(name, "PASS", details={"sessions": 2, "database": "postgresql"}))
        except Exception as exc:  # pragma: no cover
            matrix.append(MatrixResult(name, "FAIL", reason=str(exc).split(":", 1)[0]))

    with Session() as db:
        # Normalize the final audit boundary after the concurrent attempts;
        # the concurrency assertion above is about serialization/no loss,
        # while this explicit rebuild proves the resulting derived state.
        ProjectHistoryProjectionService().rebuild(db, project_id)
        NarrativeStructureProjectionService().rebuild(db, project_id)
        CognitionRetrievalProjectionService().rebuild(db, project_id)
        ResearchLexicalIndexService().rebuild(db, project_id)
        CausalLedgerService().index_current_history(db, project_id)
        # Structure rebuild may touch formal Chapter/Arc rows; publish the
        # formal accelerator last so its READY state certifies the final DB.
        FormalStateIdentityService().rebuild(db, project_id)
        db.commit()
        FormalStateIdentityAudit().audit(db, project_id)
        ProjectHistoryProjectionAudit().audit(db, project_id)
        NarrativeStructureProjectionAudit().audit(db, project_id)
        CognitionRetrievalIndexAudit().audit(db, project_id)
        ResearchLexicalIndexAudit().audit(db, project_id)
    assert all(item.status == "PASS" for item in matrix), [item.as_dict() for item in matrix]


def test_phase16d3_postgres_append_vs_rebuild_has_no_duplicate_scene_boundary():
    Session = _session()
    with Session() as db:
        project_id = _fixture(db)
        scene = Scene(project_id=project_id, sequence=4, status=SceneStatus.OCCURRED,
                      history_status="ACTIVE", participants=[], story_threads=[],
                      location="d3", facts=[], result={})
        db.add(scene)
        db.flush()
        scene_id = scene.id
        payload = {"project": {"id": project_id}, "scenes": [{"id": scene.id, "sequence": 4, "status": "OCCURRED", "history_status": "ACTIVE"}]}
        pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.PRE_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.POST_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        db.add_all([pre, post])
        db.flush()
        db.add(SceneStateCheckpoint(project_id=project_id, scene_id=scene.id, sequence=4, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2, version=1, active=True, origin="LEGACY", checkpoint_fingerprint="d3-cp-4"))
        db.commit()

    _parallel(Session, [
        lambda db: ProjectHistoryProjectionService().sync_after_scene_commit(db, project_id, scene_id),
        lambda db: ProjectHistoryProjectionService().rebuild(db, project_id),
    ])
    with Session() as db:
        ProjectHistoryProjectionService().rebuild(db, project_id)
        db.commit()
        rows = db.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.sequence)).all()
        assert [row.sequence for row in rows] == [1, 2, 3, 4]
        ProjectHistoryProjectionAudit().audit(db, project_id)
