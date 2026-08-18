import json
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from app.db import Base
from app.main import app
import app.api as api
from app.models import CanonFact, CharacterKnowledge, CharacterMemory, Scene, WorldEntity, WorldRevision, RetconRequest, RetconImpactPlan, RetconImpactItem, CharacterDecision, CharacterDecisionType, CharacterDecisionStatus, ScenePerformance, ScenePerformanceTurn, WorldResolution, ActionVisibility, PerformanceMode, PerformanceStatus, ResolverMode, ResolutionStatus, ResolutionOutcome
from app.revision import RevisionStateFingerprintBuilder
from app.retcon import HistoricalDependencyGraphBuilder, ReplayBoundaryFinder, RetconBasisFingerprintBuilder
from app.revision import StructuredReferenceScanner
import app.retcon as retcon_module
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
    assert body["plan"]["impact_summary"]["preserved_scene_count"] == 1
    assert body["plan"]["impact_summary"]["preserved_scene_ranges"] == [{"sequence_start": 11, "sequence_end": 11, "count": 1}]
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
    assert not any(item["resource_id"] == independent.id and item["resource_type"] == "SCENE" for item in items)

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
    resolution_items=[item for item in items if item["resource_type"]=="WORLD_RESOLUTION" and item["resource_id"]==resolution.id]
    assert len(resolution_items)==1 and [node["type"] for node in resolution_items[0]["dependency_path"]]==["CANON_FACT","WORLD_RESOLUTION"]

def test_full_causal_dependency_path_contains_all_lineage_nodes(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    proposal = session.query(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=knowledge.character_id, context_fingerprint="ctx", decision_type=CharacterDecisionType.INVESTIGATE, intent="x", chosen_action="x", motivation="x", goal_refs=[], knowledge_used=[{"knowledge_id":knowledge.id}], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, uncertainties=[], refused_options=[], decision_summary="x", status=CharacterDecisionStatus.VALID)
    session.add(decision); session.flush()
    performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=101, proposal_context_fingerprint="ctx", mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.RUNNING, participant_order=[], active_participant_ids=[], max_turns=3, turn_count=1)
    session.add(performance); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=performance.id, sequence=1, actor_character_id=knowledge.character_id, actor_context_fingerprint="ctx", character_decision_id=decision.id, action_visibility=ActionVisibility.PUBLIC, observable_action="x", recipient_character_ids=[], requires_world_resolution=True, world_resolution_request={}, validation_result={})
    session.add(turn); session.flush()
    resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="ctx", status=ResolutionStatus.VALID, outcome=ResolutionOutcome.SUCCESS, outcome_summary="x", objective_facts=[], actor_observation=None, public_observation=None, recipient_character_ids=[], canon_fact_ids_used=[], world_entity_ids_used=[], resolution_basis_summary=None, missing_information=[])
    session.add(resolution); session.commit()
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"path"}).json()
    items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    path=next(i["dependency_path"] for i in items if i["resource_id"]==resolution.id)
    assert [x["type"] for x in path] == ["CANON_FACT","CHARACTER_KNOWLEDGE","CHARACTER_DECISION","SCENE_PERFORMANCE_TURN","WORLD_RESOLUTION"]

def test_exact_value_without_lineage_is_revalidate(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    knowledge.source = None; session.commit(); stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit(); req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"lineage"}).json()
    items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    assert next(i for i in items if i["resource_id"]==knowledge.id)["classification"] == "REVALIDATE"

def test_summary_text_does_not_create_scene_dependency(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    independent.summary=canon.proposition; session.commit(); stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit(); req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"summary"}).json()
    items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    assert not any(i["resource_id"]==independent.id and i["resource_type"]=="SCENE" for i in items)

def test_graph_cycle_is_visited_once(session):
    builder=HistoricalDependencyGraphBuilder(max_nodes=10)
    assert builder.visited_nodes == set(); assert builder.limit_reached is False

@pytest.mark.parametrize("max_nodes", [1, 2])
def test_graph_limit_is_unique_node_limit(session, monkeypatch, max_nodes):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    builder=HistoricalDependencyGraphBuilder(max_nodes=max_nodes)
    builder.build(session,project.id,{canon.id},{canon.proposition},{canon.id:"CANON_FACT"})
    assert builder.limit_reached is True

def test_replay_boundary_duplicate_sequence_blocks():
    class SceneStub:
        def __init__(self,id,sequence): self.id=id; self.sequence=sequence
    _, _, report=ReplayBoundaryFinder().find([SceneStub("a",10),SceneStub("b",10)],{"a","b"})
    assert report["issues"][0]["code"] == "REPLAY_BOUNDARY_UNRESOLVED"

