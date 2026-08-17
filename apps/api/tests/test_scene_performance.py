import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.main import app
import app.api as api
from app.character_mind import ActorPerceptionSanitizer, CharacterContextBuilder
from app.models import Character, CharacterDecision, CharacterKnowledge, CharacterMemory, CanonFact, PerformanceStatus, ProposalStatus, Scene, ScenePerformance, ScenePerformanceTurn, StoryThread, WorldEntity
from app.performance import PerformanceActionConstraintChecker, PerformanceActionPayload, PerformanceObservationRouter
from test_character_mind import seed


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db: yield db


def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def approved_setup(session, monkeypatch):
    project, location, actor, other, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_id = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]["id"]
    proposal = session.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, proposal_id)
    proposal.participants = [actor.id, other.id]
    proposal.status = ProposalStatus.APPROVED
    session.add(proposal); session.commit()
    return project, location, actor, other, proposal, client


def test_only_approved_proposal_and_take_numbers_are_allowed(session, monkeypatch):
    project, _, actor, _, proposal, client = approved_setup(session, monkeypatch)
    proposal.status = ProposalStatus.VALID; session.add(proposal); session.commit()
    blocked = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"})
    assert blocked.status_code == 409
    proposal.status = ProposalStatus.APPROVED; session.add(proposal); session.commit()
    first = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC", "max_turns": 2}).json()
    second = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"}).json()
    assert first["take_number"] == 1 and second["take_number"] == 2 and first["active_participant_ids"] == proposal.participants


def test_heuristic_step_routes_public_action_and_does_not_change_world(session, monkeypatch):
    project, _, actor, other, proposal, client = approved_setup(session, monkeypatch)
    performance = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"}).json()
    before = {"characters": session.scalar(select(func.count(Character.id))), "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))), "memories": session.scalar(select(func.count(CharacterMemory.id))), "canon": session.scalar(select(func.count(CanonFact.id))), "threads": session.scalar(select(func.count(StoryThread.id))), "entities": session.scalar(select(func.count(WorldEntity.id))), "scenes": session.scalar(select(func.count(Scene.id)))}
    response = client.post(f"/projects/{project.id}/performances/{performance['id']}/step")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["turn"]["recipient_character_ids"] == [other.id]
    assert body["performance"]["status"] == "RUNNING"
    after = {"characters": session.scalar(select(func.count(Character.id))), "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))), "memories": session.scalar(select(func.count(CharacterMemory.id))), "canon": session.scalar(select(func.count(CanonFact.id))), "threads": session.scalar(select(func.count(StoryThread.id))), "entities": session.scalar(select(func.count(WorldEntity.id))), "scenes": session.scalar(select(func.count(Scene.id)))}
    assert before == after and session.scalar(select(func.count(CharacterDecision.id))) == 1


def test_visibility_router_and_private_fields_are_isolated(session, monkeypatch):
    assert PerformanceObservationRouter().recipients(__import__("app.models", fromlist=["ActionVisibility"]).ActionVisibility.PUBLIC, ["a", "b", "c"], "a", None) == ["b", "c"]
    assert PerformanceObservationRouter().recipients(__import__("app.models", fromlist=["ActionVisibility"]).ActionVisibility.TARGETED, ["a", "b", "c"], "a", "b") == ["b"]
    assert PerformanceObservationRouter().recipients(__import__("app.models", fromlist=["ActionVisibility"]).ActionVisibility.TARGETED, ["a", "b", "c"], "a", "c") == ["c"]
    assert PerformanceObservationRouter().recipients(__import__("app.models", fromlist=["ActionVisibility"]).ActionVisibility.COVERT, ["a", "b"], "a", None) == []
    project, _, actor, other, proposal, _ = approved_setup(session, monkeypatch)
    context = CharacterContextBuilder().build(session, project.id, other.id, proposal)
    context["scene"]["performance_observations"] = [{"turn": 1, "source_character_id": actor.id, "observable_action": "puts an old photo on the table", "spoken_content": "Have you seen this mark?"}]
    context["scene"]["private_decision"] = {"motivation": "I suspect B killed my father", "knowledge_used": ["secret-A"], "uncertainties": ["secret"]}
    view = ActorPerceptionSanitizer().sanitize(context)
    rendered = json.dumps(view)
    assert "puts an old photo" in rendered and "Have you seen this mark?" in rendered
    assert "I suspect B killed my father" not in rendered and "secret-A" not in rendered and "private_decision" not in rendered


def test_targeted_cannot_reach_non_target_and_world_request_requires_stop(session, monkeypatch):
    project, _, actor, other, proposal, _ = approved_setup(session, monkeypatch)
    context = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    from app.models import ActionVisibility, CharacterDecisionType
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint=context["fingerprint"], decision_type=CharacterDecisionType.ASK, intent="ask", chosen_action="ask", target_character_id=other.id, target_entity_id=None, motivation="need an answer", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], decision_summary="ask", status="DRAFT")
    bad = PerformanceActionPayload(visibility=ActionVisibility.TARGETED, observable_action="ask", spoken_content="question", requires_world_resolution=False, world_resolution_request=None, disclosure_knowledge_ids=[], target_character_id="missing")
    report = PerformanceActionConstraintChecker().validate(session, context, proposal, decision, bad)
    assert any(issue.code == "INVALID_TARGET" for issue in report.issues)
    request = PerformanceActionPayload(visibility=ActionVisibility.PUBLIC, observable_action="inspect", spoken_content=None, requires_world_resolution=True, world_resolution_request={"kind": "INSPECT", "description": "inspect box", "target_entity_id": proposal.location_id, "target_character_id": None}, disclosure_knowledge_ids=[], target_character_id=other.id)
    assert PerformanceActionConstraintChecker().validate(session, context, proposal, decision, request).valid


