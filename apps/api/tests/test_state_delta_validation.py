import copy
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.main import app
import app.api as api
from app.models import (
    CanonFact, CanonType, Character, CharacterDecision, CharacterDecisionStatus, CharacterDecisionType,
    PerformanceMode, PerformanceStatus, Project, ResolutionStatus, ScenePerformance, ScenePerformanceTurn,
    StateDeltaBatch, StateDeltaBatchStatus, StateDeltaDomain, StateDeltaItem, StateDeltaOperation,
    StateDeltaTargetType, StoryThread, ThreadStatus, WorldEntity, WorldResolution,
)
from app.state_delta import StateDeltaCandidateBuilder
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


def make_effect(target_id, path="/profile/opened", value=True, *, domain="WORLD_ENTITY_PROFILE", target_type="WORLD_ENTITY", operation="SET"):
    return {"effect_kind": "STATE_CHANGE", "target_type": target_type, "target_id": target_id, "domain": domain, "operation": operation, "path": path, "value": value, "reason": "explicit validation fixture", "evidence": {"source": "test", "target_entity_id": target_id}}


def source_fixture(session, monkeypatch, *, effects=None, facts=None):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    location.profile = {"opened": False, "locked": False}
    actor.current_state = {"location_id": location.id}
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint="validation", decision_type=CharacterDecisionType.ACT, intent="interact", chosen_action="open", motivation="test", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], decision_summary="test", status=CharacterDecisionStatus.VALID)
    performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=700, proposal_context_fingerprint="validation", mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.COMPLETED, participant_order=[actor.id, other.id], active_participant_ids=[actor.id, other.id], max_turns=1, turn_count=1)
    session.add_all([decision, performance]); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=performance.id, sequence=1, actor_character_id=actor.id, actor_context_fingerprint="validation", character_decision_id=decision.id, action_visibility="PUBLIC", observable_action="open", spoken_content=None, recipient_character_ids=[other.id], requires_world_resolution=True, world_resolution_request={"kind": "INTERACT", "target_entity_id": location.id}, validation_result={})
    session.add(turn); session.flush()
    resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode="HEURISTIC", world_context_fingerprint="validation-context", status=ResolutionStatus.VALID, outcome="SUCCESS", outcome_summary="structured", objective_facts=facts if facts is not None else [{"subject_type": "ENTITY", "subject_id": location.id, "predicate": "opened", "value": True}], state_effects=effects if effects is not None else [make_effect(location.id)], actor_observation="The entity opens.", public_observation="The entity opens.", recipient_character_ids=[other.id], canon_fact_ids_used=[], world_entity_ids_used=[location.id], resolution_basis_summary="structured", missing_information=[])
    session.add(resolution); session.commit()
    batch, _items, _ = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)
    session.commit()
    return project, location, actor, other, proposal, performance, turn, resolution, batch, client


def issue_codes(body):
    return {item["code"] for item in body["validation_report"]["issues"]}


def validate(client, project, batch):
    return client.post(f"/projects/{project.id}/state-delta-batches/{batch.id}/validate")


