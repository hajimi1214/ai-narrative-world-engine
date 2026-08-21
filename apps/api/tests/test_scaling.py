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
    SceneProposal, ScenePerformance, SceneExecutionBinding, ProposalType, PerformanceMode, PerformanceStatus,
    CharacterKnowledge, KnowledgeStatus,
    CausalLink, CausalRelationType,
)
from app.causal_ledger import CausalLedgerService
from app.scaling import (
    HistoryProjectionFingerprintBuilder, ProjectHistoryProjectionAudit,
    ProjectHistoryProjectionService, SceneHistoryFeatureAudit, THREAD_STATS_META_KEY,
    empty_thread_stats,
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


def add_checkpoint(db, project_id, scene, fingerprint):
    payload = {"project": {"id": project_id}, "scenes": [{"id": scene.id, "sequence": scene.sequence}]}
    pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.PRE_SCENE_STATE,
                        payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.POST_SCENE_STATE,
                         payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    db.add_all([pre, post]); db.flush()
    db.add(SceneStateCheckpoint(
        project_id=project_id, scene_id=scene.id, sequence=scene.sequence,
        pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id,
        capture_protocol_version=2, version=1, active=True, origin=SceneCheckpointOrigin.LEGACY.value,
        checkpoint_fingerprint=fingerprint,
    ))
    db.flush()


def make_cold_scene(db, *, active_characters=1):
    project = Project(name="Cold start")
    db.add(project); db.flush()
    characters = [Character(project_id=project.id, name=f"A{index}") for index in range(active_characters)]
    thread = StoryThread(project_id=project.id, title="Thread", type="MYSTERY", weight=1,
                         progress=0.0, status=ThreadStatus.OPEN)
    db.add_all([*characters, thread]); db.flush()
    scene = Scene(project_id=project.id, sequence=1, status=SceneStatus.OCCURRED,
                  history_status="ACTIVE", location="loc",
                  participants=[row.id for row in characters], story_threads=[thread.id])
    db.add(scene); db.flush()
    add_checkpoint(db, project.id, scene, "cold-checkpoint-1")
    return SimpleNamespace(project=project, characters=characters, thread=thread, scene=scene)


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


def test_incremental_append_does_not_rescan_existing_state_heads(session):
    """Normal append work is independent of the number of current paths."""

    def append_query_count(head_count):
        world = make_history(session, 1)
        event_rows = []
        head_rows = []
        for index in range(head_count):
            event_id = new_id()
            event_rows.append({
                "id": event_id, "project_id": world.project.id,
                "event_type": TimelineEventType.STATE_CHANGE,
                "source_type": "SCENE", "source_id": world.scenes[0].id,
                "source_key": f"seed-state:{head_count}:{index}",
                "scene_id": world.scenes[0].id, "sequence": 1, "ordinal": index + 1,
                "origin": TimelineOrigin.LEGACY_BACKFILL, "active": True,
                "target_type": "CHARACTER", "target_id": f"target-{index}",
                "path": "/current_state/value", "before_value": None,
                "after_value": index, "structured_payload": {},
                "event_fingerprint": f"seed-event:{head_count}:{index}",
            })
            head_rows.append({
                "id": new_id(), "project_id": world.project.id,
                "timeline_event_id": event_id, "scene_id": world.scenes[0].id,
                "sequence": 1, "ordinal": index + 1,
                "target_type": "CHARACTER", "target_id": f"target-{index}",
                "path": "/current_state/value",
                "event_fingerprint": f"seed-event:{head_count}:{index}",
            })
        session.execute(TimelineEvent.__table__.insert(), event_rows)
        session.execute(CurrentStateChangeHead.__table__.insert(), head_rows)
        service = ProjectHistoryProjectionService()
        service.rebuild(session, world.project.id)
        scene = Scene(
            project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED,
            history_status="ACTIVE", location="loc", participants=[world.character.id],
            story_threads=[world.thread.id],
        )
        session.add(scene); session.flush()
        add_checkpoint(session, world.project.id, scene, f"append-{head_count}")
        statements = []

        def capture(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement.lower())

        event.listen(session.bind, "before_cursor_execute", capture)
        try:
            service.sync_after_scene_commit(session, world.project.id, scene.id)
        finally:
            event.remove(session.bind, "before_cursor_execute", capture)
        assert not any("from current_state_change_heads" in statement for statement in statements)
        assert not any(
            "from timeline_events" in statement and "timeline_events.scene_id" not in statement
            for statement in statements
        )
        ProjectHistoryProjectionAudit().audit(session, world.project.id)
        return len(statements)

    assert append_query_count(100) == append_query_count(10_000)


def test_incremental_append_replaces_state_head_accumulator_exactly(session):
    world = make_history(session, 1)
    service = ProjectHistoryProjectionService()
    service.rebuild(session, world.project.id)
    session.commit()
    original = session.scalar(select(CurrentStateChangeHead).where(
        CurrentStateChangeHead.project_id == world.project.id,
    ))
    scene = Scene(
        project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED,
        history_status="ACTIVE", location="loc", participants=[world.character.id],
        story_threads=[world.thread.id],
    )
    session.add(scene); session.flush()
    add_checkpoint(session, world.project.id, scene, "replace-head")
    replacement = TimelineEvent(
        project_id=world.project.id, event_type=TimelineEventType.STATE_CHANGE,
        source_type="SCENE", source_id=scene.id, source_key=f"replace-head:{scene.id}",
        scene_id=scene.id, sequence=2, ordinal=1,
        origin=TimelineOrigin.LEGACY_BACKFILL, active=True,
        target_type=original.target_type, target_id=original.target_id, path=original.path,
        before_value=True, after_value=False, structured_payload={}, event_fingerprint="replacement-head",
    )
    session.add(replacement); session.flush()
    service.sync_after_scene_commit(session, world.project.id, scene.id)
    session.commit()
    head = session.scalar(select(CurrentStateChangeHead).where(
        CurrentStateChangeHead.project_id == world.project.id,
    ))
    projection = session.scalar(select(ProjectHistoryProjection).where(
        ProjectHistoryProjection.project_id == world.project.id,
    ))
    assert head.timeline_event_id == replacement.id
    assert projection.thread_stats[THREAD_STATS_META_KEY]["state_head_accumulator"] == service._head_accumulator(
        session, world.project.id,
    )
    ProjectHistoryProjectionAudit().audit(session, world.project.id)


def test_cold_start_first_scene_with_active_characters_is_ready_and_fast(session):
    world = make_cold_scene(session, active_characters=2)
    service = ProjectHistoryProjectionService()
    service.sync_after_scene_commit(session, world.project.id, world.scene.id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert getattr(projection.status, "value", projection.status) == "READY"
    assert projection.built_through_sequence == projection.active_scene_count == 1
    assert projection.last_scene_id == world.scene.id
    assert projection.thread_stats[THREAD_STATS_META_KEY]["active_character_ids"] == sorted(row.id for row in world.characters)
    assert len(projection.recent_scene_signatures) == 1
    assert projection.source_history_fingerprint == service.current_source_fingerprint(session, world.project.id)
    assert service.status(session, world.project.id)["fast_path_available"] is True
    assert StoryGravityContextBuilder().build(session, world.project.id)["protocol_version"] == "story-gravity-context-v2"
    ProjectHistoryProjectionAudit().audit(session, world.project.id)


def test_cold_start_first_scene_with_no_active_characters_is_ready(session):
    world = make_cold_scene(session, active_characters=0)
    service = ProjectHistoryProjectionService()
    service.sync_after_scene_commit(session, world.project.id, world.scene.id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert getattr(projection.status, "value", projection.status) == "READY"
    assert projection.thread_stats == {THREAD_STATS_META_KEY: {
        "active_character_ids": [],
        "state_head_accumulator": {"count": 0, "xor": 0, "sum": 0},
    }, world.thread.id: {
        "last_touched_sequence": 1, "scene_count": 1, "aligned_participant_ids": [], "scene_alignment_count": 0,
    }}
    ProjectHistoryProjectionAudit().audit(session, world.project.id)


def test_cold_start_second_scene_appends_without_explicit_rebuild(session):
    world = make_cold_scene(session, active_characters=1)
    service = ProjectHistoryProjectionService()
    service.sync_after_scene_commit(session, world.project.id, world.scene.id)
    scene2 = Scene(project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED,
                   history_status="ACTIVE", location="loc", participants=[world.characters[0].id],
                   story_threads=[world.thread.id])
    session.add(scene2); session.flush()
    add_checkpoint(session, world.project.id, scene2, "cold-checkpoint-2")
    service.sync_after_scene_commit(session, world.project.id, scene2.id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert getattr(projection.status, "value", projection.status) == "READY"
    assert projection.built_through_sequence == projection.active_scene_count == 2
    assert session.scalar(select(func.count(SceneHistoryFeature.id)).where(
        SceneHistoryFeature.project_id == world.project.id, SceneHistoryFeature.active.is_(True),
    )) == 2
    ProjectHistoryProjectionAudit().audit(session, world.project.id)


def test_missing_projection_beyond_first_scene_stays_dirty(session):
    world = make_history(session, 2)
    ProjectHistoryProjectionService().sync_after_scene_commit(session, world.project.id, world.scenes[1].id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert getattr(projection.status, "value", projection.status) == "DIRTY"
    assert projection.dirty_from_sequence == 1


def test_fast_projection_preserves_formal_scene_and_legacy_proposal_signature_semantics(session):
    world = make_history(session, 1)
    inactive = Character(project_id=world.project.id, name="Historical")
    session.add(inactive); session.flush()
    scene = world.scenes[0]
    scene.location = "new-location"
    scene.participants = [world.character.id]
    proposal = SceneProposal(
        project_id=world.project.id, context_fingerprint="ctx", proposal_type=ProposalType.CONTINUE_THREAD,
        primary_thread_id=world.thread.id, location_id=None, proposed_location="new-location",
        participants=[world.character.id, inactive.id], scene_goal="goal", character_motivations={},
        entry_state={}, expected_progress={}, allowed_reveals=[], forbidden_reveals=[], required_canon=[],
        possible_outcomes=[], new_entity_requests=[], risk_flags=[], director_reasoning_summary="reason",
    )
    session.add(proposal); session.flush()
    performance = ScenePerformance(
        project_id=world.project.id, scene_proposal_id=proposal.id, take_number=1,
        proposal_context_fingerprint="ctx", mode=PerformanceMode.HEURISTIC,
        status=PerformanceStatus.COMPLETED, participant_order=[], active_participant_ids=[], max_turns=1,
        turn_count=1,
    )
    session.add(performance); session.flush()
    session.add(SceneExecutionBinding(project_id=world.project.id, scene_id=scene.id, performance_id=performance.id, active=True))
    session.commit()

    legacy = StoryGravityContextBuilder().build(session, world.project.id)
    ProjectHistoryProjectionService().rebuild(session, world.project.id)
    session.commit()
    fast = StoryGravityContextBuilder().build(session, world.project.id)

    assert legacy["scenes"] == fast["scenes"]
    assert legacy["recent_scene_signatures"] == fast["recent_scene_signatures"]
    assert fast["scenes"][0]["participants"] == [world.character.id]
    assert fast["scenes"][0]["location_id"] == "new-location"
    assert fast["recent_scene_signatures"][0]["participants"] == sorted([world.character.id, inactive.id])
    assert fast["recent_scene_signatures"][0]["location_id"] is None

    legacy_report = StoryGravityEngine().build(legacy)
    fast_report = StoryGravityEngine().build(fast)
    assert legacy_report.thread_gravity == fast_report.thread_gravity
    assert legacy_report.character_gravity == fast_report.character_gravity
    legacy_candidates = DirectorCandidateEngine().generate(legacy, legacy_report)
    fast_candidates = DirectorCandidateEngine().generate(fast, fast_report)
    assert [(row.candidate_key, row.score, row.score_components, row.reason_codes) for row in legacy_candidates] == [
        (row.candidate_key, row.score, row.score_components, row.reason_codes) for row in fast_candidates
    ]


def test_active_character_set_stales_projection_and_rebuilds_thread_alignment(session):
    world = make_history(session, 2)
    historical = Character(project_id=world.project.id, name="Historical", active=False)
    session.add(historical)
    world.scenes[0].participants = [historical.id]
    world.scenes[1].participants = [world.character.id]
    session.commit()
    service = ProjectHistoryProjectionService()
    projection = service.rebuild(session, world.project.id)
    session.commit()
    assert projection.thread_stats[world.thread.id]["scene_alignment_count"] == 1

    world.character.active = False
    session.commit()
    assert StoryGravityContextBuilder().build(session, world.project.id)["protocol_version"] == "story-gravity-context-v1"
    projection = service.rebuild(session, world.project.id)
    session.commit()
    assert projection.thread_stats[world.thread.id]["scene_alignment_count"] == 0


def test_projection_source_freshness_does_not_scan_character_knowledge(session):
    world = make_history(session, 1)
    session.add_all([
        CharacterKnowledge(character_id=world.character.id, proposition=f"fact-{index}", status=KnowledgeStatus.KNOWN,
                           confidence=1.0)
        for index in range(200)
    ])
    session.commit()
    statements = []

    def receive(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(session.bind, "before_cursor_execute", receive)
    ProjectHistoryProjectionService().current_source_fingerprint(session, world.project.id)
    event.remove(session.bind, "before_cursor_execute", receive)
    assert not any("character_knowledge" in statement for statement in statements)


def test_projection_append_failure_marks_dirty_without_removing_formal_scene(session, monkeypatch):
    world = make_history(session, 1)
    service = ProjectHistoryProjectionService()
    service.rebuild(session, world.project.id)
    session.commit()
    scene = Scene(project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED,
                  history_status="ACTIVE", location="loc", participants=[world.character.id],
                  story_threads=[world.thread.id])
    session.add(scene); session.flush()
    payload = {"project": {"id": world.project.id}, "scenes": [{"id": scene.id, "sequence": 2}]}
    pre = WorldSnapshot(project_id=world.project.id, snapshot_type=SnapshotType.PRE_SCENE_STATE,
                        payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    post = WorldSnapshot(project_id=world.project.id, snapshot_type=SnapshotType.POST_SCENE_STATE,
                         payload=payload, state_fingerprint=snapshot_fingerprint(payload))
    session.add_all([pre, post]); session.flush()
    session.add(SceneStateCheckpoint(project_id=world.project.id, scene_id=scene.id, sequence=2,
              pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id,
              capture_protocol_version=2, version=1, active=True, origin=SceneCheckpointOrigin.LEGACY.value,
              checkpoint_fingerprint="checkpoint-2"))
    session.flush()

    def fail(*args, **kwargs):
        raise RuntimeError("SCALING_TEST_FAILURE")

    monkeypatch.setattr(service.feature_builder, "build", fail)
    service.sync_after_scene_commit(session, world.project.id, scene.id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == world.project.id))
    assert session.get(Scene, scene.id).status == SceneStatus.OCCURRED
    assert getattr(projection.status, "value", projection.status) == "DIRTY"


def test_projection_audit_detects_tampered_thread_stats(session):
    world = make_history(session, 2)
    service = ProjectHistoryProjectionService()
    projection = service.rebuild(session, world.project.id)
    session.commit()
    projection.thread_stats[world.thread.id]["scene_count"] = 999
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(projection, "thread_stats")
    session.flush()
    with pytest.raises(ValueError, match="SCALING_PROJECTION_INTEGRITY_INVALID"):
        ProjectHistoryProjectionAudit().audit(session, world.project.id)


@pytest.mark.parametrize(("field", "tampered_value"), [
    ("location_id", "tampered-location"),
    ("participant_ids", ["tampered-participant"]),
    ("thread_ids", ["tampered-thread"]),
    ("state_change_paths", ["/tampered"]),
    ("checkpoint_fingerprint", "tampered-checkpoint"),
])
def test_feature_audit_detects_semantic_column_tampering(session, field, tampered_value):
    world = make_history(session, 2)
    ProjectHistoryProjectionService().rebuild(session, world.project.id)
    session.commit()
    feature = session.scalar(select(SceneHistoryFeature).where(
        SceneHistoryFeature.project_id == world.project.id,
        SceneHistoryFeature.scene_id == world.scenes[-1].id,
    ))
    setattr(feature, field, tampered_value)
    with pytest.raises(ValueError, match="SCALING_PROJECTION_INTEGRITY_INVALID"):
        SceneHistoryFeatureAudit().audit(session, world.project.id)


def test_project_audit_propagates_feature_semantic_tampering(session):
    world = make_history(session, 2)
    ProjectHistoryProjectionService().rebuild(session, world.project.id)
    session.commit()
    feature = session.scalar(select(SceneHistoryFeature).where(
        SceneHistoryFeature.project_id == world.project.id,
        SceneHistoryFeature.scene_id == world.scenes[-1].id,
    ))
    feature.state_change_count += 1
    with pytest.raises(ValueError, match="SCALING_PROJECTION_INTEGRITY_INVALID"):
        ProjectHistoryProjectionAudit().audit(session, world.project.id)


def test_temporal_append_and_replay_suffix_preserve_untouched_prefix(session):
    project = Project(name="Temporal prefix")
    session.add(project); session.flush()
    scenes = [Scene(project_id=project.id, sequence=index, status=SceneStatus.OCCURRED,
                    history_status="ACTIVE", participants=[], story_threads=[])
              for index in range(1, 101)]
    session.add_all(scenes); session.flush()
    ledger = CausalLedgerService()
    ledger.rebuild_temporal_edges(session, project.id)
    session.flush()
    prefix = {
        row.source_key: (row.id, row.link_fingerprint, row.active)
        for row in session.scalars(select(CausalLink).where(
            CausalLink.project_id == project.id,
            CausalLink.relation_type == CausalRelationType.SCENE_PRECEDES_SCENE,
            CausalLink.sequence < 60,
        )).all()
    }
    scene_101 = Scene(project_id=project.id, sequence=101, status=SceneStatus.OCCURRED,
                      history_status="ACTIVE", participants=[], story_threads=[])
    session.add(scene_101); session.flush()
    ledger.sync_temporal_append(session, project.id, scene_101.id)
    session.flush()
    assert session.scalar(select(func.count(CausalLink.id)).where(
        CausalLink.project_id == project.id,
        CausalLink.relation_type == CausalRelationType.SCENE_PRECEDES_SCENE,
        CausalLink.active.is_(True),
    )) == 100
    ledger.rebuild_temporal_edges_from_sequence(session, project.id, 60)
    session.flush()
    after = {
        row.source_key: (row.id, row.link_fingerprint, row.active)
        for row in session.scalars(select(CausalLink).where(
            CausalLink.project_id == project.id,
            CausalLink.relation_type == CausalRelationType.SCENE_PRECEDES_SCENE,
            CausalLink.sequence < 60,
        )).all()
    }
    assert after == prefix


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


def test_fast_context_uses_stored_state_head_identity_without_duplicate_head_scan(session):
    """The context needs heads for pressure, but freshness must not read them twice."""
    world = make_history(session, 3)
    service = ProjectHistoryProjectionService()
    service.rebuild(session, world.project.id)
    session.commit()
    statements = []

    def receive(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement.lower())

    event.listen(session.bind, "before_cursor_execute", receive)
    try:
        context = StoryGravityContextBuilder().build(session, world.project.id)
    finally:
        event.remove(session.bind, "before_cursor_execute", receive)
    assert context["protocol_version"] == "story-gravity-context-v2"
    head_queries = [statement for statement in statements if "from current_state_change_heads" in statement]
    assert len(head_queries) == 1


def test_missing_state_head_accumulator_explicitly_falls_back_to_legacy(session):
    world = make_history(session, 3)
    service = ProjectHistoryProjectionService()
    service.rebuild(session, world.project.id)
    session.commit()
    projection = session.scalar(select(ProjectHistoryProjection).where(
        ProjectHistoryProjection.project_id == world.project.id,
    ))
    projection.thread_stats[THREAD_STATS_META_KEY].pop("state_head_accumulator")
    session.commit()
    assert StoryGravityContextBuilder().build(session, world.project.id)["protocol_version"] == "story-gravity-context-v1"
    assert service.status(session, world.project.id)["fast_path_available"] is False


def test_10000_scene_projection_fast_path_stays_bounded(session):
    project = Project(name="Ten Thousand Scenes")
    session.add(project); session.flush()
    scenes = [Scene(id=new_id(), project_id=project.id, sequence=index, status=SceneStatus.OCCURRED, history_status="ACTIVE", participants=[], story_threads=[], location="loc") for index in range(1, 10001)]
    session.bulk_save_objects(scenes)
    features = [SceneHistoryFeature(id=new_id(), project_id=project.id, scene_id=scene.id, sequence=scene.sequence, active=True, participant_ids=[], thread_ids=[], state_change_targets=[], state_change_paths=[], thread_state_event_ids=[], state_change_count=0, feature_fingerprint=f"synthetic-{scene.sequence}") for scene in scenes]
    session.bulk_save_objects(features)
    service = ProjectHistoryProjectionService()
    projection = ProjectHistoryProjection(project_id=project.id, protocol_version="project-history-projection-v1", status=HistoryProjectionStatus.READY, built_through_sequence=10000, active_scene_count=10000, last_scene_id=scenes[-1].id, recent_scene_signatures=[service.feature_builder.signature(scene, None) for scene in scenes[-10:]], thread_stats=empty_thread_stats([], service._empty_head_accumulator()), character_stats={}, projection_fingerprint="synthetic")
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
