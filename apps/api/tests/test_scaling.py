from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.director import DirectorCandidateEngine, StoryGravityContextBuilder, StoryGravityEngine
from app.historical import snapshot_fingerprint
from app.models import (
    Character, CurrentStateChangeHead, Project, ProjectHistoryProjection, Scene,
    SceneHistoryFeature, SceneStateCheckpoint, SceneStatus, SceneCheckpointOrigin, StoryThread, ThreadStatus,
    TimelineEvent, TimelineEventType, TimelineOrigin, WorldSnapshot, SnapshotType, new_id, HistoryProjectionStatus,
)
from app.scaling import (
    HistoryProjectionFingerprintBuilder, ProjectHistoryProjectionAudit,
    ProjectHistoryProjectionService, SceneHistoryFeatureAudit,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def make_history(db, count=4):
    project = Project(name="Scaling")
    db.add(project); db.flush()
    character = Character(project_id=project.id, name="A", current_state={"location_id": "loc"}, goals={"goal": True}, narrative_relevance={"score": 1})
    thread = StoryThread(project_id=project.id, title="Thread", type="MYSTERY", weight=5, progress=0.2, status=ThreadStatus.OPEN)
    db.add_all([character, thread]); db.flush()
    scenes = []
    for sequence in range(1, count + 1):
        scene = Scene(project_id=project.id, sequence=sequence, status=SceneStatus.OCCURRED, history_status="ACTIVE", location="loc", participants=[character.id], story_threads=[thread.id])
        db.add(scene); db.flush()
        payload = {"project": {"id": project.id}, "scenes": [{"id": scene.id, "sequence": sequence, "status": "OCCURRED", "history_status": "ACTIVE"}]}
        pre = WorldSnapshot(project_id=project.id, snapshot_type=SnapshotType.PRE_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        post = WorldSnapshot(project_id=project.id, snapshot_type=SnapshotType.POST_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
        db.add_all([pre, post]); db.flush()
        checkpoint = SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=sequence, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2, version=1, active=True, origin=SceneCheckpointOrigin.LEGACY.value, checkpoint_fingerprint=f"checkpoint-{sequence}")
        db.add(checkpoint); db.flush()
        if sequence == count:
            event = TimelineEvent(project_id=project.id, event_type=TimelineEventType.STATE_CHANGE, source_type="SCENE", source_id=scene.id, source_key=f"state:{scene.id}", scene_id=scene.id, sequence=sequence, ordinal=1, origin=TimelineOrigin.LEGACY_BACKFILL, active=True, checkpoint_id=checkpoint.id, target_type="CHARACTER", target_id=character.id, path="/physical_state/injured", before_value=False, after_value=True, structured_payload={}, event_fingerprint=f"event-{sequence}")
            db.add(event)
        scenes.append(scene)
    db.commit()
    return SimpleNamespace(project=project, character=character, thread=thread, scenes=scenes)


def test_rebuild_creates_projection_features_and_heads(session):
    world = make_history(session)
    projection = ProjectHistoryProjectionService().rebuild(session, world.project.id)
    session.commit()
    assert projection.status.value == "READY"
    assert projection.built_through_sequence == 4
    assert projection.active_scene_count == 4
    assert session.scalar(select(func.count(SceneHistoryFeature.id)).where(SceneHistoryFeature.project_id == world.project.id)) == 4
    assert session.scalar(select(func.count(CurrentStateChangeHead.id)).where(CurrentStateChangeHead.project_id == world.project.id)) == 1
    ProjectHistoryProjectionAudit().audit(session, world.project.id)
    SceneHistoryFeatureAudit().audit(session, world.project.id)


def test_fast_gravity_matches_legacy_semantics(session):
    world = make_history(session)
    legacy_context = StoryGravityContextBuilder().build(session, world.project.id)
    legacy_report = StoryGravityEngine().build(legacy_context)
    ProjectHistoryProjectionService().rebuild(session, world.project.id)
    session.commit()
    fast_context = StoryGravityContextBuilder().build(session, world.project.id)
    fast_report = StoryGravityEngine().build(fast_context)
    assert fast_context["protocol_version"] == "story-gravity-context-v2"
    assert [(row["thread_id"], row["thread_gravity_score"]) for row in fast_report.thread_gravity] == [(row["thread_id"], row["thread_gravity_score"]) for row in legacy_report.thread_gravity]
    assert [(row["character_id"], row["character_gravity_score"]) for row in fast_report.character_gravity] == [(row["character_id"], row["character_gravity_score"]) for row in legacy_report.character_gravity]
    assert [candidate.candidate_key for candidate in DirectorCandidateEngine().generate(fast_context, fast_report)] == [candidate.candidate_key for candidate in DirectorCandidateEngine().generate(legacy_context, legacy_report)]


def test_dirty_projection_falls_back_without_rebuilding(session):
    world = make_history(session)
    ProjectHistoryProjectionService().rebuild(session, world.project.id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    projection.status = "DIRTY"
    session.commit()
    context = StoryGravityContextBuilder().build(session, world.project.id)
    assert context["protocol_version"] == "story-gravity-context-v1"
    persisted = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert getattr(persisted.status, "value", persisted.status) == "DIRTY"


def test_incremental_append_preserves_prefix_and_bounds_recent_signatures(session):
    world = make_history(session, 10)
    service = ProjectHistoryProjectionService()
    service.rebuild(session, world.project.id)
    session.commit()
    before = {row.scene_id: row.feature_fingerprint for row in session.scalars(select(SceneHistoryFeature).where(SceneHistoryFeature.project_id == world.project.id)).all()}
    scene = Scene(project_id=world.project.id, sequence=11, status=SceneStatus.OCCURRED, history_status="ACTIVE", location="loc", participants=[world.character.id], story_threads=[world.thread.id])
    session.add(scene); session.flush()
    payload = {"project": {"id": world.project.id}, "scenes": [{"id": scene.id, "sequence": 11, "status": "OCCURRED", "history_status": "ACTIVE"}]}
    pre = WorldSnapshot(project_id=world.project.id, snapshot_type=SnapshotType.PRE_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    post = WorldSnapshot(project_id=world.project.id, snapshot_type=SnapshotType.POST_SCENE_STATE, payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    session.add_all([pre, post]); session.flush()
    session.add(SceneStateCheckpoint(project_id=world.project.id, scene_id=scene.id, sequence=11, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2, version=1, active=True, origin=SceneCheckpointOrigin.LEGACY.value, checkpoint_fingerprint="checkpoint-11")); session.flush()
    service.sync_after_scene_commit(session, world.project.id, scene.id)
    session.commit()
    after = {row.scene_id: row.feature_fingerprint for row in session.scalars(select(SceneHistoryFeature).where(SceneHistoryFeature.project_id == world.project.id, SceneHistoryFeature.active.is_(True))).all()}
    assert all(after[scene_id] == fingerprint for scene_id, fingerprint in before.items())
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert projection.built_through_sequence == 11 and len(projection.recent_scene_signatures) == 10


def test_projection_fingerprint_chain_is_deterministic():
    builder = HistoryProjectionFingerprintBuilder()
    assert builder.extend(None, "feature-a") == builder.extend(None, "feature-a")
    assert builder.extend(builder.extend(None, "feature-a"), "feature-b") != builder.extend(None, "feature-b")


def test_large_fast_path_loads_bounded_scene_features(session):
    world = make_history(session, 100)
    service = ProjectHistoryProjectionService()
    service.rebuild(session, world.project.id)
    session.commit()
    statements = []
    def receive(conn, cursor, statement, parameters, context, executemany):
        if "scene_history_features" in statement.lower() or "timeline_events" in statement.lower():
            statements.append(statement.lower())
    event.listen(session.bind, "before_cursor_execute", receive)
    context = StoryGravityContextBuilder().build(session, world.project.id)
    event.remove(session.bind, "before_cursor_execute", receive)
    assert context["protocol_version"] == "story-gravity-context-v2"
    feature_queries = [item for item in statements if "scene_history_features" in item]
    assert feature_queries and all(" limit " in item for item in feature_queries) or len(feature_queries) <= 2
    assert all("from timeline_events" not in item or "timeline_events.id in" in item for item in statements)


def test_10000_scene_projection_fast_path_stays_bounded(session):
    project = Project(name="Ten Thousand Scenes")
    session.add(project); session.flush()
    scenes = [Scene(id=new_id(), project_id=project.id, sequence=index, status=SceneStatus.OCCURRED, history_status="ACTIVE", participants=[], story_threads=[], location="loc") for index in range(1, 10001)]
    session.bulk_save_objects(scenes)
    features = [SceneHistoryFeature(id=new_id(), project_id=project.id, scene_id=scene.id, sequence=scene.sequence, active=True, participant_ids=[], thread_ids=[], state_change_targets=[], state_change_paths=[], thread_state_event_ids=[], state_change_count=0, feature_fingerprint=f"synthetic-{scene.sequence}") for scene in scenes]
    session.bulk_save_objects(features)
    service = ProjectHistoryProjectionService()
    projection = ProjectHistoryProjection(project_id=project.id, protocol_version="project-history-projection-v1", status=HistoryProjectionStatus.READY, built_through_sequence=10000, active_scene_count=10000, last_scene_id=scenes[-1].id, recent_scene_signatures=[service.feature_builder.signature(row) for row in features[-10:]], thread_stats={}, character_stats={}, projection_fingerprint="synthetic")
    session.add(projection); session.flush()
    projection.source_history_fingerprint = service.current_source_fingerprint(session, project.id)
    session.commit()
    statements = []
    def receive(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "scene_history_features" in lowered or " from scenes " in lowered or "timeline_events" in lowered:
            statements.append(lowered)
    event.listen(session.bind, "before_cursor_execute", receive)
    context = StoryGravityContextBuilder().build(session, project.id)
    event.remove(session.bind, "before_cursor_execute", receive)
    assert context["protocol_version"] == "story-gravity-context-v2"
    assert len(context["scenes"]) == 10
    assert all("limit" in statement or "where 0 = 1" in statement for statement in statements if "scene_history_features" in statement or " from scenes " in statement), statements
    assert all("from timeline_events" not in statement for statement in statements)
