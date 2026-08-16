from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.director import DirectorConstraintChecker, DirectorContextBuilder, HeuristicDirector, RECENT_SCENE_LIMIT, extract_entity_references
from app.main import app
import app.api as api
from app.models import CanonFact, CanonType, Character, CharacterKnowledge, KnowledgeStatus, Project, ProposalStatus, ProposalType, RevealConstraint, RevealStatus, Scene, SceneProposal, StoryThread, WorldEntity

@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db

def seed(session):
    project = Project(name="Director test")
    session.add(project); session.flush()
    location = WorldEntity(project_id=project.id, entity_type="LOCATION", name="Archive")
    unrelated = WorldEntity(project_id=project.id, entity_type="CITY", name="Far City")
    session.add_all([location, unrelated]); session.flush()
    lead = Character(project_id=project.id, name="Ning Mo", current_state={"location_id": location.id}, goals={"current": "verify the register"}, narrative_relevance={"score": 9}, core_values=["truth"], boundaries=["will not harm innocents"])
    other = Character(project_id=project.id, name="Gu", current_state={"location_id": location.id}, narrative_relevance={"score": 2})
    thread = StoryThread(project_id=project.id, title="Archive identity", type="MYSTERY", weight=5, goal="identify the forged register")
    core = CanonFact(project_id=project.id, fact_type=CanonType.CORE_CANON, proposition="The archive has one sealed register", locked=True)
    secret = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="The register was forged")
    session.add_all([lead, other, thread, core, secret]); session.commit()
    return project, location, unrelated, lead, other, thread, core, secret

def proposal(project, lead, thread=None, **changes):
    data = {"project_id": project.id, "context_fingerprint": "test-context", "proposal_type": ProposalType.CONTINUE_THREAD, "primary_thread_id": thread.id if thread else None, "proposed_location": lead.current_state["location_id"], "participants": [lead.id], "scene_goal": "Verify the register", "character_motivations": {lead.id: {"reason": "verify the register"}}, "entry_state": {}, "planned_pressure": "The keeper refuses access", "expected_progress": {"thread": thread.id} if thread else {"character_arc": True}, "allowed_reveals": [], "forbidden_reveals": [], "required_canon": [], "possible_outcomes": ["A clue is found", "Access is denied"], "new_entity_requests": [], "risk_flags": [], "director_reasoning_summary": "The lead's current goal aligns with the active thread."}
    data.update(changes)
    return SceneProposal(**data)

def test_context_is_selective_and_limits_recent_scenes(session):
    project, location, unrelated, lead, _, thread, _, _ = seed(session)
    session.add_all([Scene(project_id=project.id, sequence=index, location=location.id, participants=[lead.id], story_threads=[thread.id], summary=f"Scene {index}") for index in range(15)])
    session.commit()
    context = DirectorContextBuilder().build(session, project.id)
    assert len(context["recent_scenes"]) == RECENT_SCENE_LIMIT
    assert {entity["id"] for entity in context["world_entities"]} == {location.id}
    assert unrelated.id not in {entity["id"] for entity in context["world_entities"]}

def test_context_keeps_knowledge_isolated_by_character(session):
    project, _, _, lead, other, _, _, _ = seed(session)
    session.add_all([CharacterKnowledge(character_id=lead.id, proposition="The seal is damaged", status=KnowledgeStatus.KNOWN), CharacterKnowledge(character_id=other.id, proposition="The keeper is bribed", status=KnowledgeStatus.SUSPECTED)])
    session.commit()
    knowledge = DirectorContextBuilder().build(session, project.id)["character_knowledge"]
    assert knowledge[lead.id]["KNOWN"][0]["proposition"] == "The seal is damaged"
    assert knowledge[other.id]["SUSPECTED"][0]["proposition"] == "The keeper is bribed"

def test_locked_canon_conflict_blocks_proposal(session):
    project, _, _, lead, _, thread, core, _ = seed(session)
    report = DirectorConstraintChecker().validate(session, DirectorContextBuilder().build(session, project.id), proposal(project, lead, thread, entry_state={"contradicts_canon_ids": [core.id]}))
    assert any(issue.code == "CANON_CONFLICT" and issue.severity == "BLOCKING" for issue in report.issues)

