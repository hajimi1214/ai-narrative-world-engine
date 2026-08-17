import json
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from app.db import Base
from app.main import app
import app.api as api
from app.models import CanonFact, CharacterKnowledge, Scene, WorldRevision, RetconImpactPlan, RetconImpactItem, CharacterDecision, CharacterDecisionType, CharacterDecisionStatus, ScenePerformance, ScenePerformanceTurn, WorldResolution, ActionVisibility, PerformanceMode, PerformanceStatus, ResolverMode, ResolutionStatus, ResolutionOutcome
from app.revision import RevisionStateFingerprintBuilder
from app.retcon import HistoricalDependencyGraphBuilder
from test_character_mind import seed

@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db: yield db

def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)

def prepared(session, monkeypatch):
    project, location, actor, other, _, _ = __import__("test_scene_performance", fromlist=["approved_setup"]).approved_setup(session, monkeypatch)
    canon = CanonFact(project_id=project.id, fact_type="CORE_CANON", proposition="old location truth", data={}, locked=True)
    session.add(canon); session.flush()
    knowledge = CharacterKnowledge(character_id=actor.id, proposition=canon.proposition, status="KNOWN", source=canon.id)
    scene = Scene(project_id=project.id, sequence=10, participants=[actor.id], facts=[canon.proposition], result={})
    independent = Scene(project_id=project.id, sequence=11, participants=[other.id], facts=["unrelated"], result={})
    session.add_all([knowledge, scene, independent]); session.commit()
    client = client_for(session, monkeypatch)
    revision = client.post(f"/projects/{project.id}/revisions", json={"title":"移动事实", "changes":[{"target_type":"CANON_FACT", "target_id":canon.id, "operation":"SET", "path":"/proposition", "value":"new location truth"}]}).json()
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").status_code == 200
    return project, canon, knowledge, scene, independent, revision, client

def test_create_request_and_actions(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"重新审视案发位置"})
    assert response.status_code == 201 and response.json()["available_actions"] == ["ANALYZE", "ABORT"]

def test_cross_project_request_blocked(session, monkeypatch):
    project, *_ = prepared(session, monkeypatch)
    other_project, *_ = seed(session)
    foreign_revision = WorldRevision(project_id=other_project.id, title="foreign", status="PREVIEWED", base_state_fingerprint="x", normalized_changes=[], impact_report={})
    session.add(foreign_revision); session.commit(); client = client_for(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":foreign_revision.id,"reason":"foreign"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "CROSS_PROJECT_REFERENCE"

def test_analyze_creates_append_only_plan_and_preserves_formal_world(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    before = {"canon": canon.proposition, "knowledge": knowledge.proposition, "scene": scene.facts, "counts": session.scalar(select(func.count(CanonFact.id))), "formal_fingerprint": RevisionStateFingerprintBuilder().build(session, project.id)}
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"影响预览"}).json()
    analyzed = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    body = analyzed.json(); assert body["request"]["status"] == "PLANNED"; assert body["plan"]["version"] == 1
    assert canon.proposition == before["canon"] and knowledge.proposition == before["knowledge"] and scene.facts == before["scene"]
    assert session.scalar(select(func.count(CanonFact.id))) == before["counts"]
    assert RevisionStateFingerprintBuilder().build(session, project.id) == before["formal_fingerprint"]

def test_second_analyze_creates_plan_v2_with_parent(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"repeat"}).json()
    first = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()["plan"]
    second = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()["plan"]
    assert second["version"] == 2
    assert second["parent_plan_id"] == first["id"]
    assert session.scalar(select(func.count(RetconImpactPlan.id))) == 2

def test_classifications_and_preserved_history(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"classify"}).json()
    body = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()
    by_resource = {(item["resource_type"], item["resource_id"]):item["classification"] for item in body["items"]}
    assert by_resource[("CHARACTER_KNOWLEDGE", knowledge.id)] == "REBUILD_COGNITION"
    assert by_resource[("SCENE", scene.id)] == "REPLAY_REQUIRED"
    assert by_resource[("SCENE", independent.id)] == "UNCHANGED"
    assert body["plan"]["earliest_affected_scene_id"] == scene.id

