import copy
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api
from app.db import Base
from app.director import DirectorContextBuilder
from app.main import app
from app.models import (
    ActionVisibility, CharacterDecision, CharacterDecisionStatus, CharacterDecisionType,
    Character, CharacterKnowledge, CharacterMemory, EntityType, ExecutionStage, ExecutionStatus, ExecutionTrace,
    PerformanceMode, PerformanceStatus, ProposalStatus, ResolutionOutcome, ResolutionStatus,
    ResolverMode, Scene, SceneCommit, SceneExecutionBinding, ScenePerformance,
    RetconImpactItem, ScenePerformanceTurn, SceneStateCheckpoint, StateDeltaBatch, StateDeltaBatchStatus, StateDeltaItem,
    StoryThread, ThreadStatus, WorldEntity, WorldResolution, WorldSnapshot,
)
from app.scene_commit import SceneCommitService
from app.historical import SceneCheckpointIntegrityValidator
from app.snapshot_storage import CompactSnapshotAudit, ProjectWorldSnapshotHeadService, SnapshotPayloadResolver
from app.state_delta import StateDeltaCandidateBuilder
from app.state_delta_validation import StateDeltaValidator
from app.world_resolution import WorldResolutionPayload
from test_character_mind import seed


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def effect(entity_id, value=True, path="/profile/opened"):
    return {
        "effect_kind": "STATE_CHANGE", "target_type": "WORLD_ENTITY", "target_id": entity_id,
        "domain": "WORLD_ENTITY_PROFILE", "operation": "SET", "path": path,
        "value": value, "reason": "Structured commit fixture",
        "evidence": {"request_kind": "INTERACT", "target_entity_id": entity_id},
    }


def structured_effect(target_type, target_id, domain, operation, path, value):
    return {
        "effect_kind": "STATE_CHANGE", "target_type": target_type, "target_id": target_id,
        "domain": domain, "operation": operation, "path": path, "value": value,
        "reason": "Structured commit fixture", "evidence": {"source": "scene-commit", "target_id": target_id},
    }


def prepared_commit(session, monkeypatch, *, effects=None, requires_resolution=True, outcome=ResolutionOutcome.SUCCESS):
    project, location, actor, other, _outsider, proposal = seed(session)
    location.profile = {"openable": True, "opened": False, "locked": False}
    proposal.status = ProposalStatus.APPROVED
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance = ScenePerformance(
        project_id=project.id, scene_proposal_id=proposal.id, take_number=1,
        proposal_context_fingerprint=proposal.context_fingerprint, mode=PerformanceMode.HEURISTIC,
        status=PerformanceStatus.RUNNING, participant_order=[actor.id, other.id],
        active_participant_ids=[actor.id, other.id], max_turns=2, turn_count=1,
    )
    decision = CharacterDecision(
        project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id,
        context_fingerprint="scene-commit", decision_type=CharacterDecisionType.ACT,
        intent="open", chosen_action="INTERACT", motivation="fixture", goal_refs=[],
        knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={},
        perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[],
        refused_options=[], boundary_override_reason=None, decision_summary="Open the entity.",
        status=CharacterDecisionStatus.VALID,
    )
    session.add_all([performance, decision])
    session.flush()
    turn = ScenePerformanceTurn(
        project_id=project.id, performance_id=performance.id, sequence=1,
        actor_character_id=actor.id, actor_context_fingerprint="scene-commit",
        character_decision_id=decision.id, action_visibility=ActionVisibility.PUBLIC,
        observable_action="opens the entity", spoken_content=None,
        recipient_character_ids=[other.id], requires_world_resolution=requires_resolution,
        world_resolution_request={"kind": "INTERACT", "target_entity_id": location.id} if requires_resolution else None,
        validation_result={},
    )
    session.add(turn)
    session.flush()
    resolution = None
    batch = None
    if requires_resolution:
        payload = WorldResolutionPayload.model_validate({
            "outcome": outcome, "outcome_summary": "The entity opens.",
            "objective_facts": [{"subject_type": "ENTITY", "subject_id": location.id, "predicate": "opened", "value": True}],
            "state_effects": effects if effects is not None else [effect(location.id)],
            "actor_observation": "The entity opens.", "public_observation": "The entity opens.",
            "canon_fact_ids_used": [], "world_entity_ids_used": [location.id],
            "resolution_basis_summary": "structured", "missing_information": [],
        })
        resolution = WorldResolution(
            project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id,
            resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="scene-commit",
            status=ResolutionStatus.VALID, recipient_character_ids=[actor.id, other.id],
            **payload.model_dump(mode="json"),
        )
        session.add(resolution)
    session.commit()
    if resolution:
        batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
        session.commit()
        StateDeltaValidator().validate(session, project.id, batch.id)
        session.commit()
    return project, location, actor, other, proposal, performance, turn, resolution, batch, client_for(session, monkeypatch)