def test_unknown_knowledge_drives_knowledge_leak(session):
    project, _, _, lead, _, thread, _, _ = seed(session)
    report = DirectorConstraintChecker().validate(session, DirectorContextBuilder().build(session, project.id), proposal(project, lead, thread, character_motivations={lead.id: {"reason": "counter the forgery", "required_knowledge": ["complete replacement mechanism"]}}))
    assert any(issue.code == "KNOWLEDGE_LEAK" for issue in report.issues)

def test_locked_reveal_blocks_proposal(session):
    project, _, _, lead, _, thread, _, secret = seed(session)
    session.add(RevealConstraint(project_id=project.id, canon_fact_id=secret.id, status=RevealStatus.LOCKED, allowed_character_ids=[])); session.commit()
    report = DirectorConstraintChecker().validate(session, DirectorContextBuilder().build(session, project.id), proposal(project, lead, thread, allowed_reveals=[secret.id]))
    assert any(issue.code == "PREMATURE_REVEAL" and issue.severity == "BLOCKING" for issue in report.issues)

def test_threadless_scene_is_reported(session):
    project, _, _, lead, _, _, _, _ = seed(session)
    report = DirectorConstraintChecker().validate(session, DirectorContextBuilder().build(session, project.id), proposal(project, lead, primary_thread_id=None, expected_progress={}))
    assert any(issue.code == "THREADLESS_SCENE" for issue in report.issues)

def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)

def test_dry_run_only_creates_director_artifacts(session, monkeypatch):
    project, _, _, lead, _, thread, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    before = {"characters": session.scalar(select(func.count(Character.id))), "scenes": session.scalar(select(func.count(Scene.id))), "canon": session.scalar(select(func.count(CanonFact.id))), "threads": session.scalar(select(func.count(StoryThread.id)))}
    response = client.post(f"/projects/{project.id}/director/dry-run")
    assert response.status_code == 201
    assert response.json()["proposal"]["status"] == "VALID"
    after = {"characters": session.scalar(select(func.count(Character.id))), "scenes": session.scalar(select(func.count(Scene.id))), "canon": session.scalar(select(func.count(CanonFact.id))), "threads": session.scalar(select(func.count(StoryThread.id)))}
    assert after == before

def test_valid_proposal_can_be_approved(session, monkeypatch):
    project, _, _, _, _, _, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_data = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_data['id']}/approve")
    assert response.status_code == 200
    assert response.json()["proposal"]["status"] == "APPROVED"

def test_blocking_proposal_cannot_be_approved(session, monkeypatch):
    project, _, _, lead, _, thread, core, _ = seed(session)
    blocked = proposal(project, lead, thread, entry_state={"contradicts_canon_ids": [core.id]}, status=ProposalStatus.REJECTED)
    session.add(blocked); session.commit()
    response = client_for(session, monkeypatch).post(f"/projects/{project.id}/director/proposals/{blocked.id}/approve")
    assert response.status_code == 409

def test_proposal_can_be_rejected(session, monkeypatch):
    project, _, _, _, _, _, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_data = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_data['id']}/reject", json={"reason": "Not the intended direction."})
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"

def test_paused_thread_is_contextual_but_not_selected(session):
    project, _, _, _, _, open_thread, _, _ = seed(session)
    paused = StoryThread(project_id=project.id, title="Paused but heavy", type="MYSTERY", status="PAUSED", weight=99)
    session.add(paused); session.commit()
    context = DirectorContextBuilder().build(session, project.id)
    assert [item["id"] for item in context["active_story_threads"]] == [open_thread.id]
    assert [item["id"] for item in context["paused_story_threads"]] == [paused.id]
    assert HeuristicDirector().propose(context)["primary_thread_id"] == open_thread.id