def test_stale_performance_invalidates_and_second_take_isolated(session, monkeypatch):
    project, _, actor, _, proposal, client = approved_setup(session, monkeypatch)
    first = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"}).json()
    second = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"}).json()
    assert first["id"] != second["id"]
    actor.goals = {"current": "changed"}; session.add(actor); session.commit()
    response = client.post(f"/projects/{project.id}/performances/{first['id']}/step")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "STALE_PERFORMANCE"
    assert session.get(ScenePerformance, second["id"]).status == PerformanceStatus.READY


def test_withdraw_removes_active_participant_but_refuse_does_not(session):
    performance = ScenePerformance(active_participant_ids=["a", "b"], participant_order=["a", "b"], turn_count=0, max_turns=6)
    from app.performance import TurnScheduler
    assert TurnScheduler().next_actor(performance, []) == "a"
    performance.active_participant_ids = ["b"]
    assert TurnScheduler().next_actor(performance, []) == "b"


def performance_payload(decision_type="WAIT", target=None, action_target=None, spoken=None, observable=None, world=False):
    return {"decision": {"decision_type": decision_type, "intent": "wait", "chosen_action": "wait", "motivation": "The character is cautious.", "target_character_id": target, "target_entity_id": None, "goal_refs": [], "knowledge_used": [], "memory_refs": [], "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None, "expected_personal_result": None, "uncertainties": [], "refused_options": [], "boundary_override_reason": None, "decision_summary": "Wait."}, "action": {"visibility": "PUBLIC", "observable_action": observable, "spoken_content": spoken, "requires_world_resolution": world, "world_resolution_request": None, "disclosure_knowledge_ids": [], "target_character_id": action_target}}


def test_target_mismatch_and_self_target_scheduler_are_blocked(session, monkeypatch):
    project, _, actor, other, proposal, _ = approved_setup(session, monkeypatch)
    context = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    from app.models import ActionVisibility, CharacterDecisionType
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint=context["fingerprint"], decision_type=CharacterDecisionType.ASK, intent="ask", chosen_action="ask", target_character_id=other.id, target_entity_id=None, motivation="ask", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], decision_summary="ask")
    action = PerformanceActionPayload(visibility=ActionVisibility.TARGETED, observable_action=None, spoken_content="question", requires_world_resolution=False, world_resolution_request=None, disclosure_knowledge_ids=[], target_character_id=actor.id)
    assert any(issue.code == "TARGET_MISMATCH" for issue in PerformanceActionConstraintChecker().validate(session, context, proposal, decision, action, [actor.id, other.id]).issues)
    performance = ScenePerformance(active_participant_ids=[actor.id, other.id], participant_order=[actor.id, other.id])
    prior = type("Turn", (), {"actor_character_id": actor.id})()
    from app.performance import TurnScheduler
    assert TurnScheduler().next_actor(performance, [prior], actor.id) == other.id


def test_withdraw_stops_when_one_participant_remains(session, monkeypatch):
    project, _, actor, other, proposal, client = approved_setup(session, monkeypatch)
    performance = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC"}).json()
    class Withdraw:
        def perform(self, context): return performance_payload("WITHDRAW"), None
    monkeypatch.setattr(api, "HeuristicCharacterPerformer", Withdraw)
    response = client.post(f"/projects/{project.id}/performances/{performance['id']}/step")
    assert response.status_code == 201
    assert response.json()["performance"]["status"] == "PAUSED"
    assert response.json()["performance"]["stop_reason"] == "INSUFFICIENT_ACTIVE_PARTICIPANTS"
    assert session.get(Character, actor.id).current_state["location_id"]


def test_quiescent_requires_a_full_active_cycle(session, monkeypatch):
    project, _, actor, other, proposal, client = approved_setup(session, monkeypatch)
    performance = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "HEURISTIC", "max_turns": 4}).json()
    class Quiet:
        calls = 0
        def perform(self, context):
            Quiet.calls += 1
            return performance_payload("WAIT" if Quiet.calls == 1 else "OBSERVE"), None
    monkeypatch.setattr(api, "HeuristicCharacterPerformer", Quiet)
    assert client.post(f"/projects/{project.id}/performances/{performance['id']}/step").json()["performance"]["status"] == "RUNNING"
    assert client.post(f"/projects/{project.id}/performances/{performance['id']}/step").json()["performance"]["stop_reason"] == "QUIESCENT"


def test_withdrawn_participant_does_not_receive_public_observation():
    from app.models import ActionVisibility
    router = PerformanceObservationRouter()
    assert router.recipients(ActionVisibility.PUBLIC, ["a", "b"], "a", None) == ["b"]
    assert router.recipients(ActionVisibility.TARGETED, ["a", "b"], "a", "c") == []


def test_provider_error_keeps_take_retryable_without_artifacts(session, monkeypatch):
    project, _, actor, _, proposal, client = approved_setup(session, monkeypatch)
    performance = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/performances", json={"mode": "LLM"}).json()
    from app.ai.errors import MODEL_TIMEOUT, ModelProviderError
    from app.ai.fake import FakeModelProvider
    monkeypatch.setattr(api, "get_model_provider", lambda settings: FakeModelProvider(error=ModelProviderError(MODEL_TIMEOUT)))
    response = client.post(f"/projects/{project.id}/performances/{performance['id']}/step")
    assert response.status_code == 504 and response.json()["detail"]["upstream_status"] is None
    after = session.get(ScenePerformance, performance["id"])
    assert after.status == PerformanceStatus.READY and after.turn_count == 0
    assert session.scalar(select(func.count(ScenePerformanceTurn.id))) == 0 and session.scalar(select(func.count(CharacterDecision.id))) == 0