def test_happy_path_validates_candidate_and_preserves_formal_world(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    before = copy.deepcopy(location.profile)
    response = validate(client, project, batch)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "VALIDATED"
    assert response.json()["validation_report"]["valid"] is True
    session.refresh(location)
    assert location.profile == before


def test_repeated_validation_is_idempotent(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    first = validate(client, project, batch).json()
    second = validate(client, project, batch)
    assert second.status_code == 200 and second.json()["idempotent"] is True
    assert second.json()["validation_fingerprint"] == first["validation_fingerprint"]


def test_source_changed_rejects_candidate(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch)
    resolution.state_effects = [make_effect(resolution.world_entity_ids_used[0], value=False)]
    session.commit()
    response = validate(client, project, batch)
    assert response.status_code == 200 and response.json()["status"] == "REJECTED"
    assert "STATE_DELTA_SOURCE_CHANGED" in issue_codes(response.json())


def test_base_world_stale_rejects_even_unrelated_change(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    extra = WorldEntity(project_id=project.id, entity_type="CUSTOM", name="unrelated", profile={})
    session.add(extra); session.commit()
    response = validate(client, project, batch)
    assert response.json()["status"] == "REJECTED" and "STATE_DELTA_BASE_STATE_STALE" in issue_codes(response.json())


def test_before_value_stale_and_item_tamper_are_detected(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    location.profile = {**location.profile, "opened": True}; session.add(location); session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_BEFORE_VALUE_STALE" in issue_codes(response.json())

    project2, location2, _actor2, _other2, _proposal2, _performance2, _turn2, _resolution2, batch2, client2 = source_fixture(session, monkeypatch)
    item = session.scalar(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch2.id))
    item.after_value = False; session.commit()
    response2 = validate(client2, project2, batch2)
    assert "STATE_DELTA_ITEM_FINGERPRINT_MISMATCH" in issue_codes(response2.json())


def test_zero_item_batch_validates_as_no_state_change(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    response = validate(client, project, batch)
    body = response.json()
    assert body["status"] == "VALIDATED" and body["validation_report"]["item_count"] == 0 and body["validation_report"]["no_state_change"] is True


def test_missing_batch_and_cross_project_are_404(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/state-delta-batches/missing/validate").status_code == 404
    other = Project(name="other"); session.add(other); session.commit()
    assert client.post(f"/projects/{other.id}/state-delta-batches/{batch.id}/validate").status_code == 404


def test_duplicate_and_ancestor_paths_reject(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    first = session.scalar(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id))
    duplicate = StateDeltaItem(project_id=project.id, batch_id=batch.id, ordinal=2, target_type=first.target_type, target_id=first.target_id, domain=first.domain, operation=first.operation, path=first.path, before_value=first.before_value, after_value=first.after_value, causal_reason=first.causal_reason, source_turn_id=first.source_turn_id, source_resolution_id=first.source_resolution_id, evidence=first.evidence, semantic_fingerprint=first.semantic_fingerprint)
    session.add(duplicate); session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_DUPLICATE_PATH" in issue_codes(response.json())


def test_target_missing_and_inactive_reject(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch)
    item = session.scalar(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id)); item.target_id = "missing"; item.evidence = {**item.evidence, "state_effect": {**item.evidence["state_effect"], "target_id": "missing"}}; session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_TARGET_NOT_FOUND" in issue_codes(response.json())

    project2, location2, _actor2, _other2, _proposal2, _performance2, _turn2, _resolution2, batch2, client2 = source_fixture(session, monkeypatch)
    location2.active = False; session.commit()
    response2 = validate(client2, project2, batch2)
    assert "STATE_DELTA_BASE_STATE_STALE" in issue_codes(response2.json()) or "STATE_DELTA_TARGET_INACTIVE" in issue_codes(response2.json())


def test_character_location_requires_active_location(session, monkeypatch):
    project, location, actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    resolution.state_effects = [make_effect(actor.id, path="/current_state/location_id", value=location.id, domain="CHARACTER_LOCATION", target_type="CHARACTER")]; session.commit()
    new_batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    location.active = False; session.commit()
    response = validate(client, project, new_batch)
    assert response.json()["status"] == "REJECTED"


def test_inventory_add_requires_active_item_entity(session, monkeypatch):
    project, _location, actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    resolution.state_effects = [make_effect(actor.id, path="/inventory", value="not-an-item", domain="CHARACTER_INVENTORY", target_type="CHARACTER", operation="ADD")]; session.commit()
    new_batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, new_batch)
    assert "STATE_DELTA_INVENTORY_ITEM_INVALID" in issue_codes(response.json())


def test_relationship_self_and_trust_range_reject(session, monkeypatch):
    project, _location, actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    resolution.state_effects = [make_effect(actor.id, path=f"/relationships/{actor.id}/trust", value=2, domain="CHARACTER_RELATIONSHIP", target_type="CHARACTER", operation="UPSERT")]; session.commit()
    new_batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, new_batch)
    codes = issue_codes(response.json())
    assert "STATE_DELTA_RELATIONSHIP_SELF_REFERENCE" in codes and "STATE_DELTA_RELATIONSHIP_VALUE_INVALID" in codes


def test_thread_progress_and_terminal_status_constraints(session, monkeypatch):
    project, location, actor, other, proposal, performance, turn, resolution, batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    thread = StoryThread(project_id=project.id, title="t", type="MAIN", progress=0.8, state={}, status=ThreadStatus.RESOLVED)
    session.add(thread); session.flush()
    effect = make_effect(thread.id, path="/progress", value=0.2, domain="STORY_THREAD_PROGRESS", target_type="STORY_THREAD")
    resolution.state_effects = [effect]; resolution.objective_facts = []; session.commit()
    new_batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, new_batch)
    assert "STATE_DELTA_THREAD_PROGRESS_REGRESSION" in issue_codes(response.json())


def test_world_time_regression_rejects(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    project.current_world_time = datetime(2040, 1, 2); session.commit()
    resolution.state_effects = [make_effect(project.id, path="/current_world_time", value="2039-01-01T00:00:00", domain="WORLD_TIME", target_type="PROJECT")]; session.commit()
    new_batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, new_batch)
    assert "STATE_DELTA_WORLD_TIME_REGRESSION" in issue_codes(response.json())


def test_invalid_world_time_format_rejects(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, resolution, _batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    resolution.state_effects = [make_effect(project.id, path="/current_world_time", value="not-a-time", domain="WORLD_TIME", target_type="PROJECT")]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_WORLD_TIME_INVALID" in issue_codes(response.json())


def test_locked_structured_canon_conflict_is_safe_for_secret(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    canon = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="SECRET TEXT MUST NOT APPEAR", data={"target_type": "WORLD_ENTITY", "target_id": location.id, "path": "/profile/opened", "value": False}, locked=True)
    session.add(canon); session.commit()
    response = validate(client, project, batch)
    body = response.json()
    assert "STATE_DELTA_CANON_CONFLICT" in issue_codes(body)
    assert "SECRET TEXT MUST NOT APPEAR" not in str(body)


def test_formal_payloads_are_unchanged_by_validation(session, monkeypatch):
    project, location, actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch)
    snapshot = {"entity": copy.deepcopy(location.profile), "character": copy.deepcopy(actor.current_state), "resolution": copy.deepcopy(resolution.state_effects)}
    validate(client, project, batch)
    session.refresh(location); session.refresh(actor); session.refresh(resolution)
    assert snapshot == {"entity": location.profile, "character": actor.current_state, "resolution": resolution.state_effects}


def test_rejected_batch_rederive_returns_same_artifact(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch)
    item = session.scalar(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id)); item.after_value = False; session.commit()
    first = validate(client, project, batch).json()
    assert first["status"] == "REJECTED"
    repeated = client.post(f"/projects/{project.id}/state-delta-batches/derive", json={"source_resolution_id": resolution.id})
    assert repeated.status_code == 201 and repeated.json()["id"] == batch.id and repeated.json()["status"] == "REJECTED" and repeated.json()["idempotent"] is True