def test_knowledge_statuses_are_not_interchangeable(session):
    project, _, _, lead, _, thread, _, _ = seed(session)
    session.add_all([CharacterKnowledge(character_id=lead.id, proposition="confirmed", status=KnowledgeStatus.KNOWN), CharacterKnowledge(character_id=lead.id, proposition="rumor", status=KnowledgeStatus.SUSPECTED), CharacterKnowledge(character_id=lead.id, proposition="wrong theory", status=KnowledgeStatus.FALSE_BELIEF)])
    session.commit(); context = DirectorContextBuilder().build(session, project.id)
    confirmed = proposal(project, lead, thread, character_motivations={lead.id: {"reason": "act", "required_knowledge": [{"proposition": "confirmed", "accepted_statuses": ["KNOWN"]}]}})
    suspected_as_known = proposal(project, lead, thread, character_motivations={lead.id: {"reason": "act", "required_knowledge": [{"proposition": "rumor", "accepted_statuses": ["KNOWN"]}]}})
    false_belief_allowed = proposal(project, lead, thread, character_motivations={lead.id: {"reason": "act", "required_knowledge": [{"proposition": "wrong theory", "accepted_statuses": ["FALSE_BELIEF"]}]}})
    assert not any(issue.code == "KNOWLEDGE_LEAK" for issue in DirectorConstraintChecker().validate(session, context, confirmed).issues)
    assert any(issue.code == "KNOWLEDGE_LEAK" for issue in DirectorConstraintChecker().validate(session, context, suspected_as_known).issues)
    assert not any(issue.code == "KNOWLEDGE_LEAK" for issue in DirectorConstraintChecker().validate(session, context, false_belief_allowed).issues)

def test_fingerprint_changes_for_director_inputs(session):
    project, _, _, lead, _, thread, _, _ = seed(session)
    before = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    assert DirectorContextBuilder().build(session, project.id)["fingerprint"] == before
    session.add(CharacterKnowledge(character_id=lead.id, proposition="new clue", status=KnowledgeStatus.KNOWN)); session.commit()
    after_knowledge = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    lead.current_state = {"location_id": "new-location"}; session.add(lead); session.commit()
    after_state = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    session.add(Scene(project_id=project.id, sequence=1, status="OCCURRED", facts=[{"entity_ids": ["new-location"]}])); session.commit()
    after_scene = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    thread.progress = 0.5; session.add(thread); session.commit()
    after_thread = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    assert len({before, after_knowledge, after_state, after_scene, after_thread}) == 5

def test_irrelevant_secret_canon_is_not_in_context(session):
    project, _, _, _, _, _, _, secret = seed(session)
    context = DirectorContextBuilder().build(session, project.id)
    assert secret.id not in {fact["id"] for fact in context["canon"]}
    secret.data = {"global_director_required": True}; session.add(secret); session.commit()
    assert secret.id in {fact["id"] for fact in DirectorContextBuilder().build(session, project.id)["canon"]}

def test_reveal_constraint_rejects_cross_project_references(session, monkeypatch):
    project, _, _, lead, _, _, _, local_secret = seed(session)
    foreign = Project(name="Foreign"); session.add(foreign); session.flush()
    foreign_canon = CanonFact(project_id=foreign.id, fact_type=CanonType.SECRET_CANON, proposition="Foreign secret")
    foreign_character = Character(project_id=foreign.id, name="Foreign character")
    session.add_all([foreign_canon, foreign_character]); session.commit()
    client = client_for(session, monkeypatch)
    bad_canon = client.post(f"/projects/{project.id}/reveal-constraints", json={"canon_fact_id": foreign_canon.id, "allowed_character_ids": [lead.id]} )
    bad_character = client.post(f"/projects/{project.id}/reveal-constraints", json={"canon_fact_id": local_secret.id, "allowed_character_ids": [foreign_character.id]} )
    assert bad_canon.status_code == 409
    assert bad_character.status_code == 409

def test_stale_proposal_cannot_be_approved(session, monkeypatch):
    project, _, _, lead, _, _, _, _ = seed(session)
    client = client_for(session, monkeypatch)
    proposal_data = client.post(f"/projects/{project.id}/director/dry-run").json()["proposal"]
    lead.goals = {"current": "different goal"}; session.add(lead); session.commit()
    response = client.post(f"/projects/{project.id}/director/proposals/{proposal_data['id']}/approve")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_PROPOSAL"

def test_entity_references_ignore_unstructured_strings():
    assert extract_entity_references({"summary": "city-id", "unknown": {"entity_id": "ignored"}}, [{"entity_ids": ["one"], "location_id": "two"}]) == {"one", "two"}
