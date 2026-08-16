from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.director import DirectorConstraintChecker, DirectorContextBuilder, RECENT_SCENE_LIMIT
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
    lead = Character(project_id=project.id, name="Ning Mo", current_state={"location": location.id}, goals={"current": "verify the register"}, narrative_relevance={"score": 9}, core_values=["truth"], boundaries=["will not harm innocents"])
    other = Character(project_id=project.id, name="Gu", current_state={"location": location.id}, narrative_relevance={"score": 2})
    thread = StoryThread(project_id=project.id, title="Archive identity", type="MYSTERY", weight=5, goal="identify the forged register")
    core = CanonFact(project_id=project.id, fact_type=CanonType.CORE_CANON, proposition="The archive has one sealed register", locked=True)
    secret = CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="The register was forged")
    session.add_all([lead, other, thread, core, secret]); session.commit()
    return project, location, unrelated, lead, other, thread, core, secret

def proposal(project, lead, thread=None, **changes):
    data = {"project_id": project.id, "proposal_type": ProposalType.CONTINUE_THREAD, "primary_thread_id": thread.id if thread else None, "proposed_location": lead.current_state["location"], "participants": [lead.id], "scene_goal": "Verify the register", "character_motivations": {lead.id: {"reason": "verify the register"}}, "entry_state": {}, "planned_pressure": "The keeper refuses access", "expected_progress": {"thread": thread.id} if thread else {"character_arc": True}, "allowed_reveals": [], "forbidden_reveals": [], "required_canon": [], "possible_outcomes": ["A clue is found", "Access is denied"], "new_entity_requests": [], "risk_flags": [], "director_reasoning_summary": "The lead's current goal aligns with the active thread."}
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
