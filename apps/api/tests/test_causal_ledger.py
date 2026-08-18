"""Phase 8 derived Timeline/Causal Ledger contracts."""
import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Enum, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api
from app.causal_ledger import (
    CausalLedgerBackfillService, CausalLedgerService, CurrentCausalLedgerAudit,
    SceneStateTransitionExtractor, explicit_knowledge_id, explicit_memory_id, read_overlay_path,
)
from app.db import Base
from app.main import app
from app.models import (
    CausalEdgeKind, CausalLink, CausalRelationType, CausalResourceType, CharacterDecision,
    CharacterKnowledge, CharacterMemory,
    RetconApplication, RetconReplaySession, Scene, SceneCommit, SceneExecutionBinding,
    SceneStateCheckpoint, StateDeltaBatch, TimelineEvent, TimelineEventType, WorldEntity,
    WorldSnapshot,
)
from test_scene_commit import add_resolution_turn, effect, prepared_commit


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def _world(*, opened=False, inventory=None, canon=None):
    return {
        "project": {"id": "p", "current_world_time": "2040-01-01T00:00:00"},
        "world_entities": [{"id": "door", "profile": {"opened": opened, "security": {"locked": True, "alarm": True}}, "active": True}],
        "characters": [{"id": "a", "current_state": {}, "inventory": inventory or [], "relationships": {}, "physical_state": {}, "emotional_state": {}}],
        "story_threads": [{"id": "t", "state": {}, "status": "OPEN", "progress": 0.0}],
        "canon_facts": canon or [], "character_knowledge": [], "character_memories": [], "scenes": [],
    }


def _commit(session, monkeypatch):
    return prepared_commit(session, monkeypatch)


def test_pointer_reader_is_pure_and_rfc6901():
    document = {"a/b": {"~key": ["x"]}}
    original = copy.deepcopy(document)
    assert read_overlay_path(document, "/a~1b/~0key/0") == (True, "x")
    assert read_overlay_path(document, "/missing") == (False, None)
    assert document == original


def test_phase8_orm_enum_columns_use_migration_compatible_varchar():
    from app.models import CausalLink as LinkModel, TimelineEvent as EventModel

    columns = {
        EventModel.__table__.c.event_type: 30,
        EventModel.__table__.c.origin: 30,
        LinkModel.__table__.c.cause_type: 40,
        LinkModel.__table__.c.effect_type: 40,
        LinkModel.__table__.c.edge_kind: 30,
        LinkModel.__table__.c.relation_type: 60,
    }
    for column, expected_length in columns.items():
        assert isinstance(column.type, Enum)
        assert column.type.native_enum is False
        assert column.type.length == expected_length


def test_transition_extractor_emits_stable_leaf_paths():
    before, after = _world(), _world(opened=True, inventory=["key"])
    after["world_entities"][0]["profile"]["security"]["alarm"] = False
    paths = [(item.target_type, item.target_id, item.path) for item in SceneStateTransitionExtractor().extract(before, after)]
    assert paths == sorted(paths)
    assert ("WORLD_ENTITY", "door", "/profile/opened") in paths
    assert ("WORLD_ENTITY", "door", "/profile/security/alarm") in paths
    assert ("CHARACTER", "a", "/inventory") in paths


def test_transition_extractor_ignores_cognition_and_scenes():
    before, after = _world(), _world()
    after["character_knowledge"] = [{"id": "k"}]
    after["scenes"] = [{"id": "s"}]
    assert SceneStateTransitionExtractor().extract(before, after) == []


def test_transition_extractor_rejects_canon_mutation():
    before = _world(canon=[{"id": "c", "data": {"locked": True}}])
    after = _world(canon=[{"id": "c", "data": {"locked": False}}])
    with pytest.raises(ValueError, match="CAUSAL_LEDGER_UNEXPECTED_CANON_MUTATION"):
        SceneStateTransitionExtractor().extract(before, after)