def test_dependency_path_and_reason_are_auditable(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"audit"}).json()
    items = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()["items"]
    cognition = next(item for item in items if item["resource_type"] == "CHARACTER_KNOWLEDGE")
    assert cognition["reason_code"] == "CANON_TO_KNOWLEDGE" and cognition["dependency_path"] and cognition["reason_summary"]

def test_aborted_request_cannot_analyze_and_actions_never_apply(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"abort"}).json()
    client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/abort")
    blocked = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "RETCON_REQUEST_ABORTED"
    assert client.get(f"/projects/{project.id}/retcon/requests/{request['id']}").json()["available_actions"] == []

def test_no_apply_or_replay_endpoints(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"routes"}).json()
    assert client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply").status_code == 404
    assert client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/replay").status_code == 404

def test_plan_stale_after_formal_change(session, monkeypatch):
    project, canon, *_unused, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"stale"}).json()
    plan = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()["plan"]
    canon.proposition = "changed after analysis"; session.add(canon); session.commit()
    detail = client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]
    assert detail["is_stale"] is True and detail["status"] == "STALE"

def test_basis_fingerprint_ignores_retcon_artifacts(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    before = RevisionStateFingerprintBuilder().build(session, project.id)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"fingerprint"}).json()
    client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert RevisionStateFingerprintBuilder().build(session, project.id) == before

def test_text_only_scene_does_not_create_dependency(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    independent.summary = canon.proposition; session.add(independent); session.commit()
    stored = session.get(WorldRevision, revision["id"]); stored.base_state_fingerprint = RevisionStateFingerprintBuilder().build(session, project.id); session.add(stored); session.commit()
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"text"}).json()
    items = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()["items"]
    item = next(item for item in items if item["resource_id"] == independent.id and item["resource_type"] == "SCENE")
    assert item["classification"] == "UNCHANGED"

def test_dependency_graph_has_bounded_traversal():
    graph = HistoricalDependencyGraphBuilder(max_nodes=0)
    assert graph.max_nodes == 0 and graph.limit_reached is False

def test_canon_knowledge_decision_turn_resolution_chain(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    proposal = session.query(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=knowledge.character_id, context_fingerprint="ctx", decision_type=CharacterDecisionType.INVESTIGATE, intent="check", chosen_action="check", target_character_id=None, target_entity_id=None, motivation="know", goal_refs=[], knowledge_used=[{"knowledge_id":knowledge.id,"proposition":knowledge.proposition}], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], boundary_override_reason=None, decision_summary="check", status=CharacterDecisionStatus.VALID)
    performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=99, proposal_context_fingerprint="ctx", mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.RUNNING, participant_order=[], active_participant_ids=[], max_turns=1, turn_count=1)
    session.add_all([decision, performance]); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=performance.id, sequence=1, actor_character_id=knowledge.character_id, actor_context_fingerprint="ctx", character_decision_id=decision.id, action_visibility=ActionVisibility.PUBLIC, observable_action="check", spoken_content=None, recipient_character_ids=[], requires_world_resolution=True, world_resolution_request={}, validation_result={})
    session.add(turn); session.flush()
    resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="ctx", status=ResolutionStatus.VALID, outcome=ResolutionOutcome.SUCCESS, outcome_summary="ok", objective_facts=[], actor_observation=None, public_observation=None, recipient_character_ids=[], canon_fact_ids_used=[canon.id], world_entity_ids_used=[], resolution_basis_summary=None, missing_information=[])
    session.add(resolution); session.commit(); stored = session.get(WorldRevision, revision["id"]); stored.base_state_fingerprint = RevisionStateFingerprintBuilder().build(session, project.id); session.add(stored); session.commit()
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id":revision["id"],"reason":"chain"}).json()
    items = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()["items"]
    states = {(item["resource_type"],item["resource_id"]):item["classification"] for item in items}
    assert states[("CHARACTER_DECISION",decision.id)] == "REPLAY_REQUIRED"
    assert states[("SCENE_PERFORMANCE_TURN",turn.id)] == "REPLAY_REQUIRED"
    assert states[("WORLD_RESOLUTION",resolution.id)] == "INVALIDATED"
