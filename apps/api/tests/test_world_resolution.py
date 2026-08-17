import json
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.models import CharacterDecision, CharacterDecisionStatus, CharacterDecisionType, PerformanceStatus, ScenePerformance, ScenePerformanceTurn, WorldResolution, ResolutionStatus, ResolverMode, ActionVisibility, CanonFact, CanonType, RevealConstraint, RevealStatus
from app.world_resolution import PerformanceWorldStateBuilder, WorldObservationRouter, WorldResolutionContextBuilder, WorldResolutionConstraintChecker, HeuristicWorldResolver, WorldContextSanitizer, WorldResolutionPayload
from test_scene_performance import approved_setup


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def _pending_world_turn(session, project, actor, other, proposal, client):
    take_json = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"}).json()
    take = session.get(ScenePerformance, take_json["id"])
    context = {"fingerprint": "character-context-v1:test"}
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint=context["fingerprint"], decision_type=CharacterDecisionType.ACT, intent="inspect", chosen_action="inspect the location", motivation="need evidence", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], decision_summary="inspect")
    session.add(decision); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=take.id, sequence=1, actor_character_id=actor.id, actor_context_fingerprint=context["fingerprint"], character_decision_id=decision.id, action_visibility=ActionVisibility.PUBLIC, observable_action="inspect the location", spoken_content=None, recipient_character_ids=[other.id], requires_world_resolution=True, world_resolution_request={"kind": "INSPECT", "description": "inspect", "target_entity_id": proposal.location_id, "target_character_id": None}, validation_result={})
    session.add(turn); take.turn_count = 1; take.status = PerformanceStatus.AWAITING_WORLD; session.add(take); session.commit()
    return take, turn


def test_take_world_state_isolated_and_latest_fact_wins(session):
    from app.models import Project, ScenePerformance
    project = Project(name="p"); session.add(project); session.flush()
    first = ScenePerformance(project_id=project.id, scene_proposal_id="s1", take_number=1, proposal_context_fingerprint="x", mode=ResolverMode.HEURISTIC, participant_order=[], active_participant_ids=[])
    second = ScenePerformance(project_id=project.id, scene_proposal_id="s2", take_number=2, proposal_context_fingerprint="x", mode=ResolverMode.HEURISTIC, participant_order=[], active_participant_ids=[])
    session.add_all([first, second]); session.flush()
    for index, (performance, value) in enumerate(((first, False), (first, True), (second, False))):
        resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=f"turn-{performance.id}-{value}", resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="w", status=ResolutionStatus.VALID, outcome="SUCCESS", outcome_summary="ok", objective_facts=[{"subject_type":"ENTITY","subject_id":"door","predicate":"open","value":value}], recipient_character_ids=[], canon_fact_ids_used=[], world_entity_ids_used=[], missing_information=[])
        resolution.created_at = datetime(2020, 1, 1) + timedelta(seconds=index)
        session.add(resolution)
    session.commit()
    assert PerformanceWorldStateBuilder().build(session, first.id)["facts"][0]["value"] is True
    assert PerformanceWorldStateBuilder().build(session, second.id)["facts"][0]["value"] is False


def test_world_context_excludes_unrelated_secret_canon(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    related = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="related secret", data={"entity_id": location.id})
    unrelated = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="unrelated secret", data={"entity_id": "other-entity"})
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    session.add_all([related, unrelated]); session.commit()
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    assert [item["id"] for item in context["canon"]] == [related.id]
    assert "director_reasoning_summary" not in json.dumps(context)


