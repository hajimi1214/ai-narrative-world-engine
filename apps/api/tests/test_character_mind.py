import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.character_mind import CharacterContextBuilder, CharacterDecisionConstraintChecker, ContextBudgetController, HeuristicCharacterActor, MAX_CHARACTER_MEMORIES
from app.db import Base
from app.director import DirectorContextBuilder
from app.main import app
import app.api as api
from app.models import Character, CharacterDecision, CharacterDecisionType, CharacterKnowledge, CharacterMemory, KnowledgeStatus, Project, ProposalType, SceneProposal, StoryThread, WorldEntity

@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db: yield db

def seed(session):
    project = Project(name="Mind test"); session.add(project); session.flush()
    location = WorldEntity(project_id=project.id, entity_type="LOCATION", name="Archive")
    session.add(location); session.flush()
    actor = Character(project_id=project.id, name="Ning Mo", current_state={"location_id": location.id}, goals={"current": "verify register"}, personality={"cautious": True}, core_values=["truth"], boundaries=["protect innocents"], narrative_relevance={"score": 10}, abilities=[{"id": "compare", "name": "Compare records", "status": "AVAILABLE", "director_only": {"real_cost": "hidden"}}, {"id": "sealed", "name": "Sealed skill", "status": "UNAVAILABLE"}], inventory=[{"id": "father-notes", "name": "Father notes"}], relationships={})
    other = Character(project_id=project.id, name="Liu Bai", current_state={"location_id": location.id}, goals={"current": "hide secret"})
    outsider = Character(project_id=project.id, name="Outsider", current_state={"location_id": location.id})
    thread = StoryThread(project_id=project.id, title="Register", type="MYSTERY", weight=1, goal="verify register")
    session.add_all([actor, other, outsider, thread]); session.commit()
    director_context = DirectorContextBuilder().build(session, project.id)
    proposal = SceneProposal(project_id=project.id, context_fingerprint=director_context["fingerprint"], proposal_type=ProposalType.CONTINUE_THREAD, primary_thread_id=thread.id, location_id=location.id, participants=[actor.id, other.id], scene_goal="Verify the archive register", planned_pressure="The keeper delays access", entry_state={"visible_context": {"keeper_present": True}, "secret": "hidden"}, expected_progress={"thread": thread.id}, character_motivations={}, allowed_reveals=[], forbidden_reveals=["secret"], required_canon=[], possible_outcomes=["hidden"], new_entity_requests=[], risk_flags=[], director_reasoning_summary="Director-only plan")
    session.add(proposal); session.commit()
    before_context = DirectorContextBuilder().build(session, project.id)
    proposal.context_fingerprint = before_context["fingerprint"]
    session.add(proposal); session.commit()
    after_context = DirectorContextBuilder().build(session, project.id)
    assert before_context == after_context, [key for key in before_context if before_context[key] != after_context[key]]
    return project, location, actor, other, outsider, proposal


def test_context_budget_reports_and_drops_lower_ranked_rows(session):
    project, location, actor, other, outsider, proposal = seed(session)
    for index in range(6):
        session.add(CharacterMemory(character_id=actor.id, content="x" * 500 + str(index), importance=10 - index, emotional_weight=0, confidence=1, distortion={}))
    session.commit()
    context = CharacterContextBuilder(budget={"max_chars": 900, "max_tokens": 900, "max_knowledge_items": 32, "max_memory_items": 6}).build(session, project.id, actor.id, proposal)
    assert context["context_budget"]["dropped_memory_items"] > 0
    assert context["context_budget"]["used_chars"] <= 900
    assert context["retrieval_evidence"]["sources"]


def test_context_budget_token_estimator_is_deterministic():
    controller = ContextBudgetController()
    assert controller.estimate_tokens({"text": "中文 novel 123"}) == controller.estimate_tokens({"text": "中文 novel 123"})

def context(session, project, actor, proposal): return CharacterContextBuilder().build(session, project.id, actor.id, proposal)

def decision(project, actor, proposal, ctx, **changes):
    data = {"project_id": project.id, "scene_proposal_id": proposal.id, "character_id": actor.id, "context_fingerprint": ctx["fingerprint"], "decision_type": CharacterDecisionType.INVESTIGATE, "intent": "verify register", "chosen_action": "Compare the register with father notes.", "motivation": "The current goal is verify register.", "goal_refs": ["verify register"], "knowledge_used": [], "memory_refs": [], "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "uncertainties": [], "refused_options": [], "decision_summary": "Ning Mo verifies the register before confronting anyone."}
    data.update(changes)
    return CharacterDecision(**data)

def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)

def test_context_contains_only_own_knowledge_and_isolated(session):
    project, _, actor, other, _, proposal = seed(session)
    session.add_all([CharacterKnowledge(character_id=actor.id, proposition="father left notes", status=KnowledgeStatus.KNOWN), CharacterKnowledge(character_id=other.id, proposition="Liu Bai secret", status=KnowledgeStatus.KNOWN)]); session.commit()
    actor_context = context(session, project, actor, proposal)
    other_context = context(session, project, other, proposal)
    assert actor_context["knowledge"]["KNOWN"][0]["proposition"] == "father left notes"
    assert other_context["knowledge"]["KNOWN"][0]["proposition"] == "Liu Bai secret"
    assert "Liu Bai secret" not in json.dumps(actor_context)