def test_validated_batch_rederive_returns_same_artifact(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, resolution, batch, client = source_fixture(session, monkeypatch)
    assert validate(client, project, batch).json()["status"] == "VALIDATED"
    repeated = client.post(f"/projects/{project.id}/state-delta-batches/derive", json={"source_resolution_id": resolution.id})
    assert repeated.status_code == 201 and repeated.json()["id"] == batch.id and repeated.json()["status"] == "VALIDATED" and repeated.json()["idempotent"] is True


def test_apply_endpoint_remains_absent(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/state-delta-batches/{batch.id}/apply", json={}).status_code == 404


def test_active_location_deactivation_is_rejected_when_in_use(session, monkeypatch):
    project, location, _actor, _other, _proposal, _performance, _turn, resolution, _batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    resolution.state_effects = [make_effect(location.id, path="/active", value=False, domain="WORLD_ENTITY_ACTIVE", target_type="WORLD_ENTITY")]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_ENTITY_IN_USE" in issue_codes(response.json())


def test_valid_structured_item_inventory_passes(session, monkeypatch):
    project, _location, actor, _other, _proposal, _performance, _turn, resolution, _batch, client = source_fixture(session, monkeypatch, effects=[], facts=[])
    item = WorldEntity(project_id=project.id, entity_type="ITEM", name="key", profile={})
    session.add(item); session.flush()
    resolution.state_effects = [make_effect(actor.id, path="/inventory", value=item.id, domain="CHARACTER_INVENTORY", target_type="CHARACTER", operation="ADD")]
    session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]; session.commit()
    response = validate(client, project, batch)
    assert response.json()["status"] == "VALIDATED"


def test_ancestor_path_conflict_is_rejected(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    first = session.scalar(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id))
    ancestor = StateDeltaItem(project_id=first.project_id, batch_id=first.batch_id, ordinal=2, target_type=first.target_type, target_id=first.target_id, domain=first.domain, operation=first.operation, path="/profile", before_value=first.before_value, after_value=first.after_value, causal_reason=first.causal_reason, source_turn_id=first.source_turn_id, source_resolution_id=first.source_resolution_id, evidence=copy.deepcopy(first.evidence), semantic_fingerprint=first.semantic_fingerprint)
    session.add(ancestor); session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_PATH_CONFLICT" in issue_codes(response.json())


def test_applied_batch_has_stable_lifecycle_error(session, monkeypatch):
    project, _location, _actor, _other, _proposal, _performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    batch.status = StateDeltaBatchStatus.APPLIED; session.commit()
    response = validate(client, project, batch)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "STATE_DELTA_ALREADY_APPLIED"


def test_source_lineage_tamper_rejects_validation(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, batch, client = source_fixture(session, monkeypatch)
    performance.scene_proposal_id = "missing-proposal"; session.commit()
    response = validate(client, project, batch)
    assert "STATE_DELTA_SOURCE_LINEAGE_INVALID" in issue_codes(response.json())