def add_resolution_turn(session, project, location, proposal, performance, actor, sequence, effects, *, facts=None):
    decision = CharacterDecision(
        project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id,
        context_fingerprint="scene-commit", decision_type=CharacterDecisionType.ACT,
        intent="act", chosen_action="INTERACT", motivation="fixture", goal_refs=[], knowledge_used=[],
        memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None,
        accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[],
        boundary_override_reason=None, decision_summary="Follow up.", status=CharacterDecisionStatus.VALID,
    )
    session.add(decision); session.flush()
    turn = ScenePerformanceTurn(
        project_id=project.id, performance_id=performance.id, sequence=sequence, actor_character_id=actor.id,
        actor_context_fingerprint="scene-commit", character_decision_id=decision.id,
        action_visibility=ActionVisibility.PUBLIC, observable_action="continues", spoken_content=None,
        recipient_character_ids=[], requires_world_resolution=True,
        world_resolution_request={"kind": "INTERACT", "target_entity_id": location.id}, validation_result={},
    )
    session.add(turn); session.flush()
    payload = WorldResolutionPayload.model_validate({
        "outcome": "SUCCESS", "outcome_summary": "Follow-up.",
        "objective_facts": facts if facts is not None else [{"subject_type": "ENTITY", "subject_id": location.id, "predicate": f"effect.{sequence}", "value": True}],
        "state_effects": effects, "actor_observation": "Observed.", "public_observation": None,
        "canon_fact_ids_used": [], "world_entity_ids_used": [location.id],
        "resolution_basis_summary": "structured", "missing_information": [],
    })
    resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id,
        resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint=f"scene-commit-{sequence}",
        status=ResolutionStatus.VALID, recipient_character_ids=[actor.id], **payload.model_dump(mode="json"))
    session.add(resolution); performance.turn_count = sequence; session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    return turn, resolution, batch


def test_commit_scene_applies_validated_entity_delta_and_materializes_history(session, monkeypatch):
    project, location, actor, other, proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    before = copy.deepcopy(location.profile)
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["idempotent"] is False
    assert body["scene"]["status"] == "OCCURRED" and body["scene"]["history_status"] == "ACTIVE"
    assert body["scene_commit"]["status"] == "COMMITTED"
    session.refresh(location); session.refresh(batch); session.refresh(performance); session.refresh(proposal)
    assert before["opened"] is False and location.profile["opened"] is True
    assert batch.status == StateDeltaBatchStatus.APPLIED and batch.applied_scene_id == body["scene"]["id"]
    assert performance.status == PerformanceStatus.COMPLETED and proposal.status == ProposalStatus.EXECUTED
    assert session.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == body["scene"]["id"], SceneExecutionBinding.active.is_(True)))
    checkpoint = session.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.scene_id == body["scene"]["id"]))
    assert checkpoint.capture_protocol_version == 4 and checkpoint.version == 1 and checkpoint.active is True
    assert checkpoint.origin == "NORMAL_COMMIT" and checkpoint.source_scene_commit_id == body["scene_commit"]["id"]