def test_normal_commit_materializes_scene_and_state_events(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    scene_id = response.json()["scene"]["id"]
    events = session.scalars(select(TimelineEvent).where(TimelineEvent.scene_id == scene_id, TimelineEvent.active.is_(True))).all()
    assert len([row for row in events if row.event_type == TimelineEventType.SCENE_OCCURRED]) == 1
    state = next(row for row in events if row.event_type == TimelineEventType.STATE_CHANGE)
    assert state.target_id == location.id and state.path == "/profile/opened"
    assert state.before_value is False and state.after_value is True
    assert state.structured_payload["state_delta_item_id"]


def test_state_change_has_resolution_item_and_scene_provenance(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    session.expire_all()
    event = session.scalar(select(TimelineEvent).where(TimelineEvent.project_id == project.id, TimelineEvent.event_type == TimelineEventType.STATE_CHANGE))
    links = session.scalars(select(CausalLink).where(CausalLink.project_id == project.id, CausalLink.active.is_(True))).all()
    assert any(row.cause_id == resolution.id and row.effect_id == event.structured_payload["state_delta_item_id"] and row.relation_type == CausalRelationType.RESOLUTION_PRODUCED_STATE_CHANGE for row in links)
    assert any(row.cause_id == event.id and row.relation_type == CausalRelationType.STATE_CHANGE_COMMITTED_IN_SCENE for row in links)


def test_state_event_ordinals_are_stable_and_scene_starts_at_zero(session, monkeypatch):
    project, location, actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    add_resolution_turn(
        session, project, location, proposal, performance, actor, 2,
        [effect(location.id, True, "/profile/locked")],
    )
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    scene_id = response.json()["scene"]["id"]
    first = session.scalars(select(TimelineEvent).where(TimelineEvent.scene_id == scene_id).order_by(TimelineEvent.ordinal)).all()
    assert [event.ordinal for event in first] == [0, 1, 2]
    before = [(event.id, event.ordinal, event.event_fingerprint) for event in first]
    CausalLedgerService().index_scene(session, project.id, scene_id)
    after = [(event.id, event.ordinal, event.event_fingerprint) for event in session.scalars(select(TimelineEvent).where(TimelineEvent.scene_id == scene_id).order_by(TimelineEvent.ordinal)).all()]
    assert after == before


def test_replay_resolution_link_requires_structured_effect_value_match(session, monkeypatch):
    project, location, actor, _other, proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    add_resolution_turn(
        session, project, location, proposal, performance, actor, 2,
        [effect(location.id, False, "/profile/opened")],
    )
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert committed.status_code == 200, committed.text
    scene = session.get(Scene, committed.json()["scene"]["id"])
    binding = session.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
    event = session.scalar(select(TimelineEvent).where(
        TimelineEvent.scene_id == scene.id,
        TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        TimelineEvent.path == "/profile/opened",
    ))
    CausalLedgerService()._link_replay_resolution_if_unique(session, scene, binding, event)
    links = session.scalars(select(CausalLink).where(
        CausalLink.effect_id == event.id,
        CausalLink.relation_type == CausalRelationType.RESOLUTION_PRODUCED_STATE_CHANGE,
        CausalLink.cause_type == CausalResourceType.WORLD_RESOLUTION,
    )).all()
    assert [link.cause_id for link in links] == [resolution.id]


def test_replay_resolution_link_refuses_ambiguous_structured_candidates(session, monkeypatch):
    project, location, actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert committed.status_code == 200, committed.text
    # The second valid effect is deliberately added after formal commit: it is
    # a correlation fixture, never a second applied state change.
    add_resolution_turn(
        session, project, location, proposal, performance, actor, 2,
        [effect(location.id, True, "/profile/opened")],
    )
    scene = session.get(Scene, committed.json()["scene"]["id"])
    binding = session.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
    event = session.scalar(select(TimelineEvent).where(
        TimelineEvent.scene_id == scene.id,
        TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        TimelineEvent.path == "/profile/opened",
    ))
    CausalLedgerService()._link_replay_resolution_if_unique(session, scene, binding, event)
    assert not session.scalars(select(CausalLink).where(
        CausalLink.effect_id == event.id,
        CausalLink.relation_type == CausalRelationType.RESOLUTION_PRODUCED_STATE_CHANGE,
        CausalLink.cause_type == CausalResourceType.WORLD_RESOLUTION,
    )).all()


def test_explicit_knowledge_informs_decision_only(session, monkeypatch):
    project, _location, actor, _other, _proposal, performance, turn, _resolution, _batch, client = _commit(session, monkeypatch)
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert committed.status_code == 200
    knowledge = CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: opened = false", status="KNOWN", source=None)
    session.add(knowledge); session.flush()
    decision = session.get(CharacterDecision, turn.character_decision_id)
    decision.knowledge_used = [{"knowledge_id": knowledge.id, "proposition": knowledge.proposition, "accepted_statuses": ["KNOWN"]}]
    session.commit()
    CausalLedgerService().index_scene(session, project.id, committed.json()["scene"]["id"])
    session.flush(); session.expire_all()
    link = session.scalar(select(CausalLink).where(CausalLink.cause_id == knowledge.id, CausalLink.effect_id == decision.id))
    assert link and link.relation_type == CausalRelationType.KNOWLEDGE_INFORMED_DECISION


def test_proposition_only_knowledge_reference_does_not_create_edge(session, monkeypatch):
    project, _location, actor, _other, _proposal, performance, turn, _resolution, _batch, client = _commit(session, monkeypatch)
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert committed.status_code == 200
    knowledge = CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: opened = false", status="KNOWN", source=None)
    session.add(knowledge); session.flush()
    decision = session.get(CharacterDecision, turn.character_decision_id)
    decision.knowledge_used = [{"proposition": knowledge.proposition, "accepted_statuses": ["KNOWN"]}]
    session.commit()
    CausalLedgerService().index_scene(session, project.id, committed.json()["scene"]["id"])
    assert not session.scalar(select(CausalLink).where(CausalLink.cause_id == knowledge.id, CausalLink.effect_id == decision.id))


def test_plain_string_knowledge_reference_is_not_text_lookup(session, monkeypatch):
    project, _location, actor, _other, _proposal, performance, turn, _resolution, _batch, client = _commit(session, monkeypatch)
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert committed.status_code == 200
    knowledge = CharacterKnowledge(character_id=actor.id, proposition="same proposition", status="KNOWN", source=None)
    session.add(knowledge); session.flush()
    decision = session.get(CharacterDecision, turn.character_decision_id)
    decision.knowledge_used = [knowledge.proposition]
    session.commit()
    CausalLedgerService().index_scene(session, project.id, committed.json()["scene"]["id"])
    assert not session.scalar(select(CausalLink).where(CausalLink.cause_id == knowledge.id, CausalLink.effect_id == decision.id))


def test_explicit_memory_reference_only_links_named_memory(session, monkeypatch):
    project, _location, actor, _other, _proposal, performance, turn, _resolution, _batch, client = _commit(session, monkeypatch)
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert committed.status_code == 200
    memory = CharacterMemory(character_id=actor.id, content="A remembered action", source_scene=None)
    unreferenced = CharacterMemory(character_id=actor.id, content="Another action", source_scene=None)
    session.add_all([memory, unreferenced]); session.flush()
    decision = session.get(CharacterDecision, turn.character_decision_id)
    decision.memory_refs = [{"memory_id": memory.id}]
    session.commit()
    CausalLedgerService().index_scene(session, project.id, committed.json()["scene"]["id"])
    linked = session.scalars(select(CausalLink).where(CausalLink.effect_id == decision.id, CausalLink.relation_type == CausalRelationType.MEMORY_INFORMED_DECISION)).all()
    assert [link.cause_id for link in linked] == [memory.id]


def test_reference_parsers_fail_closed_for_proposition_text():
    assert explicit_knowledge_id("knowledge-id") is None
    assert explicit_knowledge_id({"proposition": "same text"}) is None
    assert explicit_knowledge_id({"knowledge_id": "  k1  "}) == "k1"
    assert explicit_memory_id("m1") == "m1"
    assert explicit_memory_id({"content": "same text"}) is None


def test_temporal_edges_are_not_causal(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    # A one-scene history has no temporal edge, which is deliberate: order is not cause.
    assert not session.scalars(select(CausalLink).where(CausalLink.project_id == project.id, CausalLink.edge_kind == CausalEdgeKind.TEMPORAL)).all()


def test_backfill_is_idempotent_and_does_not_change_world(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    before = copy.deepcopy(location.profile)
    CausalLedgerBackfillService().backfill(session, project.id); session.flush()
    first = session.query(TimelineEvent).filter_by(project_id=project.id).count(), session.query(CausalLink).filter_by(project_id=project.id).count()
    CausalLedgerBackfillService().backfill(session, project.id); session.flush()
    assert first == (session.query(TimelineEvent).filter_by(project_id=project.id).count(), session.query(CausalLink).filter_by(project_id=project.id).count())
    assert location.profile == before


def test_timeline_api_ordering_and_metadata_only(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    response = client.get(f"/projects/{project.id}/timeline")
    assert response.status_code == 200
    assert all("payload" not in event for event in response.json())
    assert [event["sequence"] for event in response.json()] == sorted(event["sequence"] for event in response.json())


def test_state_history_and_why_state_api(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    history = client.get(f"/projects/{project.id}/causal-ledger/state-history", params={"target_type": "WORLD_ENTITY", "target_id": location.id, "path": "/profile/opened"})
    assert history.status_code == 200 and len(history.json()) == 1
    why = client.get(f"/projects/{project.id}/causal-ledger/why-state", params={"target_type": "WORLD_ENTITY", "target_id": location.id, "path": "/profile/opened"})
    assert why.status_code == 200 and why.json()["event"]["after_value"] is True


def test_cross_project_timeline_is_not_exposed(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    assert client.get("/projects/not-a-project/timeline").status_code == 404


def test_current_ledger_audit_passes_after_normal_commit(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    CurrentCausalLedgerAudit().audit(session, project.id)


def test_ledger_failure_rolls_back_normal_materialization(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    models = (Scene, SceneCommit, StateDeltaBatch, SceneStateCheckpoint, CharacterKnowledge, CharacterMemory, TimelineEvent, CausalLink)
    counts = {model: session.query(model).count() for model in models}
    def fail(stage):
        if stage == "AFTER_CAUSAL_LEDGER_SYNC":
            raise RuntimeError("TEST_CAUSAL_LEDGER_FAILURE")
    monkeypatch.setattr(CausalLedgerService, "failure_injector", fail)
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_FAILED"
    with sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)() as fresh:
        assert counts == {model: fresh.query(model).count() for model in counts}
        assert fresh.get(WorldEntity, location.id).profile["opened"] is False


def test_scene_payload_never_uses_summary_as_authority(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    scene_event = session.scalar(select(TimelineEvent).where(TimelineEvent.project_id == project.id, TimelineEvent.event_type == TimelineEventType.SCENE_OCCURRED))
    assert "summary" not in scene_event.structured_payload and "facts" not in scene_event.structured_payload


def test_reindex_reuses_deterministic_source_keys(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    before = [(row.source_key, row.event_fingerprint) for row in session.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project.id)).all()]
    CausalLedgerService().index_scene(session, project.id, body["scene"]["id"]); session.flush()
    after = [(row.source_key, row.event_fingerprint) for row in session.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project.id)).all()]
    assert sorted(before) == sorted(after)


def test_invalid_current_checkpoint_fails_closed(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = _commit(session, monkeypatch)
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    checkpoint = session.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.scene_id == scene_id, SceneStateCheckpoint.active.is_(True)))
    checkpoint.checkpoint_fingerprint = "corrupt"; session.commit()
    with pytest.raises(ValueError, match="CAUSAL_LEDGER_CHECKPOINT_INVALID"):
        CausalLedgerService().index_scene(session, project.id, scene_id)


def _completed_replay(session, monkeypatch):
    from test_retcon_replay import historical_replay_world
    project, old_scene, _actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201
    state = created.json()
    while state["cursor"] < len(state["queue"]):
        stepped = client.post(f"/projects/{project.id}/retcon/replay-sessions/{state['id']}/step")
        assert stepped.status_code == 200, stepped.text
        state = stepped.json()
    committed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{state['id']}/commit", json={"explicit_confirmation": True})
    assert committed.status_code == 200, committed.text
    return project, old_scene, session.get(RetconReplaySession, state["id"])


def test_completed_replay_indexes_retcon_replay_and_replacement(session, monkeypatch):
    project, old_scene, replay = _completed_replay(session, monkeypatch)
    session.expire_all()
    assert session.get(RetconApplication, replay.retcon_application_id).status == "REPLAY_COMPLETED"
    assert session.scalar(select(TimelineEvent).where(TimelineEvent.source_key == f"REPLAY_SESSION:{replay.id}"))
    assert session.scalar(select(TimelineEvent).where(TimelineEvent.source_key == f"RETCON_APPLICATION:{replay.retcon_application_id}"))
    replacement = session.get(Scene, old_scene.superseded_by_scene_id)
    link = session.scalar(select(CausalLink).where(CausalLink.cause_id == old_scene.id, CausalLink.effect_id == replacement.id, CausalLink.relation_type == CausalRelationType.REPLAY_REPLACED_SCENE))
    assert link and link.active


def test_replay_ledger_failure_rolls_back_derived_rows(session, monkeypatch):
    from test_retcon_replay import historical_replay_world
    project, _old_scene, _actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    while created["cursor"] < len(created["queue"]):
        created = client.post(f"/projects/{project.id}/retcon/replay-sessions/{created['id']}/step").json()
    counts = (session.query(TimelineEvent).count(), session.query(CausalLink).count(), session.query(Scene).count(), session.query(WorldSnapshot).count())
    monkeypatch.setattr(CausalLedgerService, "failure_injector", staticmethod(lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_LEDGER_FAILURE")) if stage == "AFTER_CAUSAL_LEDGER_SYNC" else None))
    failed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{created['id']}/commit", json={"explicit_confirmation": True})
    assert failed.status_code == 409 and failed.json()["detail"]["code"] == "REPLAY_COMMIT_FAILED"
    with sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)() as fresh:
        assert counts == (fresh.query(TimelineEvent).count(), fresh.query(CausalLink).count(), fresh.query(Scene).count(), fresh.query(WorldSnapshot).count())


def test_preserved_scene_refreshes_checkpoint_derived_ledger_rows(session, monkeypatch):
    from app.historical import CurrentSceneCheckpointResolver
    from test_scene_checkpoint_unification import historical_multiscene_replay

    fixture = historical_multiscene_replay(session, monkeypatch)
    _scene2, scene3, _scene4 = fixture.scenes
    CausalLedgerBackfillService().backfill(session, fixture.project.id)
    old_checkpoint = CurrentSceneCheckpointResolver().current(session, fixture.project.id, scene3.id)
    old_scene_event = session.scalar(select(TimelineEvent).where(
        TimelineEvent.project_id == fixture.project.id,
        TimelineEvent.event_type == TimelineEventType.SCENE_OCCURRED,
        TimelineEvent.scene_id == scene3.id,
        TimelineEvent.active.is_(True),
    ))
    old_state_events = session.scalars(select(TimelineEvent).where(
        TimelineEvent.scene_id == scene3.id,
        TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        TimelineEvent.checkpoint_id == old_checkpoint.id,
        TimelineEvent.active.is_(True),
    )).all()
    assert old_scene_event and old_state_events
    old_scene_fingerprint = old_scene_event.event_fingerprint

    created = fixture.client.post(
        f"/projects/{fixture.project.id}/retcon/applications/{fixture.application_id}/replay-sessions"
    ).json()
    while created["cursor"] < len(created["queue"]):
        stepped = fixture.client.post(
            f"/projects/{fixture.project.id}/retcon/replay-sessions/{created['id']}/step"
        )
        assert stepped.status_code == 200, stepped.text
        created = stepped.json()
    committed = fixture.client.post(
        f"/projects/{fixture.project.id}/retcon/replay-sessions/{created['id']}/commit",
        json={"explicit_confirmation": True},
    )
    assert committed.status_code == 200, committed.text
    session.expire_all()

    current_checkpoint = CurrentSceneCheckpointResolver().current(session, fixture.project.id, scene3.id)
    assert current_checkpoint.id != old_checkpoint.id and current_checkpoint.version == old_checkpoint.version + 1
    current_scene_event = session.get(TimelineEvent, old_scene_event.id)
    assert current_scene_event.active is True
    assert current_scene_event.checkpoint_id == current_checkpoint.id
    assert current_scene_event.structured_payload["checkpoint_id"] == current_checkpoint.id
    assert current_scene_event.event_fingerprint != old_scene_fingerprint
    assert all(session.get(TimelineEvent, event.id).active is False for event in old_state_events)
    current_states = session.scalars(select(TimelineEvent).where(
        TimelineEvent.scene_id == scene3.id,
        TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        TimelineEvent.checkpoint_id == current_checkpoint.id,
        TimelineEvent.active.is_(True),
    )).all()
    assert current_states
    for state in current_states:
        same_scope = session.scalars(select(TimelineEvent).where(
            TimelineEvent.scene_id == scene3.id,
            TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
            TimelineEvent.target_type == state.target_type,
            TimelineEvent.target_id == state.target_id,
            TimelineEvent.path == state.path,
            TimelineEvent.active.is_(True),
        )).all()
        assert same_scope == [state]
    history = fixture.client.get(
        f"/projects/{fixture.project.id}/causal-ledger/state-history",
        params={"target_type": current_states[0].target_type, "target_id": current_states[0].target_id, "path": current_states[0].path},
    )
    assert history.status_code == 200
    assert {entry["checkpoint_id"] for entry in history.json()} == {current_checkpoint.id}
    why = fixture.client.get(
        f"/projects/{fixture.project.id}/causal-ledger/why-state",
        params={"target_type": current_states[0].target_type, "target_id": current_states[0].target_id, "path": current_states[0].path},
    )
    assert why.status_code == 200 and why.json()["event"]["checkpoint_id"] == current_checkpoint.id


def test_project_time_transition_is_extracted():
    before, after = _world(), _world()
    after["project"]["current_world_time"] = "2040-01-02T00:00:00"
    assert SceneStateTransitionExtractor().extract(before, after)[0].path == "/current_world_time"


def test_entity_active_transition_is_extracted():
    before, after = _world(), _world()
    after["world_entities"][0]["active"] = False
    assert any(row.path == "/active" for row in SceneStateTransitionExtractor().extract(before, after))


def test_character_current_state_transition_is_extracted():
    before, after = _world(), _world()
    after["characters"][0]["current_state"] = {"location_id": "b"}
    assert any(row.path == "/current_state/location_id" for row in SceneStateTransitionExtractor().extract(before, after))


def test_character_relationship_transition_is_extracted():
    before, after = _world(), _world()
    after["characters"][0]["relationships"] = {"b": {"trust": 0.7}}
    assert any(row.path == "/relationships/b/trust" for row in SceneStateTransitionExtractor().extract(before, after))


def test_character_physical_transition_is_extracted():
    before, after = _world(), _world()
    after["characters"][0]["physical_state"] = {"healthy": False}
    assert any(row.path == "/physical_state/healthy" for row in SceneStateTransitionExtractor().extract(before, after))


def test_character_emotional_transition_is_extracted():
    before, after = _world(), _world()
    after["characters"][0]["emotional_state"] = {"mood": "afraid"}
    assert any(row.path == "/emotional_state/mood" for row in SceneStateTransitionExtractor().extract(before, after))


def test_story_thread_state_transition_is_extracted():
    before, after = _world(), _world()
    after["story_threads"][0]["state"] = {"phase": "middle"}
    assert any(row.path == "/state/phase" for row in SceneStateTransitionExtractor().extract(before, after))


def test_story_thread_status_transition_is_extracted():
    before, after = _world(), _world()
    after["story_threads"][0]["status"] = "PAUSED"
    assert any(row.path == "/status" for row in SceneStateTransitionExtractor().extract(before, after))


def test_story_thread_progress_transition_is_extracted():
    before, after = _world(), _world()
    after["story_threads"][0]["progress"] = 0.5
    assert any(row.path == "/progress" for row in SceneStateTransitionExtractor().extract(before, after))


def test_list_transitions_remain_whole_path_audits():
    before, after = _world(inventory=[]), _world(inventory=["key", "map"])
    rows = SceneStateTransitionExtractor().extract(before, after)
    assert [(row.path, row.after_value) for row in rows if row.target_type == "CHARACTER"] == [("/inventory", ["key", "map"])]


def test_extractor_does_not_mutate_snapshot_inputs():
    before, after = _world(), _world(opened=True)
    original = copy.deepcopy((before, after))
    SceneStateTransitionExtractor().extract(before, after)
    assert (before, after) == original