def test_heuristic_inspect_is_take_local_and_observation_routed(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    location.profile = {"inspectable": "a shallow scratch"}; session.add(location); session.commit()
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    raw, _ = HeuristicWorldResolver().resolve(context)
    assert raw["outcome"] == "SUCCESS" and raw["objective_facts"]
    resolution = WorldResolution(project_id=project.id, performance_id=take.id, performance_turn_id=turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint=context["fingerprint"], status=ResolutionStatus.VALID, **raw)
    resolution.recipient_character_ids = WorldObservationRouter().recipients(take, turn, resolution)
    assert resolution.recipient_character_ids == sorted([actor.id, other.id])
    assert "objective_facts" not in json.dumps({"actor_observation": resolution.actor_observation})


def test_resolve_endpoint_valid_restores_running_without_turn_increment(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    location.profile = {"inspectable": "a shallow scratch"}; session.add(location); session.commit()
    from app.director import DirectorContextBuilder
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]; session.add(proposal); session.commit()
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    response = client.post(f"/projects/{project.id}/performances/{take.id}/world/resolve", json={"mode": "HEURISTIC"})
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["resolution"]["status"] == "VALID"
    assert body["performance"]["status"] == "RUNNING"
    assert body["performance"]["turn_count"] == 1
    assert session.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id)) is not None


