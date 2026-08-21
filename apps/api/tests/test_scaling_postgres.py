"""Real PostgreSQL coverage for Phase 16A projection serialization."""
import os
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    Character, CharacterMemory, CurrentStateChangeHead, Project, Scene, SceneHistoryFeature, SceneStateCheckpoint, SceneStatus,
    SnapshotType, StoryThread, ThreadStatus, TimelineEvent, TimelineEventType, TimelineOrigin, WorldSnapshot, new_id,
)
from app.historical import snapshot_fingerprint
from app.scaling import ProjectHistoryProjectionService, ProjectHistoryProjectionAudit
from app.director import StoryGravityContextBuilder
from app.retrieval_index import CognitionRetrievalProjectionService


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


def test_postgres_cold_start_scene_sync_creates_ready_projection():
    Session = _session()
    with Session() as db:
        project_id, scene_id = _fixture(db)
        service = ProjectHistoryProjectionService()
        service.sync_after_scene_commit(db, project_id, scene_id)
        db.commit()
        projection = service._projection(db, project_id)
        assert projection and getattr(projection.status, "value", projection.status) == "READY"
        assert projection.built_through_sequence == projection.active_scene_count == 1
        assert projection.last_scene_id == scene_id
        assert projection.thread_stats["__projection_meta__"]["active_character_ids"] == []
        ProjectHistoryProjectionAudit().audit(db, project_id)


def test_postgres_story_gravity_fast_context_uses_ready_cognition_index(monkeypatch):
    Session = _session()
    with Session() as db:
        project_id, scene_id = _fixture(db)
        character = Character(project_id=project_id, name="Actor")
        db.add(character); db.flush()
        scene = db.get(Scene, scene_id)
        scene.participants = [character.id]
        memory = CharacterMemory(
            character_id=character.id, content="indexed memory", importance=.5,
            emotional_weight=0, confidence=1, distortion={}, source_scene=None,
        )
        db.add(memory); db.flush()
        CognitionRetrievalProjectionService().rebuild(db, project_id)
        ProjectHistoryProjectionService().rebuild(db, project_id)
        db.commit()
        monkeypatch.setattr(
            "app.scaling.ActiveCharacterCognitionReader.knowledge",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LEGACY_PATH_USED")),
        )
        monkeypatch.setattr(
            "app.scaling.ActiveCharacterCognitionReader.memories",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LEGACY_PATH_USED")),
        )
        builder = StoryGravityContextBuilder()
        context = builder.build(db, project_id)
        assert context["protocol_version"] == "story-gravity-context-v2"
        assert builder.last_route == "FAST_HISTORY_PROJECTION"
        assert [item["memory_id"] for item in context["memories"]] == [memory.id]


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


def test_postgres_current_state_head_rebuilds_to_latest_active_event():
    Session = _session()
    with Session() as db:
        project_id, scene_id = _fixture(db)
        events = []
        for sequence in (1, 10, 20):
            event = TimelineEvent(
                project_id=project_id, event_type=TimelineEventType.STATE_CHANGE,
                source_type="SCENE", source_id=scene_id, source_key=f"head:{sequence}",
                scene_id=scene_id, sequence=sequence, ordinal=1,
                origin=TimelineOrigin.LEGACY_BACKFILL, active=True,
                target_type="CHARACTER", target_id="character", path="/physical_state/injured",
                before_value=False, after_value=True, structured_payload={}, event_fingerprint=f"head-{sequence}",
            )
            db.add(event); events.append(event)
        db.flush()
        service = ProjectHistoryProjectionService()
        service.rebuild(db, project_id)
        db.commit()
        head = db.scalar(select(CurrentStateChangeHead).where(CurrentStateChangeHead.project_id == project_id))
        assert head.timeline_event_id == events[-1].id
        events[-1].active = False
        service.rebuild(db, project_id)
        db.commit()
        head = db.scalar(select(CurrentStateChangeHead).where(CurrentStateChangeHead.project_id == project_id))
        assert head.timeline_event_id == events[-2].id
        assert db.get(TimelineEvent, events[-1].id) is not None


def test_postgres_append_uses_stored_head_accumulator_without_rescanning_prefix():
    Session = _session()
    with Session() as db:
        project_id, scene_id = _fixture(db)
        events, heads = [], []
        for index in range(1_000):
            event_id = new_id()
            events.append({
                "id": event_id, "project_id": project_id,
                "event_type": TimelineEventType.STATE_CHANGE,
                "source_type": "SCENE", "source_id": scene_id,
                "source_key": f"pg-head-seed:{index}", "scene_id": scene_id,
                "sequence": 1, "ordinal": index, "origin": TimelineOrigin.LEGACY_BACKFILL,
                "active": True, "target_type": "CHARACTER", "target_id": f"target-{index}",
                "path": "/current_state/value", "before_value": None, "after_value": index,
                "structured_payload": {}, "event_fingerprint": f"pg-head-fingerprint:{index}",
            })
            heads.append({
                "id": new_id(), "project_id": project_id, "timeline_event_id": event_id,
                "scene_id": scene_id, "sequence": 1, "ordinal": index,
                "target_type": "CHARACTER", "target_id": f"target-{index}",
                "path": "/current_state/value", "event_fingerprint": f"pg-head-fingerprint:{index}",
            })
        db.execute(TimelineEvent.__table__.insert(), events)
        db.execute(CurrentStateChangeHead.__table__.insert(), heads)
        service = ProjectHistoryProjectionService()
        service.rebuild(db, project_id)
        scene = Scene(project_id=project_id, sequence=2, status=SceneStatus.OCCURRED,
                      history_status="ACTIVE", participants=[], story_threads=[], location="loc")
        db.add(scene); db.flush()
        payload = {"project": {"id": project_id}, "scenes": [{"id": scene.id, "sequence": 2}]}
        pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.PRE_SCENE_STATE,
                            payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.POST_SCENE_STATE,
                             payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        db.add_all([pre, post]); db.flush()
        db.add(SceneStateCheckpoint(project_id=project_id, scene_id=scene.id, sequence=2,
               pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id,
               capture_protocol_version=2, version=1, active=True, origin="LEGACY",
               checkpoint_fingerprint="pg-append"))
        db.flush()
        statements = []

        def capture(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement.lower())

        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            service.sync_after_scene_commit(db, project_id, scene.id)
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
        assert not any("from current_state_change_heads" in statement for statement in statements)
        assert not any(
            "from timeline_events" in statement and "timeline_events.scene_id" not in statement
            for statement in statements
        )
        db.commit()
        ProjectHistoryProjectionAudit().audit(db, project_id)