def test_context_hides_director_plan_and_reveal_fields(session):
    project, _, actor, _, _, proposal = seed(session)
    serialized = json.dumps(context(session, project, actor, proposal))
    for field in ("director_reasoning_summary", "possible_outcomes", "forbidden_reveals", "allowed_reveals", "required_canon", "hidden"):
        assert field not in serialized

def test_memory_selection_is_limited_and_private(session):
    project, location, actor, other, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_MEMORIES + 3): session.add(CharacterMemory(character_id=actor.id, content=f"Memory {index}", importance=index, emotional_weight=0.1, distortion={"location_id": location.id}))
    foreign = CharacterMemory(character_id=other.id, content="Foreign memory", importance=99, emotional_weight=1, distortion={})
    session.add(foreign); session.commit()
    memories = context(session, project, actor, proposal)["memories"]
    assert len(memories) == MAX_CHARACTER_MEMORIES
    assert foreign.id not in {item["memory_id"] for item in memories}

def test_foreign_memory_and_knowledge_leak_block(session):
    project, _, actor, other, _, proposal = seed(session)
    foreign_memory = CharacterMemory(character_id=other.id, content="Foreign", importance=1, emotional_weight=0, distortion={})
    session.add(foreign_memory); session.commit(); ctx = context(session, project, actor, proposal)
    report = CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, memory_refs=[foreign_memory.id], knowledge_used=["unknown fact"]))
    assert {issue.code for issue in report.issues}.issuperset({"MEMORY_NOT_RECALLED", "KNOWLEDGE_NOT_RECALLED"})

def test_false_belief_requires_explicit_status(session):
    project, _, actor, _, _, proposal = seed(session)
    belief = CharacterKnowledge(character_id=actor.id, proposition="Liu Bai is harmless", status=KnowledgeStatus.FALSE_BELIEF)
    session.add(belief); session.commit(); ctx = context(session, project, actor, proposal)
    allowed = decision(project, actor, proposal, ctx, knowledge_used=[{"knowledge_id": belief.id, "proposition": belief.proposition, "accepted_statuses": ["FALSE_BELIEF"]}])
    disguised = decision(project, actor, proposal, ctx, knowledge_used=[{"knowledge_id": belief.id, "proposition": belief.proposition, "accepted_statuses": ["KNOWN"]}])
    assert not any(issue.code == "KNOWLEDGE_NOT_RECALLED" for issue in CharacterDecisionConstraintChecker().validate(session, ctx, allowed).issues)
    assert any(issue.code == "KNOWLEDGE_NOT_RECALLED" for issue in CharacterDecisionConstraintChecker().validate(session, ctx, disguised).issues)

def test_inventory_and_ability_constraints(session):
    project, _, actor, _, _, proposal = seed(session); ctx = context(session, project, actor, proposal)
    checked = CharacterDecisionConstraintChecker()
    missing_item = checked.validate(session, ctx, decision(project, actor, proposal, ctx, inventory_refs=["missing-item"]))
    unknown = checked.validate(session, ctx, decision(project, actor, proposal, ctx, ability_refs=["unknown"]))
    unavailable = checked.validate(session, ctx, decision(project, actor, proposal, ctx, ability_refs=["sealed"]))
    assert any(issue.code == "INVENTORY_MISSING" for issue in missing_item.issues)
    assert any(issue.code == "ABILITY_UNKNOWN" for issue in unknown.issues)
    assert any(issue.code == "ABILITY_UNAVAILABLE" for issue in unavailable.issues)

def test_director_puppeting_and_stale_context_block(session):
    project, _, actor, _, _, proposal = seed(session); ctx = context(session, project, actor, proposal)
    puppeted = decision(project, actor, proposal, ctx, motivation="Director needs this action.")
    stale = decision(project, actor, proposal, ctx, context_fingerprint="character-context-v1:stale")
    assert any(issue.code == "DIRECTOR_PUPPETING" for issue in CharacterDecisionConstraintChecker().validate(session, ctx, puppeted).issues)
    assert any(issue.code == "CHARACTER_CONTEXT_STALE" for issue in CharacterDecisionConstraintChecker().validate(session, ctx, stale).issues)

def test_context_fingerprint_is_stable_and_changes_with_subjective_state(session):
    project, _, actor, _, _, proposal = seed(session)
    first = context(session, project, actor, proposal)["fingerprint"]
    assert context(session, project, actor, proposal)["fingerprint"] == first
    session.add(CharacterKnowledge(character_id=actor.id, proposition="new clue", status=KnowledgeStatus.KNOWN)); session.commit()
    after_knowledge = context(session, project, actor, proposal)["fingerprint"]
    session.add(CharacterMemory(character_id=actor.id, content="New memory", importance=1, emotional_weight=0, distortion={})); session.commit()
    after_memory = context(session, project, actor, proposal)["fingerprint"]
    actor.current_state = {"location_id": "changed"}; session.add(actor); session.commit()
    after_state = context(session, project, actor, proposal)["fingerprint"]
    assert len({first, after_knowledge, after_memory, after_state}) == 4