def test_unresolved_world_keeps_awaiting_world(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    take, _ = _pending_world_turn(session, project, actor, other, proposal, client)
    location.profile = {}; session.add(location); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{take.id}/world/resolve", json={"mode": "HEURISTIC"})
    assert response.status_code == 201
    assert response.json()["resolution"]["status"] == "UNRESOLVED"
    assert response.json()["performance"]["status"] == "AWAITING_WORLD"


def test_target_character_context_is_objective_only(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    turn.world_resolution_request["target_character_id"] = other.id; session.add(turn); session.commit()
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    assert context["target_character"]["id"] == other.id
    rendered = json.dumps(context["target_character"])
    assert "knowledge" not in rendered and "memory" not in rendered and "goals" not in rendered


def test_fact_scope_requires_exact_subject_type(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    payload = {"outcome":"SUCCESS", "outcome_summary":"ok", "objective_facts":[{"subject_type":"SCENE", "subject_id":"wrong", "predicate":"x", "value":True}], "actor_observation":None, "public_observation":None, "canon_fact_ids_used":[], "world_entity_ids_used":[], "resolution_basis_summary":None, "missing_information":[]}
    report = WorldResolutionConstraintChecker().validate(session, context, __import__("app.world_resolution", fromlist=["WorldResolutionPayload"]).WorldResolutionPayload.model_validate(payload), project.id)
    assert any(issue["code"] == "INVALID_FACT_SUBJECT" for issue in report["issues"])


def test_resolution_retry_reuses_row_and_valid_cannot_retry(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    location.profile = {}; session.add(location); session.commit()
    from app.director import DirectorContextBuilder
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]; session.add(proposal); session.commit()
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    first = client.post(f"/projects/{project.id}/performances/{take.id}/world/resolve", json={"mode":"HEURISTIC"})
    assert first.status_code == 201 and first.json()["resolution"]["status"] == "UNRESOLVED"
    resolution_id = first.json()["resolution"]["id"]
    location.profile = {"inspectable":"mark"}; session.add(location); session.commit()
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]; take.proposal_context_fingerprint = proposal.context_fingerprint; session.add_all([proposal, take]); session.commit()
    second = client.post(f"/projects/{project.id}/performances/{take.id}/world/resolve", json={"mode":"HEURISTIC"})
    assert second.status_code == 201 and second.json()["resolution"]["id"] == resolution_id
    assert second.json()["resolution"]["status"] == "VALID"
    assert client.post(f"/projects/{project.id}/performances/{take.id}/world/resolve", json={"mode":"HEURISTIC"}).status_code == 409


def test_locked_structured_canon_contradiction_blocks(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    canon = CanonFact(project_id=project.id, fact_type=CanonType.CORE_CANON, proposition="door is locked", data={"subject_type":"ENTITY", "subject_id":location.id, "predicate":"locked", "value":True}, locked=True)
    session.add(canon); session.commit()
    from app.director import DirectorContextBuilder
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]; session.add(proposal); session.commit()
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    payload = WorldResolutionPayload(outcome="FAILURE", outcome_summary="no", objective_facts=[{"subject_type":"ENTITY","subject_id":location.id,"predicate":"locked","value":False}], actor_observation=None, public_observation=None, canon_fact_ids_used=[], world_entity_ids_used=[location.id], resolution_basis_summary=None, missing_information=[])
    report = WorldResolutionConstraintChecker().validate(session, context, payload, project.id)
    assert any(item["code"] == "CANON_CONTRADICTION" for item in report["issues"])


def test_locked_secret_proposition_leak_is_blocked_but_basis_is_private(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    secret_text = "A corpse is behind the stone door."
    canon = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition=secret_text, data={"entity_id":location.id}, locked=True)
    session.add(canon); session.flush(); session.add(RevealConstraint(project_id=project.id, canon_fact_id=canon.id, status=RevealStatus.LOCKED, allowed_character_ids=[])); session.commit()
    from app.director import DirectorContextBuilder
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]; session.add(proposal); session.commit()
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    payload = WorldResolutionPayload(outcome="FAILURE", outcome_summary="blocked", objective_facts=[], actor_observation=secret_text, public_observation=None, canon_fact_ids_used=[], world_entity_ids_used=[], resolution_basis_summary=secret_text, missing_information=[])
    report = WorldResolutionConstraintChecker().validate(session, context, payload, project.id)
    assert any(item["code"] == "OBSERVATION_LEAK" for item in report["issues"])
    safe = WorldResolutionPayload(**{**payload.model_dump(), "actor_observation":"The door is blocked.", "resolution_basis_summary":secret_text})
    assert not any(item["code"] == "OBSERVATION_LEAK" for item in WorldResolutionConstraintChecker().validate(session, context, safe, project.id)["issues"])


def test_world_context_sanitizer_recursively_removes_narrative_metadata():
    value = WorldContextSanitizer().sanitize({"actor":{"current_state":{"author_only":"x","ok":True}}, "location":{"profile":{"nested":{"director_only":"x","locked":True}}}, "canon":[{"data":{"narrative_only":"x","secret":"kept"}}], "scope":{}})
    rendered = json.dumps(value)
    assert "director_only" not in rendered and "author_only" not in rendered and "narrative_only" not in rendered and "locked" in rendered and "secret" in rendered


def test_research_settings_do_not_enter_world_context_or_fingerprint(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    first = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    project.research_settings = {"provider":"private","search_policy":"secret"}; session.add(project); session.commit()
    second = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    assert "provider" not in json.dumps(first) and "provider" not in json.dumps(second)
    assert first["fingerprint"] == second["fingerprint"]


def test_forbidden_reveal_id_is_normalized_to_proposition_without_lock(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    fact = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="The king is already dead.", data={"entity_id":location.id}, locked=False)
    session.add(fact); session.commit(); proposal.forbidden_reveals = [fact.id]; session.add(proposal); session.commit()
    from app.director import DirectorContextBuilder
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]; session.add(proposal); session.commit()
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    payload = WorldResolutionPayload(outcome="FAILURE", outcome_summary="x", objective_facts=[], actor_observation="The king is already dead.", public_observation=None, canon_fact_ids_used=[], world_entity_ids_used=[], resolution_basis_summary=None, missing_information=[])
    report = WorldResolutionConstraintChecker().validate(session, context, payload, project.id)
    assert any(item["code"] == "OBSERVATION_LEAK" for item in report["issues"])


def test_global_secret_world_rule_enters_context_but_narrative_metadata_does_not(session, monkeypatch):
    project, location, actor, other, proposal, client = approved_setup(session, monkeypatch)
    fact = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="Dead people cannot naturally return.", data={"global_world_rule":True, "director_only":{"plot":"hidden"}}, locked=True)
    session.add(fact); session.commit(); proposal.context_fingerprint = __import__("app.director", fromlist=["DirectorContextBuilder"]).DirectorContextBuilder().build(session, project.id)["fingerprint"]; session.add(proposal); session.commit()
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    context = WorldResolutionContextBuilder().build(session, take, turn, proposal, turn.world_resolution_request)
    rendered = json.dumps(context)
    assert fact.id in rendered and "Dead people cannot naturally return." in rendered and "plot" not in json.dumps(WorldContextSanitizer().sanitize(context))