def test_commit_scene_is_idempotent(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    first = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    second = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert first.status_code == second.status_code == 200
    assert first.json()["scene"]["id"] == second.json()["scene"]["id"]
    assert second.json()["idempotent"] is True
    assert session.scalar(select(func.count(Scene.id)).where(Scene.project_id == project.id)) == 1


def test_zero_item_validated_resolution_still_commits(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch, effects=[])
    assert session.scalar(select(func.count(StateDeltaItem.id)).where(StateDeltaItem.batch_id == batch.id)) == 0
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200 and response.json()["scene"]["status"] == "OCCURRED"
    session.refresh(batch); session.refresh(location)
    assert batch.status == StateDeltaBatchStatus.APPLIED and location.profile["opened"] is False


def test_non_resolving_turn_commits_without_delta(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, resolution, batch, client = prepared_commit(session, monkeypatch, requires_resolution=False)
    assert resolution is None and batch is None
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200 and response.json()["scene_commit"]["delta_batch_ids"] == []


def test_commit_requires_current_validated_delta(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    batch.status = StateDeltaBatchStatus.CANDIDATE; session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_DELTA_NOT_VALIDATED"
    session.refresh(location); assert location.profile["opened"] is False


def test_commit_rejects_empty_performance(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch, requires_resolution=False)
    session.query(ScenePerformanceTurn).filter(ScenePerformanceTurn.performance_id == performance.id).delete(); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_EMPTY_PERFORMANCE"


def test_commit_rejects_invalid_paused_reason(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "WORLD_INFORMATION_MISSING"; session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_PERFORMANCE_NOT_READY"


def test_commit_rejects_invalid_resolution_lineage(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.status = ResolutionStatus.REJECTED; session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_RESOLUTION_INVALID"
    assert session.get(ScenePerformanceTurn, turn.id)


def test_commit_rejects_world_stale(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    unrelated = WorldEntity(project_id=project.id, entity_type=EntityType.CUSTOM, name="Unrelated", profile={"external": True})
    session.add(unrelated); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_WORLD_STALE"


def test_commit_rejects_source_changed(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.state_effects = [effect(location.id, path="/profile/locked", value=True)]; session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_SOURCE_CHANGED"


def test_commit_rejects_rehashed_item_tamper(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    item = session.scalar(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id))
    item.after_value = False
    from app.state_delta import StateEffectPayload, state_delta_item_fingerprint
    payload = StateEffectPayload.model_validate(item.evidence["state_effect"])
    item.semantic_fingerprint = state_delta_item_fingerprint(project.id, item.source_resolution_id, item.source_turn_id, payload, item.before_value, item.after_value, item.evidence)
    session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_ITEM_INTEGRITY_INVALID"
    session.refresh(location); assert location.profile["opened"] is False


def test_commit_rejects_cross_batch_path_conflict(session, monkeypatch):
    project, location, actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    add_resolution_turn(session, project, location, proposal, performance, actor, 2, [effect(location.id, value=True)])
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_CROSS_BATCH_PATH_CONFLICT"


def test_commit_rechecks_combined_inventory_ownership(session, monkeypatch):
    project, location, actor, other, proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch, effects=[])
    item = WorldEntity(project_id=project.id, entity_type=EntityType.ITEM, name="Key", profile={})
    session.add(item); session.flush()
    resolution.objective_facts = [{"subject_type": "CHARACTER", "subject_id": actor.id, "predicate": "inventory", "value": item.id}]
    resolution.state_effects = [structured_effect("CHARACTER", actor.id, "CHARACTER_INVENTORY", "ADD", "/inventory", item.id)]
    session.commit()
    first = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit(); StateDeltaValidator().validate(session, project.id, first.id); session.commit()
    add_resolution_turn(session, project, location, proposal, performance, other, 2, [structured_effect("CHARACTER", other.id, "CHARACTER_INVENTORY", "ADD", "/inventory", item.id)], facts=[{"subject_type": "CHARACTER", "subject_id": other.id, "predicate": "inventory", "value": item.id}])
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_FINAL_WORLD_INVALID"


def test_commit_applies_character_domains(session, monkeypatch):
    project, location, actor, other, proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch, effects=[])
    destination = WorldEntity(project_id=project.id, entity_type=EntityType.LOCATION, name="Destination", profile={})
    item = WorldEntity(project_id=project.id, entity_type=EntityType.ITEM, name="Token", profile={})
    session.add_all([destination, item]); session.flush()
    actor.physical_state = {"healthy": True}; actor.emotional_state = {"mood": "calm"}
    resolution.objective_facts = [{"subject_type": "CHARACTER", "subject_id": actor.id, "predicate": "state", "value": "changed"}]
    resolution.state_effects = [
        structured_effect("CHARACTER", actor.id, "CHARACTER_LOCATION", "SET", "/current_state/location_id", destination.id),
        structured_effect("CHARACTER", actor.id, "CHARACTER_INVENTORY", "ADD", "/inventory", item.id),
        structured_effect("CHARACTER", actor.id, "CHARACTER_RELATIONSHIP", "UPSERT", f"/relationships/{other.id}/trust", 0.5),
        structured_effect("CHARACTER", actor.id, "CHARACTER_PHYSICAL_STATE", "SET", "/physical_state/healthy", False),
        structured_effect("CHARACTER", actor.id, "CHARACTER_EMOTIONAL_STATE", "SET", "/emotional_state/mood", "alert"),
    ]
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance.proposal_context_fingerprint = proposal.context_fingerprint
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    assert batch.status == StateDeltaBatchStatus.VALIDATED, batch.validation_report
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    session.refresh(actor)
    assert actor.current_state["location_id"] == destination.id and item.id in actor.inventory
    assert actor.relationships[other.id]["trust"] == 0.5
    assert actor.physical_state["healthy"] is False and actor.emotional_state["mood"] == "alert"


def test_commit_applies_story_thread_domains(session, monkeypatch):
    project, location, _actor, _other, proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch, effects=[])
    thread = session.get(StoryThread, proposal.primary_thread_id)
    resolution.objective_facts = [{"subject_type": "SCENE", "subject_id": performance.id, "predicate": "thread", "value": True}]
    resolution.state_effects = [
        structured_effect("STORY_THREAD", thread.id, "STORY_THREAD_PROGRESS", "SET", "/progress", 0.6),
        structured_effect("STORY_THREAD", thread.id, "STORY_THREAD_STATE", "UPSERT", "/state/phase", "middle"),
        structured_effect("STORY_THREAD", thread.id, "STORY_THREAD_STATUS", "SET", "/status", "PAUSED"),
    ]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    assert batch.status == StateDeltaBatchStatus.VALIDATED, batch.validation_report["issues"]
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    session.refresh(thread)
    assert thread.progress == 0.6 and thread.state["phase"] == "middle" and thread.status == ThreadStatus.PAUSED


def test_commit_applies_world_time_as_canonical_naive_datetime(session, monkeypatch):
    project, location, _actor, _other, proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch, effects=[])
    project.current_world_time = datetime.fromisoformat("2040-01-01T00:00:00")
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance.proposal_context_fingerprint = proposal.context_fingerprint
    resolution.objective_facts = [{"subject_type": "SCENE", "subject_id": performance.id, "predicate": "time", "value": True}]
    resolution.state_effects = [structured_effect("PROJECT", project.id, "WORLD_TIME", "SET", "/current_world_time", "2040-01-02T08:00:00+08:00")]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    session.refresh(project)
    assert project.current_world_time == datetime.fromisoformat("2040-01-02T00:00:00")
    assert response.json()["scene"]["world_time"] == "2040-01-02T00:00:00"


def test_commit_aggregates_multiple_resolution_facts_and_lineage(session, monkeypatch):
    project, location, actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    location.profile = {**location.profile, "locked": False}; session.commit()
    # Re-derive the first batch after the fixture's intentional setup change.
    first_resolution = session.scalar(select(WorldResolution).where(WorldResolution.performance_id == performance.id))
    first_resolution.state_effects = [effect(location.id, path="/profile/opened", value=True)]; session.commit()
    first = StateDeltaCandidateBuilder().derive(session, project.id, first_resolution.id)[0]; session.commit(); StateDeltaValidator().validate(session, project.id, first.id); session.commit()
    _turn2, resolution2, batch2 = add_resolution_turn(session, project, location, proposal, performance, actor, 2, [effect(location.id, path="/profile/locked", value=True)])
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200
    scene = response.json()["scene"]
    assert len(scene["facts"]) == 2 and len(scene["result"]["resolutions"]) == 2
    session.refresh(first); session.refresh(batch2)
    assert first.status == batch2.status == StateDeltaBatchStatus.APPLIED and resolution2.id in [entry["resolution_id"] for entry in scene["result"]["resolutions"]]


def test_commit_materializes_observation_cognition_without_hidden_fact_leak(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.objective_facts.append({"subject_type": "ENTITY", "subject_id": resolution.world_entity_ids_used[0], "predicate": "hidden", "value": True})
    session.commit()
    # The source change requires a new current candidate; it still contains only the observed effect fact.
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200
    scene_id = response.json()["scene"]["id"]
    actor_knowledge = session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == actor.id, CharacterKnowledge.source == scene_id)).all()
    other_knowledge = session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == other.id, CharacterKnowledge.source == scene_id)).all()
    memories = session.scalars(select(CharacterMemory).where(CharacterMemory.source_scene == scene_id)).all()
    assert len(actor_knowledge) == 1 and "opened" in actor_knowledge[0].proposition and not other_knowledge
    assert {row.character_id for row in memories} == {actor.id, other.id}