def test_character_dry_run_guards_and_does_not_modify_world(session, monkeypatch):
    project, _, actor, _, outsider, _ = seed(session)
    client = client_for(session, monkeypatch)
    director_response = client.post(f"/projects/{project.id}/director/dry-run")
    assert director_response.status_code == 201
    proposal_id = director_response.json()["proposal"]["id"]
    before = {"characters": session.scalar(select(func.count(Character.id))), "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))), "decisions": session.scalar(select(func.count(CharacterDecision.id)))}
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{actor.id}/dry-run")
    assert response.status_code == 201, response.json()
    assert response.json()["decision"]["status"] == "VALID"
    after = {"characters": session.scalar(select(func.count(Character.id))), "knowledge": session.scalar(select(func.count(CharacterKnowledge.id))), "decisions": session.scalar(select(func.count(CharacterDecision.id)))}
    assert after["characters"] == before["characters"] and after["knowledge"] == before["knowledge"] and after["decisions"] == before["decisions"] + 1
    assert client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{outsider.id}/dry-run").status_code == 409
    foreign = Project(name="Foreign"); session.add(foreign); session.flush(); foreign_character = Character(project_id=foreign.id, name="Foreign"); session.add(foreign_character); session.commit()
    assert client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{foreign_character.id}/dry-run").status_code == 404

def test_stale_scene_proposal_blocks_character_dry_run(session, monkeypatch):
    project, _, actor, _, _, _ = seed(session); client = client_for(session, monkeypatch)
    proposal_id = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]["id"]
    actor.goals = {"current": "changed"}; session.add(actor); session.commit()
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_id}/characters/{actor.id}/dry-run")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_SCENE_PROPOSAL"

def test_heuristic_decision_uses_actor_view_only(session):
    project, _, actor, _, _, proposal = seed(session)
    session.add(CharacterKnowledge(character_id=actor.id, proposition="father left notes", status=KnowledgeStatus.KNOWN)); session.commit()
    ctx = context(session, project, actor, proposal)
    generated = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint=ctx["fingerprint"], **HeuristicCharacterActor().decide(ctx))
    assert generated.decision_type == CharacterDecisionType.INVESTIGATE
    assert "Director" not in generated.motivation


def test_each_character_has_an_independent_agent_profile_and_unknowns_are_opaque(session, monkeypatch):
    project, _, actor, _, _, proposal = seed(session)
    actor.agent_profile = {"role": "谨慎的档案修复师", "private_rules": ["先验证再行动"], "voice": "短句，少用比喻"}
    session.add(actor)
    canon = __import__("app.models", fromlist=["CanonFact"]).CanonFact(project_id=project.id, fact_type="SECRET_CANON", proposition="秘密档案属于敌人")
    session.add(canon); session.commit()
    view = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    assert view["character"]["agent_id"] == actor.id
    assert view["character"]["agent_profile"]["role"] == "谨慎的档案修复师"
    assert any(item["canon_fact_id"] == canon.id for item in view["knowledge"]["UNKNOWN"])
    blocked = decision(project, actor, proposal, view, knowledge_used=[{"knowledge_id": canon.id, "proposition": canon.proposition, "accepted_statuses": ["KNOWN"]}])
    assert any(issue.code == "KNOWLEDGE_UNKNOWN" for issue in CharacterDecisionConstraintChecker().validate(session, view, blocked).issues)


def test_agent_profile_endpoint_and_disabled_agent_block_simulation(session, monkeypatch):
    project, _, actor, _, _, proposal = seed(session)
    client = client_for(session, monkeypatch)
    profile = client.patch(f"/projects/{project.id}/characters/{actor.id}/agent", json={"enabled": True, "profile": {"role": "调查者", "voice": "克制"}})
    assert profile.status_code == 200
    assert profile.json()["agent_id"] == actor.id
    assert profile.json()["model_route"] == "CHARACTER"
    actor.agent_enabled = False; session.add(actor); session.commit()
    blocked = client.post(f"/projects/{project.id}/director/proposals/{proposal.id}/characters/{actor.id}/dry-run")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "CHARACTER_AGENT_DISABLED"


def test_agent_context_endpoint_is_redacted_and_fingerprinted(session, monkeypatch):
    project, _, actor, _, _, proposal = seed(session)
    actor.secrets = ["未公开身份"]; session.add(actor); session.commit()
    client = client_for(session, monkeypatch)
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    session.add(proposal); session.commit()
    response = client.get(f"/projects/{project.id}/characters/{actor.id}/agent/context", params={"proposal_id": proposal.id})
    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == actor.id
    assert body["context_fingerprint"]
    rendered = json.dumps(body["actor_view"], ensure_ascii=False)
    assert "未公开身份" not in rendered
    assert body["redaction_policy"]["world_mutation"] == "forbidden"
