import json
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.models import CharacterDecision, CharacterDecisionStatus, CharacterDecisionType, PerformanceStatus, ScenePerformance, ScenePerformanceTurn, WorldResolution, ResolutionStatus, ResolverMode, ActionVisibility, CanonFact, CanonType
from app.world_resolution import PerformanceWorldStateBuilder, WorldObservationRouter, WorldResolutionContextBuilder, WorldResolutionConstraintChecker, HeuristicWorldResolver
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
    take, turn = _pending_world_turn(session, project, actor, other, proposal, client)
    location.profile = {"inspectable": "a shallow scratch"}; session.add(location); session.commit()
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