def test_commit_checkpoint_contains_pre_and_post_formal_world(session, monkeypatch):
    project, location, actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    checkpoint = session.get(SceneStateCheckpoint, body["checkpoint"]["id"])
    pre = session.get(WorldSnapshot, checkpoint.pre_snapshot_id)
    post = session.get(WorldSnapshot, checkpoint.post_snapshot_id)
    resolver = SnapshotPayloadResolver()
    pre_payload, post_payload = resolver.materialize(session, pre), resolver.materialize(session, post)
    pre_entity = next(row for row in pre_payload["world_entities"] if row["id"] == location.id)
    post_entity = next(row for row in post_payload["world_entities"] if row["id"] == location.id)
    assert pre_entity["profile"]["opened"] is False and post_entity["profile"]["opened"] is True
    assert not any(row["id"] == body["scene"]["id"] for row in pre_payload["scenes"])
    assert any(row["id"] == body["scene"]["id"] for row in post_payload["scenes"])
    assert not any(row.get("source") == body["scene"]["id"] for row in pre_payload["character_knowledge"])
    assert any(row.get("character_id") == actor.id and row.get("source") == body["scene"]["id"] for row in post_payload["character_knowledge"])
    assert checkpoint.pre_state_fingerprint == pre.state_fingerprint
    assert checkpoint.post_state_fingerprint == post.state_fingerprint
    assert checkpoint.checkpoint_fingerprint
    SceneCheckpointIntegrityValidator().validate_integrity(session, checkpoint)


