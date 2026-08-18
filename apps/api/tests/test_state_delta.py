import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.main import app
import app.api as api
from app.models import (
    ActionVisibility, CharacterDecision, CharacterDecisionStatus, CharacterDecisionType,
    PerformanceMode, PerformanceStatus, Project, ResolutionStatus, ResolutionOutcome,
    ResolverMode, ScenePerformance, ScenePerformanceTurn, StateDeltaBatch, StoryThread,
    WorldResolution,
)
from app.state_delta import StateDeltaCandidateBuilder, StateEffectPayload
from test_scene_performance import approved_setup


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def resolution_source(session, monkeypatch, facts, *, resolution_status=ResolutionStatus.VALID, project=None):
    if project is None:
        project, location, actor, _other, proposal, _ = approved_setup(session, monkeypatch)
    else:
        location = session.scalar(select(__import__("app.models", fromlist=["WorldEntity"]).WorldEntity).where(__import__("app.models", fromlist=["WorldEntity"]).WorldEntity.project_id == project.id))
        actor = session.scalar(select(__import__("app.models", fromlist=["Character"]).Character).where(__import__("app.models", fromlist=["Character"]).Character.project_id == project.id))
        proposal = session.scalar(select(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal).where(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal.project_id == project.id))
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint="state-delta", decision_type=CharacterDecisionType.OBSERVE, intent="observe", chosen_action="observe", target_character_id=None, target_entity_id=None, motivation="test", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], boundary_override_reason=None, decision_summary="test", status=CharacterDecisionStatus.VALID)
    performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=99, proposal_context_fingerprint="state-delta", mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.COMPLETED, participant_order=[actor.id], active_participant_ids=[actor.id], max_turns=1, turn_count=1)
    session.add_all([decision, performance]); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=performance.id, sequence=1, actor_character_id=actor.id, actor_context_fingerprint="state-delta", character_decision_id=decision.id, action_visibility=ActionVisibility.PRIVATE, observable_action="observe", spoken_content=None, recipient_character_ids=[], requires_world_resolution=True, world_resolution_request={"kind": "INSPECT", "target_entity_id": location.id}, validation_result={"valid": True})
    session.add(turn); session.flush()
    resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="state-delta", status=resolution_status, outcome=ResolutionOutcome.SUCCESS, outcome_summary="structured outcome", objective_facts=facts, actor_observation=None, public_observation=None, recipient_character_ids=[], canon_fact_ids_used=[], world_entity_ids_used=[location.id], resolution_basis_summary="structured", missing_information=[])
    session.add(resolution); session.commit()
    return project, location, actor, proposal, resolution


def effect(target_type, target_id, domain, operation, path, value, reason="structured runtime consequence"):
    return {"effect_kind": "STATE_CHANGE", "target_type": target_type, "target_id": target_id, "domain": domain, "operation": operation, "path": path, "value": value, "reason": reason, "evidence": {"kind": "test"}}


def fact_with_effect(subject_type, subject_id, predicate, value, state_effect):
    return {"subject_type": subject_type, "subject_id": subject_id, "predicate": predicate, "value": value, "state_effect": state_effect}


def test_state_effect_payload_is_strict():
    valid = StateEffectPayload.model_validate(effect("WORLD_ENTITY", "door", "WORLD_ENTITY_PROFILE", "SET", "/profile/locked", True))
    assert valid.effect_kind == "STATE_CHANGE"
    with pytest.raises(ValidationError):
        StateEffectPayload.model_validate({**valid.model_dump(), "freeform_patch": "forbidden"})