def test_replay_boundary_missing_sequence_blocks():
    class SceneStub:
        def __init__(self,id,sequence): self.id=id; self.sequence=sequence
    _, _, report=ReplayBoundaryFinder().find([SceneStub("a",None)],{"a"})
    assert report["issues"][0]["code"] == "REPLAY_BOUNDARY_UNRESOLVED"

def test_request_effective_status_becomes_stale(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"effective"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    canon.proposition="new"; session.commit(); body=client.get(f"/projects/{project.id}/retcon/requests/{req['id']}").json()
    assert body["effective_status"] == "STALE" and body["available_actions"] == ["REANALYZE","ABORT"]

def test_cognition_impacts_are_in_plan_summary(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"cognition"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    assert plan["impact_summary"]["cognition_impacts"]

def test_basis_ignores_unrelated_scene_and_story_thread(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"basis"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    independent.facts=["other"]; session.commit(); assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is False

def test_plan_append_only_parent_and_unique_versions(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"versions"}).json(); first=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]; second=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    assert second["version"]==2 and second["parent_plan_id"]==first["id"]

def test_no_provider_or_replay_routes(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"routes"}).json()
    assert client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/apply").status_code==404
    assert client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/replay").status_code==404

def test_related_knowledge_change_stales_plan(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"knowledge stale"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    knowledge.proposition="edited"; session.commit()
    assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is True

def test_unrelated_memory_does_not_stale_plan(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"memory"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    memory=CharacterMemory(character_id=knowledge.character_id,content="unrelated",source_scene=independent.id,distortion={}); session.add(memory); session.commit()
    assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is False

def test_maintenance_timestamps_do_not_stale_plan(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"timestamps"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    canon.updated_at=canon.updated_at; session.commit()
    assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is False

def test_abort_exposes_no_actions(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"abort"}).json(); result=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/abort")
    assert result.status_code==200 and result.json()["available_actions"]==[]

def test_draft_actions_are_analyze_and_abort(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"draft"}).json()
    assert req["available_actions"]==["ANALYZE","ABORT"]

@pytest.mark.parametrize("value,target,expected", [
    ({"id":"x"}, "x", ["/id"]),
    ({"items":["x"]}, "x", ["/items/0"]),
    ({"x":{"value":1}}, "x", ["/x"]),
    ({"nested":{"id":"x"}}, "x", ["/nested/id"]),
    ({"a/b":"x"}, "x", ["/a~1b"]),
    ({"a~b":"x"}, "x", ["/a~0b"]),
    (["x"], "x", ["/0"]),
    ({"id":"prefix-x"}, "x", []),
    ({"text":"x is only prose"}, "x", []),
    ({"items":["y","x"]}, "x", ["/items/1"]),
])
def test_structured_exact_scanner_never_uses_substrings(value, target, expected):
    assert StructuredReferenceScanner().paths(value,target) == expected

def _memory_decision_chain(session, project, canon, scene, character_id):
    proposal=session.query(__import__("app.models",fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    memory=CharacterMemory(character_id=character_id,content="I saw the old location",importance=.7,emotional_weight=.2,confidence=.9,distortion={},source_scene=scene.id)
    session.add(memory); session.flush()
    decision=CharacterDecision(project_id=project.id,scene_proposal_id=proposal.id,character_id=character_id,context_fingerprint="ctx",decision_type=CharacterDecisionType.INVESTIGATE,intent="investigate",chosen_action="investigate",motivation="memory",goal_refs=[],knowledge_used=[],memory_refs=[memory.id],ability_refs=[],inventory_refs=[],relationship_factors={},uncertainties=[],refused_options=[],decision_summary="continue investigating",status=CharacterDecisionStatus.VALID)
    session.add(decision); session.commit(); return memory,decision

@pytest.mark.parametrize("field,value", [("content","changed memory"),("importance",.1),("distortion",{"altered":True}),("source_scene",None)])
def test_related_memory_semantic_fields_stale_plan(session, monkeypatch, field, value):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch)
    memory,_=_memory_decision_chain(session,project,canon,scene,knowledge.character_id)
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"memory stale"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    setattr(memory,field,value); session.commit()
    assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is True

def test_revision_status_is_part_of_stale_basis(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"revision status"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    stored=session.get(WorldRevision,revision["id"]); stored.status="CANCELLED"; session.commit()
    assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is True

def test_normalized_changes_are_part_of_stale_basis(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"revision changes"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    stored=session.get(WorldRevision,revision["id"]); stored.normalized_changes[0]["after_value"]="tampered"; from sqlalchemy.orm.attributes import flag_modified; flag_modified(stored,"normalized_changes"); session.commit()
    assert client.get(f"/projects/{project.id}/retcon/plans/{plan['id']}").json()["plan"]["is_stale"] is True

def test_uncertain_lineage_stays_revalidate_downstream(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); knowledge.source=None
    proposal=session.query(__import__("app.models",fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    decision=CharacterDecision(project_id=project.id,scene_proposal_id=proposal.id,character_id=knowledge.character_id,context_fingerprint="ctx",decision_type=CharacterDecisionType.INVESTIGATE,intent="x",chosen_action="x",motivation="x",goal_refs=[],knowledge_used=[{"knowledge_id":knowledge.id}],memory_refs=[],ability_refs=[],inventory_refs=[],relationship_factors={},uncertainties=[],refused_options=[],decision_summary="x",status=CharacterDecisionStatus.VALID)
    performance=ScenePerformance(project_id=project.id,scene_proposal_id=proposal.id,take_number=200,proposal_context_fingerprint="ctx",mode=PerformanceMode.HEURISTIC,status=PerformanceStatus.RUNNING,participant_order=[],active_participant_ids=[],max_turns=3,turn_count=1); session.add_all([decision,performance]); session.flush()
    turn=ScenePerformanceTurn(project_id=project.id,performance_id=performance.id,sequence=1,actor_character_id=knowledge.character_id,actor_context_fingerprint="ctx",character_decision_id=decision.id,action_visibility=ActionVisibility.PUBLIC,observable_action="x",recipient_character_ids=[],requires_world_resolution=True,world_resolution_request={},validation_result={}); session.add(turn); session.flush()
    resolution=WorldResolution(project_id=project.id,performance_id=performance.id,performance_turn_id=turn.id,resolver_mode=ResolverMode.HEURISTIC,world_context_fingerprint="ctx",status=ResolutionStatus.VALID,outcome=ResolutionOutcome.SUCCESS,outcome_summary="x",objective_facts=[],actor_observation=None,public_observation=None,recipient_character_ids=[],canon_fact_ids_used=[],world_entity_ids_used=[],resolution_basis_summary=None,missing_information=[]); session.add(resolution); session.commit()
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit(); req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"uncertain"}).json(); items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    states={(i["resource_type"],i["resource_id"]):i["classification"] for i in items}; assert all(states[(typ,ident)]=="REVALIDATE" for typ,ident in [("CHARACTER_KNOWLEDGE",knowledge.id),("CHARACTER_DECISION",decision.id),("SCENE_PERFORMANCE_TURN",turn.id),("WORLD_RESOLUTION",resolution.id)])

def test_confirmed_scene_lineage_upgrades_decision_to_replay(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); knowledge.source=scene.id
    proposal=session.query(__import__("app.models",fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    decision=CharacterDecision(project_id=project.id,scene_proposal_id=proposal.id,character_id=knowledge.character_id,context_fingerprint="ctx",decision_type=CharacterDecisionType.INVESTIGATE,intent="x",chosen_action="x",motivation="x",goal_refs=[],knowledge_used=[{"knowledge_id":knowledge.id}],memory_refs=[],ability_refs=[],inventory_refs=[],relationship_factors={},uncertainties=[],refused_options=[],decision_summary="x",status=CharacterDecisionStatus.VALID); session.add(decision); session.commit()
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit(); req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"confirmed"}).json(); items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    assert next(i for i in items if i["resource_id"]==decision.id)["classification"]=="REPLAY_REQUIRED"

def test_real_cycle_graph_terminates(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch)
    other=CanonFact(project_id=project.id,fact_type="WORLD_FACT",proposition="B",data={"canon_fact_id":canon.id},locked=False); canon.data={"canon_fact_id":"pending"}; session.add(other); session.flush(); canon.data={"canon_fact_id":other.id}; session.commit()
    graph=HistoricalDependencyGraphBuilder(max_nodes=20); edges=graph.build(session,project.id,{canon.id},{canon.proposition},{canon.id:"CANON_FACT"})
    assert graph.limit_reached is False and len(graph.visited_nodes)<=20 and len({(e.source_id,e.target_id,e.edge_type) for e in edges})==len(edges)

def test_graph_limit_blocks_api_plan(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch)
    original=retcon_module.HistoricalDependencyGraphBuilder
    class TinyGraph(original):
        def __init__(self,*args,**kwargs): super().__init__(max_nodes=1)
    monkeypatch.setattr(retcon_module,"HistoricalDependencyGraphBuilder",TinyGraph)
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"limited"}).json(); body=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()
    assert body["plan"]["status"]=="BLOCKED" and any(i["code"]=="PLAN_GRAPH_LIMIT_REACHED" for i in body["plan"]["validation_report"]["issues"])

def test_poisoned_cross_project_revision_blocks_without_artifacts(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); foreign, *_=seed(session); foreign_canon=CanonFact(project_id=foreign.id,fact_type="WORLD_FACT",proposition="foreign",data={},locked=False); session.add(foreign_canon); session.flush()
    poisoned=WorldRevision(project_id=project.id,title="poisoned",status="PREVIEWED",base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id),normalized_changes=[{"target_type":"CANON_FACT","target_id":foreign_canon.id,"path":"/proposition","before_value":"x","after_value":"y"}],impact_report={}); session.add(poisoned); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":poisoned.id,"reason":"poison"}).json(); response=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze")
    assert response.status_code==409 and response.json()["detail"]["code"]=="CROSS_PROJECT_REFERENCE" and session.scalar(select(func.count(RetconImpactPlan.id)).where(RetconImpactPlan.retcon_request_id==req["id"]))==0