def test_normal_checkpoint_uses_anchor_then_reference_and_compact_delta(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    session.expire_all()
    checkpoint = session.get(SceneStateCheckpoint, body["checkpoint"]["id"])
    pre, post = session.get(WorldSnapshot, checkpoint.pre_snapshot_id), session.get(WorldSnapshot, checkpoint.post_snapshot_id)
    assert pre.storage_mode == "COMPACT_ANCHOR"
    assert post.storage_mode == "COMPACT_DELTA"
    assert "scenes" not in post.payload and "collections" in post.payload
    assert SnapshotPayloadResolver().materialize(session, post) == __import__("app.versioning", fromlist=["WorldSnapshotBuilder"]).WorldSnapshotBuilder().build(session, project.id)[0]
    ProjectWorldSnapshotHeadService().audit(session, project.id)
    CompactSnapshotAudit().audit_current_formal_state(session, project.id)
    from app.historical import SceneCheckpointService
    next_pre = SceneCheckpointService().capture_formal_pre(session, project.id)
    assert next_pre.storage_mode == "REFERENCE" and next_pre.base_snapshot_id == post.id


def test_checkpoint_read_api_is_metadata_only(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    committed = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    current = client.get(f"/projects/{project.id}/scenes/{committed['scene']['id']}/checkpoint")
    history = client.get(f"/projects/{project.id}/scenes/{committed['scene']['id']}/checkpoints")
    assert current.status_code == history.status_code == 200
    assert current.json()["id"] == committed["checkpoint"]["id"] and current.json()["capture_protocol_version"] == 4
    assert "payload" not in current.json() and [row["version"] for row in history.json()] == [1]


def test_commit_invalidates_sibling_performances(session, monkeypatch):
    project, _location, _actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    sibling = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=2,
        proposal_context_fingerprint=proposal.context_fingerprint, mode=PerformanceMode.HEURISTIC,
        status=PerformanceStatus.READY, participant_order=[], active_participant_ids=[], max_turns=1, turn_count=0)
    session.add(sibling); session.commit()
    assert client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").status_code == 200
    session.refresh(sibling)
    assert sibling.status == PerformanceStatus.INVALIDATED and sibling.stop_reason == "PROPOSAL_EXECUTED"


def test_commit_writes_successful_execution_trace(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    trace = session.scalar(select(__import__("app.models", fromlist=["ExecutionTrace"]).ExecutionTrace).where(
        __import__("app.models", fromlist=["ExecutionTrace"]).ExecutionTrace.source_id == performance.id,
        __import__("app.models", fromlist=["ExecutionTrace"]).ExecutionTrace.stage == ExecutionStage.SCENE_COMMIT,
    ))
    assert trace.status == ExecutionStatus.SUCCEEDED and trace.provider is None and trace.model is None
    assert trace.input_fingerprint == body["scene_commit"]["source_fingerprint"]


def test_commit_failure_after_materialization_rolls_back_everything(session, monkeypatch):
    project, location, _actor, _other, proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    before = copy.deepcopy(location.profile)
    counts = {
        "scenes": session.scalar(select(func.count(Scene.id)).where(Scene.project_id == project.id)),
        "commits": session.scalar(select(func.count(SceneCommit.id)).where(SceneCommit.project_id == project.id)),
        "bindings": session.scalar(select(func.count(SceneExecutionBinding.id)).where(SceneExecutionBinding.project_id == project.id)),
        "checkpoints": session.scalar(select(func.count(SceneStateCheckpoint.id)).where(SceneStateCheckpoint.project_id == project.id)),
        "snapshots": session.scalar(select(func.count(WorldSnapshot.id)).where(WorldSnapshot.project_id == project.id)),
        "traces": session.scalar(select(func.count(ExecutionTrace.id)).where(ExecutionTrace.project_id == project.id)),
        "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))),
        "memory": session.scalar(select(func.count(CharacterMemory.id))),
    }
    monkeypatch.setattr(SceneCommitService, "failure_injector", staticmethod(lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_SCENE_COMMIT_FAILURE")) if stage == "AFTER_SCENE_COMMIT_MATERIALIZATION" else None))
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_FAILED"
    session.expire_all(); session.refresh(location); session.refresh(batch); session.refresh(performance); session.refresh(proposal)
    assert location.profile == before and batch.status == StateDeltaBatchStatus.VALIDATED and batch.applied_scene_id is None
    assert performance.status == PerformanceStatus.RUNNING and proposal.status == ProposalStatus.APPROVED
    assert session.scalar(select(func.count(Scene.id)).where(Scene.project_id == project.id)) == counts["scenes"]
    assert session.scalar(select(func.count(SceneCommit.id)).where(SceneCommit.project_id == project.id)) == counts["commits"]
    assert session.scalar(select(func.count(SceneExecutionBinding.id)).where(SceneExecutionBinding.project_id == project.id)) == counts["bindings"]
    assert session.scalar(select(func.count(SceneStateCheckpoint.id)).where(SceneStateCheckpoint.project_id == project.id)) == counts["checkpoints"]
    assert session.scalar(select(func.count(WorldSnapshot.id)).where(WorldSnapshot.project_id == project.id)) == counts["snapshots"]
    assert session.scalar(select(func.count(ExecutionTrace.id)).where(ExecutionTrace.project_id == project.id)) == counts["traces"]
    assert session.scalar(select(func.count(CharacterKnowledge.id))) == counts["knowledge"] and session.scalar(select(func.count(CharacterMemory.id))) == counts["memory"]


def test_apply_result_verification_failure_rolls_back(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    def corrupt(db, prepared):
        target = db.get(WorldEntity, prepared.items[0].target_id)
        target.profile = {**target.profile, "opened": False}
    monkeypatch.setattr(SceneCommitService, "apply_verifier", staticmethod(corrupt))
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_APPLY_RESULT_MISMATCH"
    session.expire_all(); session.refresh(location); session.refresh(batch)
    assert location.profile["opened"] is False and batch.status == StateDeltaBatchStatus.VALIDATED


def test_pending_replay_blocks_normal_scene_commit(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    from app.models import RetconApplication, RetconApplicationStatus
    session.add(RetconApplication(project_id=project.id, retcon_request_id="request", retcon_plan_id="plan", source_revision_id="revision", status=RetconApplicationStatus.APPLIED_PENDING_REPLAY, plan_basis_fingerprint="basis", pre_apply_world_fingerprint="pre"))
    session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "RETCON_REPLAY_REQUIRED"


def test_commit_rejects_proposal_taken_by_another_performance(session, monkeypatch):
    project, _location, _actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    proposal.status = ProposalStatus.EXECUTED; session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_PROPOSAL_ALREADY_EXECUTED"


def test_commit_rejects_context_stale_performance(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    performance.proposal_context_fingerprint = "stale"; session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCENE_COMMIT_CONTEXT_STALE"


def test_quiescent_paused_performance_can_commit(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    performance.status = PerformanceStatus.PAUSED
    performance.stop_reason = "QUIESCENT"
    session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text
    assert response.json()["scene_commit"]["status"] == "COMMITTED"


def test_non_resolving_turn_with_resolution_is_rejected(session, monkeypatch):
    project, location, actor, _other, proposal, performance, turn, _resolution, _batch, client = prepared_commit(
        session, monkeypatch, requires_resolution=False
    )
    payload = WorldResolutionPayload.model_validate({
        "outcome": "SUCCESS", "outcome_summary": "Unexpected resolution.",
        "objective_facts": [{"subject_type": "ENTITY", "subject_id": location.id, "predicate": "opened", "value": True}],
        "state_effects": [], "actor_observation": None, "public_observation": None,
        "canon_fact_ids_used": [], "world_entity_ids_used": [location.id],
        "resolution_basis_summary": "fixture", "missing_information": [],
    })
    session.add(WorldResolution(
        project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id,
        resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="fixture",
        status=ResolutionStatus.VALID, recipient_character_ids=[actor.id], **payload.model_dump(mode="json"),
    ))
    session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCENE_COMMIT_EXECUTION_LINEAGE_INVALID"


def test_applied_batch_audit_links_are_immutable_commit_lineage(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    session.refresh(batch)
    assert batch.status == StateDeltaBatchStatus.APPLIED
    assert batch.applied_scene_id == body["scene"]["id"]
    assert batch.applied_commit_id == body["scene_commit"]["id"]
    assert batch.applied_at is not None


def test_active_cognition_reader_sees_only_new_scene_cognition(session, monkeypatch):
    project, _location, actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    from app.character_mind import ActiveCharacterCognitionReader
    knowledge = ActiveCharacterCognitionReader().knowledge(session, project.id, actor.id)
    memories = ActiveCharacterCognitionReader().memories(session, project.id, actor.id)
    assert any(row.source == body["scene"]["id"] and "opened" in row.proposition for row in knowledge)
    assert any(row.source_scene == body["scene"]["id"] for row in memories)


def test_state_delta_apply_route_is_not_exposed(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = prepared_commit(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/state-delta-batches/{batch.id}/apply")
    assert response.status_code == 404


def test_scene_commit_database_integrity_error_is_normalized(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    original_apply = SceneCommitService.apply_engine.apply

    def add_duplicate_commit(db, project_id, items):
        original_apply(db, project_id, items)
        original = db.scalar(select(SceneCommit).where(
            SceneCommit.project_id == project_id,
            SceneCommit.performance_id == performance.id,
        ))
        db.add(SceneCommit(
            project_id=project_id, proposal_id=original.proposal_id, performance_id=performance.id,
            status=original.status, delta_batch_ids=[], source_fingerprint="duplicate-test",
        ))

    monkeypatch.setattr(SceneCommitService.apply_engine, "apply", add_duplicate_commit)
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "SCENE_COMMIT_INTEGRITY_ERROR"}


def test_normal_scene_binding_is_replay_resource_mapper_compatible(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    plan_id = "normal-scene-commit-plan"
    session.add_all([
        RetconImpactItem(plan_id=plan_id, resource_type="CHARACTER_DECISION", resource_id=turn.character_decision_id,
            classification="REPLAY", reason_code="TEST", reason_summary="fixture", scene_id=scene_id),
        RetconImpactItem(plan_id=plan_id, resource_type="SCENE_PERFORMANCE_TURN", resource_id=turn.id,
            classification="REPLAY", reason_code="TEST", reason_summary="fixture", scene_id=scene_id),
        RetconImpactItem(plan_id=plan_id, resource_type="WORLD_RESOLUTION", resource_id=resolution.id,
            classification="REPLAY", reason_code="TEST", reason_summary="fixture", scene_id=scene_id),
    ])
    session.commit()
    from app.replay import ReplayResourceMapper
    mapped = ReplayResourceMapper().map(session, SimpleNamespace(retcon_plan_id=plan_id), [scene_id])[scene_id]
    assert mapped["decision_ids"] == [turn.character_decision_id]
    assert mapped["turn_ids"] == [turn.id]
    assert mapped["resolution_ids"] == [resolution.id]
    assert mapped["execution_pairs"] == [{"decision_id": turn.character_decision_id, "turn_id": turn.id, "resolution_ids": [resolution.id]}]


def test_scene_facts_are_stably_ordered_within_each_resolution(session, monkeypatch):
    project, location, _actor, _other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.objective_facts = [
        {"subject_type": "ENTITY", "subject_id": location.id, "predicate": "zeta", "value": True},
        {"subject_type": "ENTITY", "subject_id": location.id, "predicate": "alpha", "value": True},
    ]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200
    facts = response.json()["scene"]["facts"]
    assert facts == sorted(facts, key=lambda value: __import__("app.execution_trace", fromlist=["stable_fingerprint"]).stable_fingerprint(value, "scene-fact-v1"))


def test_two_performances_commit_to_distinct_active_scene_sequences(session, monkeypatch):
    project, location, actor, other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    first = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    session.expire_all()
    proposal2 = type(proposal)(
        project_id=project.id, context_fingerprint="pending", proposal_type=proposal.proposal_type,
        primary_thread_id=proposal.primary_thread_id, location_id=proposal.location_id,
        proposed_location=proposal.proposed_location, participants=list(proposal.participants),
        scene_goal="A second structured scene.", character_motivations=copy.deepcopy(proposal.character_motivations),
        entry_state=copy.deepcopy(proposal.entry_state), planned_pressure=proposal.planned_pressure,
        expected_progress=copy.deepcopy(proposal.expected_progress), allowed_reveals=[], forbidden_reveals=[],
        required_canon=[], possible_outcomes=[], new_entity_requests=[], risk_flags=[],
        director_reasoning_summary="fixture", status=ProposalStatus.APPROVED,
    )
    session.add(proposal2); session.flush()
    proposal2.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance2 = ScenePerformance(
        project_id=project.id, scene_proposal_id=proposal2.id, take_number=1,
        proposal_context_fingerprint=proposal2.context_fingerprint, mode=PerformanceMode.HEURISTIC,
        status=PerformanceStatus.RUNNING, participant_order=[actor.id, other.id],
        active_participant_ids=[actor.id, other.id], max_turns=1, turn_count=0,
    )
    session.add(performance2); session.commit()
    add_resolution_turn(session, project, location, proposal2, performance2, actor, 1, [])
    proposal2.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance2.proposal_context_fingerprint = proposal2.context_fingerprint
    session.commit()
    assert proposal2.context_fingerprint == performance2.proposal_context_fingerprint
    assert proposal2.context_fingerprint == DirectorContextBuilder().build(session, project.id)["fingerprint"]
    second = client.post(f"/projects/{project.id}/performances/{performance2.id}/commit-scene")
    assert second.status_code == 200, second.text
    active = session.scalars(select(Scene).where(
        Scene.project_id == project.id, Scene.history_status == "ACTIVE"
    ).order_by(Scene.sequence)).all()
    assert [scene.sequence for scene in active] == [1, 2]
    assert first["scene"]["id"] != second.json()["scene"]["id"]


def test_scene_commit_never_calls_an_ai_provider(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    monkeypatch.setattr(api, "get_model_provider", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Scene Commit must be deterministic")))
    response = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene")
    assert response.status_code == 200, response.text


def test_public_turn_uses_persisted_recipients_not_participant_order(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    withdrawn = session.scalar(select(Character).where(
        Character.project_id == project.id,
        Character.name == "Outsider",
    ))
    performance.participant_order = [actor.id, other.id, withdrawn.id]
    performance.active_participant_ids = [actor.id, other.id]
    turn.action_visibility = ActionVisibility.PUBLIC
    turn.recipient_character_ids = [other.id]
    session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    action_memories = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id, CharacterMemory.content == "opens the entity"
    )).all()
    assert {row.character_id for row in action_memories} == {actor.id, other.id}
    assert not any(row.character_id == withdrawn.id for row in action_memories)


def test_public_resolution_uses_persisted_recipients_not_participant_order(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    withdrawn = session.scalar(select(Character).where(Character.project_id == project.id, Character.name == "Outsider"))
    performance.participant_order = [actor.id, other.id, withdrawn.id]
    performance.active_participant_ids = [actor.id, other.id]
    resolution.recipient_character_ids = [other.id]
    session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    memories = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id, CharacterMemory.content == "The entity opens."
    )).all()
    assert sorted(row.character_id for row in memories) == sorted([actor.id, other.id])


def test_actor_keeps_own_action_memory_when_turn_has_no_recipients(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    turn.recipient_character_ids = []
    session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    action_memories = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id, CharacterMemory.content == "opens the entity"
    )).all()
    assert {row.character_id for row in action_memories} == {actor.id}
    assert not any(row.character_id == other.id for row in action_memories)


def test_turn_preserves_distinct_observable_action_and_spoken_content(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    turn.spoken_content = "The door is open."
    session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    action_memories = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id,
        CharacterMemory.content.in_(["opens the entity", "The door is open."]),
    )).all()
    assert {(row.character_id, row.content) for row in action_memories} == {
        (actor.id, "opens the entity"), (actor.id, "The door is open."),
        (other.id, "opens the entity"), (other.id, "The door is open."),
    }


def test_same_action_content_is_deduplicated_per_turn_and_recipient(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    turn.spoken_content = turn.observable_action
    session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    action_memories = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id, CharacterMemory.content == "opens the entity"
    )).all()
    assert sorted(row.character_id for row in action_memories) == sorted([actor.id, other.id])


def test_actor_and_public_resolution_observation_are_deduplicated(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    assert resolution.actor_observation == resolution.public_observation == "The entity opens."
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    observations = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id, CharacterMemory.content == "The entity opens."
    )).all()
    assert sorted(row.character_id for row in observations) == sorted([actor.id, other.id])


def test_distinct_actor_and_public_resolution_observations_remain_distinct(session, monkeypatch):
    project, _location, actor, other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.actor_observation = "I opened the entity."
    resolution.public_observation = "The entity opens for everyone."
    session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    observations = session.scalars(select(CharacterMemory).where(
        CharacterMemory.source_scene == scene_id,
        CharacterMemory.content.in_([resolution.actor_observation, resolution.public_observation]),
    )).all()
    assert {(row.character_id, row.content) for row in observations} == {
        (actor.id, resolution.actor_observation),
        (actor.id, resolution.public_observation),
        (other.id, resolution.public_observation),
    }


def test_knowledge_uses_canonical_json_and_is_replay_matcher_compatible(session, monkeypatch):
    project, location, actor, _other, proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    location.profile = {**location.profile, "label": "closed", "metadata": {}}
    resolution.objective_facts = [
        {"subject_type": "ENTITY", "subject_id": location.id, "predicate": "opened", "value": True},
        {"subject_type": "ENTITY", "subject_id": location.id, "predicate": "label", "value": "open"},
        {"subject_type": "ENTITY", "subject_id": location.id, "predicate": "metadata", "value": {"a": 1}},
    ]
    resolution.state_effects = [
        effect(location.id, True, "/profile/opened"),
        effect(location.id, "open", "/profile/label"),
        effect(location.id, {"a": 1}, "/profile/metadata"),
    ]
    session.commit()
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance.proposal_context_fingerprint = proposal.context_fingerprint
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    body = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()
    scene_id = body["scene"]["id"]
    assert body["scene_commit"]["applied_delta_count"] == body["scene"]["result"]["applied_item_count"] == 3
    rows = session.scalars(select(CharacterKnowledge).where(
        CharacterKnowledge.character_id == actor.id, CharacterKnowledge.source == scene_id
    )).all()
    propositions = {row.proposition for row in rows}
    assert propositions == {
        f"ENTITY {location.id}: opened = true",
        f"ENTITY {location.id}: label = {json.dumps('open', ensure_ascii=True, sort_keys=True)}",
        f"ENTITY {location.id}: metadata = {json.dumps({'a': 1}, ensure_ascii=True, sort_keys=True)}",
    }
    from app.replay import ReplayCognitionReplacementMatcher
    for row in rows:
        predicate, raw_value = row.proposition.split(": ", 1)[1].split(" = ", 1)
        assert ReplayCognitionReplacementMatcher().knowledge(row, {
            "character_id": actor.id,
            "fact_identity": {"subject_type": "ENTITY", "subject_id": location.id, "predicate": predicate, "value": json.loads(raw_value)},
        }, scene_id)


@pytest.mark.parametrize("facts", [
    [
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "hidden", "value": True},
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "opened", "value": True},
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "locked", "value": False},
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "color", "value": "red"},
    ],
    [
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "color", "value": "red"},
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "locked", "value": False},
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "opened", "value": True},
        {"subject_type": "ENTITY", "subject_id": "LOCATION_ID", "predicate": "hidden", "value": True},
    ],
])
def test_knowledge_uses_exact_effect_fact_regardless_of_fact_order(session, monkeypatch, facts):
    project, location, actor, _other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.objective_facts = [{**fact, "subject_id": location.id} for fact in facts]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    knowledge = session.scalars(select(CharacterKnowledge).where(
        CharacterKnowledge.character_id == actor.id, CharacterKnowledge.source == scene_id
    )).all()
    assert [row.proposition for row in knowledge] == [f"ENTITY {location.id}: opened = true"]


def test_unmatched_effect_fact_applies_world_change_without_creating_knowledge(session, monkeypatch):
    project, location, actor, _other, _proposal, performance, _turn, resolution, _batch, client = prepared_commit(session, monkeypatch)
    resolution.objective_facts = [{"subject_type": "ENTITY", "subject_id": location.id, "predicate": "hidden", "value": True}]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    scene_id = client.post(f"/projects/{project.id}/performances/{performance.id}/commit-scene").json()["scene"]["id"]
    session.refresh(location)
    knowledge = session.scalars(select(CharacterKnowledge).where(
        CharacterKnowledge.character_id == actor.id, CharacterKnowledge.source == scene_id
    )).all()
    assert location.profile["opened"] is True
    assert knowledge == []