def test_safe_entity_effect_creates_candidate_without_mutating_formal_world(session, monkeypatch):
    project, location, actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    location.profile = {"locked": False}; actor.current_state = {"location_id": location.id}; session.commit()
    resolution.objective_facts = [fact_with_effect("ENTITY", location.id, "locked", True, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_PROFILE", "SET", "/profile/locked", True))]
    session.commit(); before = copy.deepcopy(location.profile)
    batch, items, existing = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    assert not existing and len(items) == 1
    assert items[0].before_value is False and items[0].after_value is True
    assert session.get(type(location), location.id).profile == before
    assert session.get(type(actor), actor.id).current_state == {"location_id": location.id}
    assert batch.status.value == "CANDIDATE" and items[0].source_resolution_id == resolution.id


def test_character_allowlist_inventory_relationship_time_and_thread_effects(session, monkeypatch):
    project, location, actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    other = __import__("app.models", fromlist=["Character"]).Character(project_id=project.id, name="other", profile={}, goals={})
    thread = StoryThread(project_id=project.id, title="thread", type="MAIN", progress=0.1, state={"phase": "start"})
    actor.current_state = {"location_id": "old"}; actor.inventory = ["coin"]; actor.relationships = {}; actor.physical_state = {"healthy": True}; actor.emotional_state = {"mood": "calm"}; location.active = True
    session.add_all([other, thread]); session.flush()
    facts = [
        fact_with_effect("CHARACTER", actor.id, "current_state.location_id", location.id, effect("CHARACTER", actor.id, "CHARACTER_LOCATION", "SET", "/current_state/location_id", location.id)),
        fact_with_effect("CHARACTER", actor.id, "inventory", "key", effect("CHARACTER", actor.id, "CHARACTER_INVENTORY", "ADD", "/inventory", "key")),
        fact_with_effect("CHARACTER", actor.id, "trust", 0.8, effect("CHARACTER", actor.id, "CHARACTER_RELATIONSHIP", "UPSERT", f"/relationships/{other.id}/trust", 0.8)),
        fact_with_effect("CHARACTER", actor.id, "healthy", False, effect("CHARACTER", actor.id, "CHARACTER_PHYSICAL_STATE", "SET", "/physical_state/healthy", False)),
        fact_with_effect("CHARACTER", actor.id, "mood", "afraid", effect("CHARACTER", actor.id, "CHARACTER_EMOTIONAL_STATE", "SET", "/emotional_state/mood", "afraid")),
        fact_with_effect("ENTITY", location.id, "active", False, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_ACTIVE", "SET", "/active", False)),
        fact_with_effect("SCENE", thread.id, "progress", 0.5, effect("STORY_THREAD", thread.id, "STORY_THREAD_PROGRESS", "SET", "/progress", 0.5)),
        fact_with_effect("SCENE", thread.id, "phase", "middle", effect("STORY_THREAD", thread.id, "STORY_THREAD_STATE", "SET", "/state/phase", "middle")),
        fact_with_effect("SCENE", thread.id, "status", "PAUSED", effect("STORY_THREAD", thread.id, "STORY_THREAD_STATUS", "SET", "/status", "PAUSED")),
        fact_with_effect("SCENE", project.id, "world_time", "2040-01-01T00:00:00", effect("PROJECT", project.id, "WORLD_TIME", "SET", "/current_world_time", "2040-01-01T00:00:00")),
    ]
    resolution.objective_facts = facts; session.commit()
    _batch, items, _ = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    assert len(items) == 10
    assert {item.domain.value for item in items} >= {"CHARACTER_LOCATION", "CHARACTER_INVENTORY", "CHARACTER_RELATIONSHIP", "WORLD_TIME", "STORY_THREAD_PROGRESS"}
    assert actor.inventory == ["coin"] and actor.relationships == {} and thread.progress == 0.1 and location.active is True


def test_unsupported_character_objective_fact_is_not_guessed(session, monkeypatch):
    project, _location, actor, _proposal, resolution = resolution_source(session, monkeypatch, [{"subject_type": "CHARACTER", "subject_id": "missing", "predicate": "arbitrary", "value": "x", "effect_kind": "STATE_CHANGE"}])
    before = copy.deepcopy(actor.current_state)
    batch, items, _ = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    assert items == [] and any(row["code"] == "UNSUPPORTED_STATE_EFFECT" for row in batch.derivation_report["entries"])
    assert actor.current_state == before


def test_observation_only_and_noop_are_suppressed(session, monkeypatch):
    project, location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    location.profile = {"locked": True}; session.commit()
    resolution.objective_facts = [
        {"subject_type": "ENTITY", "subject_id": location.id, "predicate": "color", "value": "red"},
        fact_with_effect("ENTITY", location.id, "locked", True, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_PROFILE", "SET", "/profile/locked", True)),
    ]; session.commit()
    batch, items, _ = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    assert items == [] and len([row for row in batch.derivation_report["entries"] if row["code"] == "NO_STATE_CHANGE"]) == 2


@pytest.mark.parametrize("resolution_status,code", [(ResolutionStatus.REJECTED, "STATE_DELTA_SOURCE_INVALID"), (ResolutionStatus.UNRESOLVED, "STATE_DELTA_SOURCE_UNRESOLVED")])
def test_invalid_resolution_source_is_rejected(session, monkeypatch, resolution_status, code):
    project, _location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [], resolution_status=resolution_status)
    with pytest.raises(ValueError, match=code):
        StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)


def test_source_and_target_project_isolation(session, monkeypatch):
    project, location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    other_project = Project(name="other"); session.add(other_project); session.flush()
    with pytest.raises(ValueError, match="STATE_DELTA_CROSS_PROJECT_REFERENCE"):
        StateDeltaCandidateBuilder().derive(session, other_project.id, resolution.id)
    resolution.objective_facts = [fact_with_effect("ENTITY", location.id, "locked", True, effect("WORLD_ENTITY", "missing", "WORLD_ENTITY_PROFILE", "SET", "/profile/locked", True))]; session.commit()
    with pytest.raises(ValueError, match="STATE_DELTA_TARGET_NOT_FOUND"):
        StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)


def test_fingerprint_determinism_and_idempotency(session, monkeypatch):
    project, location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    location.profile = {"locked": False}; resolution.objective_facts = [fact_with_effect("ENTITY", location.id, "locked", True, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_PROFILE", "SET", "/profile/locked", True))]; session.commit()
    first, first_items, first_existing = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id); session.commit()
    second, second_items, second_existing = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    assert not first_existing and second_existing and first.id == second.id
    assert first.input_fingerprint == second.input_fingerprint and first_items[0].semantic_fingerprint == second_items[0].semantic_fingerprint
    assert session.query(StateDeltaBatch).filter(StateDeltaBatch.project_id == project.id).count() == 1


def test_multiple_items_have_stable_ordinals(session, monkeypatch):
    project, location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    location.profile = {"a": False, "b": False}; session.commit()
    resolution.objective_facts = [
        fact_with_effect("ENTITY", location.id, "b", True, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_PROFILE", "SET", "/profile/b", True)),
        fact_with_effect("ENTITY", location.id, "a", True, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_PROFILE", "SET", "/profile/a", True)),
    ]; session.commit()
    _batch, items, _ = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    assert [item.ordinal for item in items] == [1, 2]
    assert [item.path for item in items] == ["/profile/a", "/profile/b"]


def test_state_delta_api_read_list_and_project_isolation(session, monkeypatch):
    project, location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [])
    location.profile = {"locked": False}; resolution.objective_facts = [fact_with_effect("ENTITY", location.id, "locked", True, effect("WORLD_ENTITY", location.id, "WORLD_ENTITY_PROFILE", "SET", "/profile/locked", True))]; session.commit()
    client = client_for(session, monkeypatch)
    monkeypatch.setattr(api, "get_model_provider", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider must not run")))
    created = client.post(f"/projects/{project.id}/state-delta-batches/derive", json={"source_resolution_id": resolution.id})
    assert created.status_code == 201, created.text
    body = created.json(); assert body["idempotent"] is False and len(body["items"]) == 1
    repeated = client.post(f"/projects/{project.id}/state-delta-batches/derive", json={"source_resolution_id": resolution.id})
    assert repeated.status_code == 201 and repeated.json()["idempotent"] is True and repeated.json()["id"] == body["id"]
    assert client.get(f"/projects/{project.id}/state-delta-batches/{body['id']}").json()["items"][0]["ordinal"] == 1
    assert len(client.get(f"/projects/{project.id}/state-delta-batches?source_resolution_id={resolution.id}&status=CANDIDATE").json()) == 1
    other = Project(name="other"); session.add(other); session.commit()
    assert client.get(f"/projects/{other.id}/state-delta-batches/{body['id']}").status_code == 404
    assert client.post(f"/projects/{project.id}/state-delta-batches/{body['id']}/apply", json={}).status_code == 404


def test_state_delta_api_source_errors(session, monkeypatch):
    project, _location, _actor, _proposal, resolution = resolution_source(session, monkeypatch, [], resolution_status=ResolutionStatus.REJECTED)
    client = client_for(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/state-delta-batches/derive", json={"source_resolution_id": resolution.id}).json()["detail"]["code"] == "STATE_DELTA_SOURCE_INVALID"
    assert client.post(f"/projects/{project.id}/state-delta-batches/derive", json={"source_resolution_id": "missing"}).json()["detail"]["code"] == "STATE_DELTA_SOURCE_NOT_FOUND"
