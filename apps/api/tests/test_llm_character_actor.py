import json
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.ai.errors import MODEL_AUTH_FAILED, MODEL_OUTPUT_INVALID, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR, ModelProviderError
from app.ai.fake import FakeModelProvider
from app.character_mind import ActorPerceptionSanitizer, CharacterContextBuilder, CharacterDecisionConstraintChecker
from app.llm_actor import LLMCharacterActor
from app.models import CharacterDecision, CharacterKnowledge, CharacterMemory, KnowledgeStatus
from app.db import Base
import app.api as api
from test_character_mind import client_for, context, decision, seed


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def valid_payload(**changes):
    payload = {
        "decision_type": "INVESTIGATE", "intent": "verify register", "chosen_action": "Compare the available notes with the register.",
        "motivation": "Verifying the register serves the current goal.", "goal_refs": ["verify register"],
        "knowledge_used": [], "memory_refs": [], "ability_refs": [], "inventory_refs": ["father-notes"],
        "relationship_factors": {}, "perceived_risk": "The keeper may delay access.", "accepted_cost": "Time.",
        "uncertainties": ["The records may be incomplete."], "refused_options": [], "decision_summary": "Verify records before escalating.",
    }
    payload.update(changes)
    return json.dumps(payload)


def test_perception_sanitizer_is_whitelist_and_isolates_actor_visible_context(session):
    project, _, actor, other, _, proposal = seed(session)
    proposal.entry_state = {"visible_context": {"keeper_present": True}, "actor_visible_context": {actor.id: {"noticed": "keeper avoids a book"}, other.id: {"noticed": "Ning watches hands"}}}
    session.add(proposal); session.commit()
    view = ActorPerceptionSanitizer().sanitize(context(session, project, actor, proposal))
    rendered = json.dumps(view)
    assert view["scene"]["visible_context"] == {"keeper_present": True}
    assert view["scene"]["actor_visible_context"] == {"noticed": "keeper avoids a book"}
    for hidden in ("narrative_relevance", "scene_goal", "planned_pressure", "possible_outcomes", "forbidden_reveals", "director_reasoning_summary", "visible_goal", "visible_pressure", "Ning watches hands"):
        assert hidden not in rendered


def test_llm_actor_parses_json_fence_and_repairs_once():
    fenced = "```json\n" + valid_payload() + "\n```"
    payload, _ = LLMCharacterActor(FakeModelProvider(fenced), "test").decide({})
    assert payload["decision_type"] == "INVESTIGATE"
    provider = FakeModelProvider(["not json", valid_payload()])
    payload, _ = LLMCharacterActor(provider, "test").decide({})
    assert payload["chosen_action"] and provider.calls == 2


def test_llm_actor_rejects_two_invalid_json_outputs():
    with pytest.raises(ModelProviderError) as error:
        LLMCharacterActor(FakeModelProvider(["bad", "still bad"]), "test").decide({})
    assert error.value.code == MODEL_OUTPUT_INVALID


@pytest.mark.parametrize("payload,code", [
    (valid_payload(knowledge_used=["invented fact"]), "KNOWLEDGE_LEAK"),
    (valid_payload(memory_refs=["foreign-memory"]), "FOREIGN_MEMORY"),
    (valid_payload(inventory_refs=["missing-item"]), "INVENTORY_MISSING"),
    (valid_payload(ability_refs=["unknown"]), "ABILITY_UNKNOWN"),
    (valid_payload(ability_refs=["sealed"]), "ABILITY_UNAVAILABLE"),
])
def test_llm_payload_constraints_block_invalid_references(session, payload, code):
    project, _, actor, _, _, proposal = seed(session)
    ctx = context(session, project, actor, proposal)
    generated, _ = LLMCharacterActor(FakeModelProvider(payload), "test").decide(ActorPerceptionSanitizer().sanitize(ctx))
    report = CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, **generated))
    assert any(issue.code == code and issue.severity == "BLOCKING" for issue in report.issues)


def test_llm_allows_refuse_and_false_belief_only_when_explicit(session):
    project, _, actor, _, _, proposal = seed(session)
    session.add(CharacterKnowledge(character_id=actor.id, proposition="Liu Bai is harmless", status=KnowledgeStatus.FALSE_BELIEF)); session.commit()
    ctx = context(session, project, actor, proposal)
    allowed, _ = LLMCharacterActor(FakeModelProvider(valid_payload(decision_type="REFUSE", knowledge_used=[{"proposition": "Liu Bai is harmless", "accepted_statuses": ["FALSE_BELIEF"]}])), "test").decide({})
    disguised, _ = LLMCharacterActor(FakeModelProvider(valid_payload(knowledge_used=[{"proposition": "Liu Bai is harmless", "accepted_statuses": ["KNOWN"]}])), "test").decide({})
    assert not any(issue.code == "KNOWLEDGE_LEAK" for issue in CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, **allowed)).issues)
    assert any(issue.code == "KNOWLEDGE_LEAK" for issue in CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, **disguised)).issues)


def test_ai_dry_run_uses_fake_provider_and_only_adds_decision(session, monkeypatch):
    project, _, actor, _, outsider, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_id = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]["id"]
    provider = FakeModelProvider(valid_payload())
    monkeypatch.setattr(api, "get_model_provider", lambda settings: provider)
    before = {"decisions": session.scalar(select(func.count(CharacterDecision.id))), "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))), "memories": session.scalar(select(func.count(CharacterMemory.id)))}
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{actor.id}/ai-dry-run")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["decision"]["status"] == "VALID" and body["model_metadata"]["provider"] == "fake"
    assert "narrative_relevance" not in json.dumps(body["character_context_summary"])
    after = {"decisions": session.scalar(select(func.count(CharacterDecision.id))), "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))), "memories": session.scalar(select(func.count(CharacterMemory.id)))}
    assert after["decisions"] == before["decisions"] + 1 and after["knowledge"] == before["knowledge"] and after["memories"] == before["memories"]
    assert client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{outsider.id}/ai-dry-run").status_code == 409


@pytest.mark.parametrize("error,code", [
    (ModelProviderError(MODEL_TIMEOUT), MODEL_TIMEOUT),
    (ModelProviderError(MODEL_AUTH_FAILED, "secret should never be exposed"), MODEL_AUTH_FAILED),
    (ModelProviderError(MODEL_UPSTREAM_ERROR, "sensitive upstream body"), MODEL_UPSTREAM_ERROR),
])
def test_ai_dry_run_returns_safe_provider_errors(session, monkeypatch, error, code):
    project, _, actor, _, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_id = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]["id"]
    monkeypatch.setattr(api, "get_model_provider", lambda settings: FakeModelProvider(error=error))
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{actor.id}/ai-dry-run")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == code
    assert "secret should never be exposed" not in response.text and "sensitive upstream body" not in response.text


def test_ai_dry_run_blocks_stale_proposal(session, monkeypatch):
    project, _, actor, _, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_id = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]["id"]
    actor.goals = {"current": "changed"}; session.add(actor); session.commit()
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{actor.id}/ai-dry-run")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "STALE_SCENE_PROPOSAL"