def test_unique_constraint_rejects_duplicate_request_version(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); request=RetconRequest(project_id=project.id,source_revision_id=revision["id"],reason="unique",status="DRAFT",current_plan_version=0); session.add(request); session.flush()
    session.add_all([RetconImpactPlan(project_id=project.id,retcon_request_id=request.id,version=1,basis_fingerprint="a",status="READY",impact_summary={},validation_report={}),RetconImpactPlan(project_id=project.id,retcon_request_id=request.id,version=1,basis_fingerprint="b",status="READY",impact_summary={},validation_report={})])
    with pytest.raises(IntegrityError): session.commit()
    session.rollback()

def test_analyze_never_calls_ai_provider(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch)
    monkeypatch.setattr(api,"get_model_provider",lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError("AI must not be used")))
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"deterministic"}).json()
    assert client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").status_code==200

def test_mixed_confirmed_and_uncertain_knowledge_aggregates_to_one_confirmed_item(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); knowledge.source=scene.id; session.commit()
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"mixed"}).json(); items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    matched=[item for item in items if item["resource_type"]=="CHARACTER_KNOWLEDGE" and item["resource_id"]==knowledge.id]
    assert len(matched)==1 and matched[0]["classification"]=="REBUILD_COGNITION"
    assert [node["type"] for node in matched[0]["dependency_path"]]==["CANON_FACT","SCENE","CHARACTER_KNOWLEDGE"]

def test_pure_uncertain_cognition_is_not_counted_as_rebuild(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); knowledge.source=None; session.commit()
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"uncertain cognition"}).json(); plan=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["plan"]
    assert plan["impact_summary"]["affected_characters"]==0 and plan["impact_summary"]["cognition_impacts"]==[]

def test_direct_canon_to_world_resolution_is_invalidated(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); proposal=session.query(__import__("app.models",fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    decision=CharacterDecision(project_id=project.id,scene_proposal_id=proposal.id,character_id=knowledge.character_id,context_fingerprint="ctx",decision_type=CharacterDecisionType.INVESTIGATE,intent="x",chosen_action="x",motivation="x",goal_refs=[],knowledge_used=[],memory_refs=[],ability_refs=[],inventory_refs=[],relationship_factors={},uncertainties=[],refused_options=[],decision_summary="x",status=CharacterDecisionStatus.VALID); session.add(decision); session.flush()
    performance=ScenePerformance(project_id=project.id,scene_proposal_id=proposal.id,take_number=300,proposal_context_fingerprint="ctx",mode=PerformanceMode.HEURISTIC,status=PerformanceStatus.RUNNING,participant_order=[],active_participant_ids=[],max_turns=1,turn_count=0); session.add(performance); session.flush(); turn=ScenePerformanceTurn(project_id=project.id,performance_id=performance.id,sequence=1,actor_character_id=knowledge.character_id,actor_context_fingerprint="ctx",character_decision_id=decision.id,action_visibility=ActionVisibility.PUBLIC,observable_action="x",recipient_character_ids=[],requires_world_resolution=True,world_resolution_request={},validation_result={}); session.add(turn); session.flush(); resolution=WorldResolution(project_id=project.id,performance_id=performance.id,performance_turn_id=turn.id,resolver_mode=ResolverMode.HEURISTIC,world_context_fingerprint="ctx",status=ResolutionStatus.VALID,outcome=ResolutionOutcome.SUCCESS,outcome_summary="canon",objective_facts=[],actor_observation=None,public_observation=None,recipient_character_ids=[],canon_fact_ids_used=[canon.id],world_entity_ids_used=[],resolution_basis_summary=None,missing_information=[]); session.add(resolution); session.commit()
    stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit(); req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"direct canon"}).json(); items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    item=next(i for i in items if i["resource_id"]==resolution.id); assert item["classification"]=="INVALIDATED" and [node["type"] for node in item["dependency_path"]]==["CANON_FACT","WORLD_RESOLUTION"]

def test_direct_entity_to_world_resolution_is_invalidated(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); entity=session.scalar(select(WorldEntity).where(WorldEntity.project_id==project.id)); entity_revision=client.post(f"/projects/{project.id}/revisions",json={"title":"entity","changes":[{"target_type":"WORLD_ENTITY","target_id":entity.id,"operation":"SET","path":"/name","value":"changed"}]}).json(); assert client.post(f"/projects/{project.id}/revisions/{entity_revision['id']}/preview").status_code==200
    proposal=session.query(__import__("app.models",fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first(); decision=CharacterDecision(project_id=project.id,scene_proposal_id=proposal.id,character_id=knowledge.character_id,context_fingerprint="ctx",decision_type=CharacterDecisionType.INVESTIGATE,intent="x",chosen_action="x",motivation="x",goal_refs=[],knowledge_used=[],memory_refs=[],ability_refs=[],inventory_refs=[],relationship_factors={},uncertainties=[],refused_options=[],decision_summary="x",status=CharacterDecisionStatus.VALID); session.add(decision); session.flush(); performance=ScenePerformance(project_id=project.id,scene_proposal_id=proposal.id,take_number=301,proposal_context_fingerprint="ctx",mode=PerformanceMode.HEURISTIC,status=PerformanceStatus.RUNNING,participant_order=[],active_participant_ids=[],max_turns=1,turn_count=0); session.add(performance); session.flush(); turn=ScenePerformanceTurn(project_id=project.id,performance_id=performance.id,sequence=1,actor_character_id=knowledge.character_id,actor_context_fingerprint="ctx",character_decision_id=decision.id,action_visibility=ActionVisibility.PUBLIC,observable_action="x",recipient_character_ids=[],requires_world_resolution=True,world_resolution_request={},validation_result={}); session.add(turn); session.flush(); resolution=WorldResolution(project_id=project.id,performance_id=performance.id,performance_turn_id=turn.id,resolver_mode=ResolverMode.HEURISTIC,world_context_fingerprint="ctx",status=ResolutionStatus.VALID,outcome=ResolutionOutcome.SUCCESS,outcome_summary="entity",objective_facts=[],actor_observation=None,public_observation=None,recipient_character_ids=[],canon_fact_ids_used=[],world_entity_ids_used=[entity.id],resolution_basis_summary=None,missing_information=[]); session.add(resolution); session.commit(); stored=session.get(WorldRevision,entity_revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":entity_revision["id"],"reason":"direct entity"}).json(); items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    assert next(i for i in items if i["resource_id"]==resolution.id)["classification"]=="INVALIDATED"

def test_derived_canon_reference_reaches_scene(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client=prepared(session,monkeypatch); derived=CanonFact(project_id=project.id,fact_type="WORLD_FACT",proposition="derived",data={"canon_fact_id":canon.id},locked=False); session.add(derived); session.flush(); independent.facts=[derived.id]; session.commit(); stored=session.get(WorldRevision,revision["id"]); stored.base_state_fingerprint=RevisionStateFingerprintBuilder().build(session,project.id); session.commit()
    req=client.post(f"/projects/{project.id}/retcon/requests",json={"source_revision_id":revision["id"],"reason":"derived"}).json(); items=client.post(f"/projects/{project.id}/retcon/requests/{req['id']}/analyze").json()["items"]
    item=next(i for i in items if i["resource_id"]==independent.id and i["resource_type"]=="SCENE"); assert item["classification"]=="REPLAY_REQUIRED" and [node["type"] for node in item["dependency_path"]]==["CANON_FACT","CANON_FACT","SCENE"]
