import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base
from app.main import app
import app.api as api
from app.models import RetconReplaySession, ReplaySceneRun, RetconApplication, RetconApplicationStatus, Scene, WorldSnapshot, SceneStateCheckpoint, Character, CharacterDecision, CharacterDecisionType, CharacterDecisionStatus, ScenePerformance, ScenePerformanceTurn, SceneExecutionBinding, PerformanceMode, PerformanceStatus, ActionVisibility, WorldEntity, WorldResolution, ResolverMode, ResolutionStatus, ResolutionOutcome
from app.historical import SceneStateCheckpointService
from app.historical import TemporalCharacterCognitionReader
from app.replay import ReplayService
from app.character_mind import ActiveCharacterCognitionReader
from app.models import RetconCognitionInvalidation, RetconCognitionInvalidationStatus, CharacterKnowledge, CharacterMemory
from app.replay import ReplayWorldView, ReplayCognitionReplacementMatcher, PreservedSceneValidator, ReplayResourceMapper
from test_retcon_apply import analyzed_setup, apply_success, client_for

@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db

def replay_ready(session, monkeypatch):
    values = apply_success(session, monkeypatch)
    project, canon, knowledge, scene, revision, request, plan, client, applied = values
    knowledge.source = scene.id
    pre = SceneStateCheckpointService().capture_pre(session, project.id, scene.id)
    session.flush()
    SceneStateCheckpointService().finalize(session, project.id, scene.id, pre.id)
    session.commit()
    return values + (applied["application"]["id"],)

def historical_replay_world(session, monkeypatch):
    """A real historical timeline: PRE -> old execution -> POST -> future -> retcon."""
    from test_retcon_planning import prepared
    project, canon, knowledge, scene, _independent, revision, client = prepared(session, monkeypatch)
    actor = session.get(Character, knowledge.character_id)
    other = session.scalar(select(Character).where(Character.project_id == project.id, Character.id != actor.id))
    scene.participants = [actor.id, other.id]
    location_id = session.query(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first().location_id
    location = session.get(WorldEntity, location_id); location.profile = {**(location.profile or {}), "locked": True}
    actor.current_state = {"location_id": location_id}; actor.inventory = []; actor.relationships = {other.id: {"trust": 0.2}}; actor.physical_state = {"condition": "healthy"}; actor.emotional_state = {"mood": "calm"}; knowledge.source = scene.id
    pre = SceneStateCheckpointService().capture_pre(session, project.id, scene.id)
    proposal = session.query(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal).filter_by(project_id=project.id).first()
    proposal.entry_state = {"world_affordances": [{"kind": "INTERACT", "description": "try the locked door", "target_entity_id": location_id, "target_character_id": None}]}
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint="historical", decision_type=CharacterDecisionType.OBSERVE, intent="observe", chosen_action="observe", target_character_id=None, target_entity_id=None, motivation="historical", goal_refs=[], knowledge_used=[{"knowledge_id": knowledge.id, "proposition": knowledge.proposition}], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], boundary_override_reason=None, decision_summary="historical", status=CharacterDecisionStatus.VALID)
    performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=77, proposal_context_fingerprint="historical", mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.COMPLETED, participant_order=[actor.id], active_participant_ids=[actor.id], max_turns=1, turn_count=1)
    session.add_all([decision, performance]); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=performance.id, sequence=1, actor_character_id=actor.id, actor_context_fingerprint="historical", character_decision_id=decision.id, action_visibility=ActionVisibility.PRIVATE, observable_action="observe", spoken_content=None, recipient_character_ids=[], requires_world_resolution=False, world_resolution_request=None, validation_result={"valid": True})
    session.add(turn); session.flush(); session.add(SceneExecutionBinding(project_id=project.id, scene_id=scene.id, performance_id=performance.id, active=True)); session.flush()
    SceneStateCheckpointService().finalize(session, project.id, scene.id, pre.id)
    actor.current_state = {"location_id": "future-location"}; actor.inventory = ["future-key"]; actor.relationships = {other.id: {"trust": 0.9}}; actor.physical_state = {"condition": "wounded"}; actor.emotional_state = {"mood": "fearful"}
    session.add(CharacterKnowledge(character_id=actor.id, proposition="future secret", status="KNOWN", source="future-scene")); session.add(CharacterMemory(character_id=actor.id, content="future memory", source_scene="future-scene")); session.commit()
    # The revision is authored after the future timeline exists, exactly as a
    # real retcon request is; the replay baseline still comes from Scene PRE.
    revision = client.post(f"/projects/{project.id}/revisions", json={"title": "historical retcon", "changes": [{"target_type": "CANON_FACT", "target_id": canon.id, "operation": "SET", "path": "/proposition", "value": "new location truth"}]}).json()
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").status_code == 200
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id": revision["id"], "reason": "historical fixture"}).json()
    analyzed = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed.json()["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "historical fixture"})
    assert applied.status_code == 200, applied.text
    return project, scene, actor, proposal, client, applied.json()["application"]["id"]

def test_create_replay_session_requires_pending_application(session, monkeypatch):
    project, *_unused, revision, client = __import__("test_retcon_planning", fromlist=["prepared"]).prepared(session, monkeypatch)
    result = client.post(f"/projects/{project.id}/retcon/applications/not-an-application/replay-sessions")
    assert result.status_code == 409

def test_historical_fixture_replay_context_excludes_future_character_state(session, monkeypatch):
    from app.replay import ReplayCharacterContextBuilder
    project, scene, actor, proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    context = ReplayCharacterContextBuilder().build(session, session.get(RetconReplaySession, created.json()["id"]), scene, proposal, actor.id)
    assert context["character"]["current_state"]["location_id"] != "future-location"
    assert context["inventory"] == []
    other_id = next(character.id for character in session.scalars(select(Character)).all() if character.id != actor.id and character.project_id == project.id)
    assert context["character"]["relationships"][other_id]["trust"] == 0.2
    assert context["character"]["physical_state"] == {"condition": "healthy"}
    assert context["character"]["emotional_state"] == {"mood": "calm"}
    assert all(row.proposition != "future secret" for row in context["knowledge"]["KNOWN"])
    assert all(row["content"] != "future memory" for row in context["memories"])

def test_historical_execution_atomic_failure_rolls_back_all_formal_rows(session, monkeypatch):
    project, scene, _actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    while True:
        state = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
        if state["cursor"] >= len(state["queue"]): break
        stepped = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step")
        assert stepped.status_code == 200, stepped.text
    staged_results = session.get(RetconReplaySession, session_id).staged_world_state["scene_results"]
    assert any(result["resolutions"] for result in staged_results.values())
    counts = {model: session.query(model).count() for model in (Scene, ScenePerformance, CharacterDecision, ScenePerformanceTurn, WorldResolution, CharacterKnowledge, CharacterMemory, SceneExecutionBinding)}
    old_binding = session.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
    monkeypatch.setattr(ReplayService, "failure_injector", lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_REPLAY_COMMIT_FAILURE")) if stage == "AFTER_FORMAL_MATERIALIZATION" else None)
    response = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "REPLAY_COMMIT_FAILED"
    session.expire_all()
    assert {model: session.query(model).count() for model in counts} == counts
    assert session.get(Scene, scene.id).history_status == "ACTIVE"
    assert session.get(SceneExecutionBinding, old_binding.id).active is True
    assert session.query(WorldSnapshot).filter(WorldSnapshot.snapshot_type.in_(["PRE_REPLAY_COMMIT", "POST_REPLAY_COMMIT"])).count() == 0

def test_failed_commit_rolls_back_staged_dynamic_cognition_invalidation(session, monkeypatch):
    project, scene, actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    dynamic_memory = CharacterMemory(character_id=actor.id, content="dynamic rollback evidence", source_scene=scene.id)
    session.add(dynamic_memory); session.commit()
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    while True:
        state = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
        if state["cursor"] >= len(state["queue"]): break
        assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step").status_code == 200
    replay_session = session.get(RetconReplaySession, session_id)
    staged = dict(replay_session.staged_world_state)
    staged["dynamic_cognition_invalidations"] = [{"resource_type": "MEMORY", "resource_id": dynamic_memory.id, "character_id": actor.id, "scene_id": scene.id, "sequence": scene.sequence, "fingerprint": "dynamic-rollback-test"}]
    replay_session.staged_world_state = staged
    session.commit()
    counts = {model: session.query(model).count() for model in (Scene, ScenePerformance, CharacterDecision, ScenePerformanceTurn, WorldResolution, CharacterKnowledge, CharacterMemory, SceneExecutionBinding, RetconCognitionInvalidation)}
    monkeypatch.setattr(ReplayService, "failure_injector", lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_REPLAY_COMMIT_FAILURE")) if stage == "AFTER_FORMAL_MATERIALIZATION" else None)
    response = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "REPLAY_COMMIT_FAILED"
    session.expire_all()
    assert {model: session.query(model).count() for model in counts} == counts
    assert session.query(RetconCognitionInvalidation).filter(RetconCognitionInvalidation.reason == "DYNAMIC_REPLAY_EXPANSION").count() == 0
    assert session.query(WorldSnapshot).filter(WorldSnapshot.snapshot_type.in_(["PRE_REPLAY_COMMIT", "POST_REPLAY_COMMIT"])).count() == 0

def test_historical_execution_commit_materializes_replay_lineage(session, monkeypatch):
    project, scene, _actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    while True:
        state = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
        if state["cursor"] >= len(state["queue"]): break
        assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step").status_code == 200
    committed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert committed.status_code == 200, committed.text
    session.expire_all(); old = session.get(Scene, scene.id); replacement = session.get(Scene, old.superseded_by_scene_id)
    assert old.history_status == "SUPERSEDED" and replacement.history_status == "ACTIVE"
    new_decision = session.scalar(select(CharacterDecision).where(CharacterDecision.replay_session_id == session_id))
    new_turn = session.scalar(select(ScenePerformanceTurn).where(ScenePerformanceTurn.replay_session_id == session_id))
    assert new_decision and new_turn and new_turn.replay_of_id and new_turn.character_decision_id == new_decision.id
    assert session.get(RetconApplication, application_id).status == RetconApplicationStatus.REPLAY_COMPLETED
    assert session.get(RetconReplaySession, session_id).status == "COMPLETED"

def test_dynamic_expansion_promotes_execution_and_quarantines_scene_cognition(session, monkeypatch):
    project, scene2, actor, proposal, client, application_id = historical_replay_world(session, monkeypatch)
    scene3 = session.scalar(select(Scene).where(Scene.project_id == project.id, Scene.sequence == scene2.sequence + 1))
    scene3.participants = [actor.id]
    proposal.entry_state = {"world_affordances": [{"kind": "INTERACT", "description": "try the locked door", "target_entity_id": proposal.location_id, "target_character_id": None}], "replay_prerequisites": {"required_entity_facts": [{"entity_id": proposal.location_id, "predicate": "locked", "expected": False}]}}
    old_decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint="scene3-old", decision_type=CharacterDecisionType.OBSERVE, intent="observe", chosen_action="observe", target_character_id=None, target_entity_id=None, motivation="historical", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, perceived_risk=None, accepted_cost=None, expected_personal_result=None, uncertainties=[], refused_options=[], boundary_override_reason=None, decision_summary="scene3 old", status=CharacterDecisionStatus.VALID)
    old_performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=78, proposal_context_fingerprint="scene3-old", mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.COMPLETED, participant_order=[actor.id], active_participant_ids=[actor.id], max_turns=1, turn_count=1)
    session.add_all([old_decision, old_performance]); session.flush()
    old_turn = ScenePerformanceTurn(project_id=project.id, performance_id=old_performance.id, sequence=1, actor_character_id=actor.id, actor_context_fingerprint="scene3-old", character_decision_id=old_decision.id, action_visibility=ActionVisibility.PRIVATE, observable_action="observe", spoken_content=None, recipient_character_ids=[], requires_world_resolution=True, world_resolution_request={"kind":"INTERACT","description":"old","target_entity_id":proposal.location_id,"target_character_id":None}, validation_result={"valid": True})
    session.add(old_turn); session.flush()
    old_resolution = WorldResolution(project_id=project.id, performance_id=old_performance.id, performance_turn_id=old_turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint="scene3-old", status=ResolutionStatus.VALID, outcome=ResolutionOutcome.SUCCESS, outcome_summary="old success", objective_facts=[], actor_observation=None, public_observation=None, recipient_character_ids=[], canon_fact_ids_used=[], world_entity_ids_used=[proposal.location_id], resolution_basis_summary=None, missing_information=[])
    old_knowledge = CharacterKnowledge(character_id=actor.id, proposition=f"ENTITY {proposal.location_id}: locked = true", status="KNOWN", source=scene3.id)
    old_memory = CharacterMemory(character_id=actor.id, content="old scene3 observation", source_scene=scene3.id)
    session.add_all([old_resolution, old_knowledge, old_memory]); session.flush(); old_binding = SceneExecutionBinding(project_id=project.id, scene_id=scene3.id, performance_id=old_performance.id, active=True); session.add(old_binding); session.commit()

    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    first_step = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step")
    assert first_step.status_code == 200, first_step.text
    escalated = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step")
    assert escalated.status_code == 200 and escalated.json()["runs"][-1]["validation_report"]["result"] == "REPLAY_ESCALATED"
    replay_session = session.get(RetconReplaySession, session_id); promoted = replay_session.queue[replay_session.cursor]
    assert promoted["mode"] == "REPLAY" and promoted["reason"] == "DYNAMIC_EXPANSION"
    assert promoted["decision_ids"] == [old_decision.id] and promoted["turn_ids"] == [old_turn.id] and promoted["resolution_ids"] == [old_resolution.id]
    assert replay_session.failure_report is None and replay_session.staged_world_state["dynamic_cognition_invalidations"]
    assert session.query(RetconCognitionInvalidation).filter(RetconCognitionInvalidation.reason == "DYNAMIC_REPLAY_EXPANSION").count() == 0
    replayed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step")
    assert replayed.status_code == 200 and replayed.json()["status"] == "RUNNING", replayed.text
    session.expire_all()
    staged = session.get(RetconReplaySession, session_id).staged_world_state["scene_results"][scene3.id]
    assert len(staged["decisions"]) == len(staged["turns"]) == len(staged["resolutions"]) == 1
    committed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert committed.status_code == 200, committed.text
    session.expire_all(); old_scene3 = session.get(Scene, scene3.id); new_scene3 = session.get(Scene, old_scene3.superseded_by_scene_id)
    assert old_scene3.history_status == "SUPERSEDED" and new_scene3.history_status == "ACTIVE" and session.get(SceneExecutionBinding, old_binding.id).active is False
    assert session.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == new_scene3.id, SceneExecutionBinding.active.is_(True))) is not None
    assert old_knowledge.id not in {row.id for row in ActiveCharacterCognitionReader().knowledge(session, project.id, actor.id)}
    assert old_memory.id not in {row.id for row in ActiveCharacterCognitionReader().memories(session, project.id, actor.id)}
    knowledge_invalidation = session.scalar(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.resource_id == old_knowledge.id))
    memory_invalidation = session.scalar(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.resource_id == old_memory.id))
    assert knowledge_invalidation.status == RetconCognitionInvalidationStatus.RESOLVED
    assert knowledge_invalidation.resolution_report["result"] == "REPLACED"
    assert knowledge_invalidation.resolution_report["replacement_resource_id"]
    assert memory_invalidation.status == RetconCognitionInvalidationStatus.RESOLVED
    assert memory_invalidation.resolution_report["result"] == "INVALIDATED_WITHOUT_REPLACEMENT"

def test_replay_session_initial_queue_is_frozen_and_deterministic(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    first = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "READY"
    assert body["queue"] == sorted(body["queue"], key=lambda item: (item["sequence"], item["scene_id"]))
    duplicate = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "REPLAY_SESSION_ALREADY_EXISTS"

def test_replay_step_does_not_modify_old_scene_and_requires_explicit_commit(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, _, client, _, application_id = values
    session_body = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    session_id = session_body["id"]
    before = (scene.summary, scene.facts, scene.history_status)
    stepped = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step")
    assert stepped.status_code == 200, stepped.text
    session.expire_all(); old = session.get(Scene, scene.id)
    assert (old.summary, old.facts, old.history_status) == before
    incomplete = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert incomplete.status_code == 409

def test_replay_commit_switches_current_scene_and_resolves_application(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, _, client, _, application_id = values
    session_id = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    queue = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()["queue"]
    while True:
        state = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
        if state["cursor"] >= len(queue): break
        step = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step")
        assert step.status_code == 200, step.text
    committed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert committed.status_code == 200, committed.text
    assert committed.json()["status"] == "COMPLETED"
    session.expire_all(); old = session.get(Scene, scene.id)
    assert old.history_status == "SUPERSEDED" and old.superseded_by_scene_id
    application = session.get(RetconApplication, application_id)
    assert application.status == "REPLAY_COMPLETED"

def test_replay_commit_requires_confirmation(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    session_id = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    result = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "EXPLICIT_CONFIRMATION_REQUIRED"

def test_replay_endpoint_without_session_context_does_not_exist(session, monkeypatch):
    project, *_unused, revision, client = __import__("test_retcon_planning", fromlist=["prepared"]).prepared(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/retcon/replay").status_code == 404

def test_missing_historical_checkpoint_blocks_session(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, _, _, client, applied = values
    result = client.post(f"/projects/{project.id}/retcon/applications/{applied['application']['id']}/replay-sessions")
    assert result.status_code == 409 and result.json()["detail"]["code"] == "HISTORICAL_BASELINE_UNAVAILABLE"

def test_checkpoint_is_two_phase_and_legacy_protocol_is_rejected(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, scene, _, _, _, client, applied = values
    pre = SceneStateCheckpointService().capture_pre(session, project.id, scene.id)
    assert session.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.scene_id == scene.id)) is None
    checkpoint = SceneStateCheckpointService().finalize(session, project.id, scene.id, pre.id); session.commit()
    assert checkpoint.capture_protocol_version == 2 and checkpoint.pre_snapshot_id != checkpoint.post_snapshot_id
    checkpoint.capture_protocol_version = 1; session.commit()
    blocked = client.post(f"/projects/{project.id}/retcon/applications/{applied['application']['id']}/replay-sessions")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "HISTORICAL_BASELINE_UNAVAILABLE"

def test_replay_baseline_overlays_retcon_target_without_future_world_state(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, canon, _, scene, revision, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert replay.status_code == 201, replay.text
    state = session.get(RetconReplaySession, replay.json()["id"]).staged_world_state
    baseline_canon = next(row for row in state["baseline"]["canon_facts"] if row["id"] == canon.id)
    assert baseline_canon["proposition"] == revision["change_set"][0]["value"]

def test_replay_abort_cleans_staging_and_allows_retcon_rollback(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    session_id = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/abort").json()["status"] == "ABORTED"
    assert client.post(f"/projects/{project.id}/retcon/applications/{application_id}/rollback").status_code == 200

def test_active_replay_blocks_retcon_rollback(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    result = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/rollback")
    assert result.status_code == 409 and result.json()["detail"]["code"] == "REPLAY_SESSION_ACTIVE"

def test_temporal_reader_excludes_invalidated_cognition(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, _, _, _, _, client, _, application_id = values
    session_id = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    replay_session = session.get(RetconReplaySession, session_id)
    result = TemporalCharacterCognitionReader().read(session, project.id, knowledge.character_id, replay_session, 1)
    assert knowledge.id not in {row.id for row in result["knowledge"]}

def test_resolved_invalidation_keeps_old_cognition_hidden(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, knowledge, _, _, _, _, client, applied = values
    invalidation = session.scalar(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.resource_id == knowledge.id)); invalidation.status = RetconCognitionInvalidationStatus.RESOLVED; session.commit()
    assert knowledge.id not in {row.id for row in ActiveCharacterCognitionReader().knowledge(session, project.id, knowledge.character_id)}

def test_replay_status_ready_before_first_step(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    body = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    assert body["status"] == "READY" and body["cursor"] == 0

def test_replay_queue_ignores_superseded_scene(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, _, client, _, application_id = values
    scene.history_status = "SUPERSEDED"; session.commit()
    result = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    # A superseded scene is excluded from the active queue; affected cognition
    # without another exact active replay coverage must fail closed.
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "COGNITION_REPLAY_COVERAGE_UNRESOLVED"

def test_commit_failure_hook_rolls_back_real_api(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, _, client, _, application_id = values
    session_id = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    while True:
        body = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
        if body["cursor"] >= len(body["queue"]): break
        assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step").status_code == 200
    stages = []
    def inject(stage):
        stages.append(stage)
        if stage == "AFTER_FORMAL_MATERIALIZATION":
            raise RuntimeError("TEST_REPLAY_COMMIT_FAILURE")
    monkeypatch.setattr(ReplayService, "failure_injector", inject)
    failed = client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation": True})
    assert failed.status_code == 409 and failed.json()["detail"]["code"] == "REPLAY_COMMIT_FAILED"
    assert stages == ["AFTER_FORMAL_MATERIALIZATION"]
    session.expire_all(); assert session.get(Scene, scene.id).history_status == "ACTIVE"

def test_replay_world_view_reads_historical_character_state(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    replay_session = session.get(RetconReplaySession, replay["id"])
    character = replay_session.staged_world_state["current_world"]["characters"][0]
    character["current_state"] = {"location_id": "historical"}; replay_session.staged_world_state = replay_session.staged_world_state
    assert ReplayWorldView(replay_session).character(character["id"])["current_state"]["location_id"] == "historical"

def test_replay_world_view_reads_baseline_entity_fact(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    replay_session = session.get(RetconReplaySession, replay["id"]); entity = replay_session.staged_world_state["current_world"]["world_entities"][0]
    assert ReplayWorldView(replay_session).fact("ENTITY", entity["id"], "locked") == (entity.get("profile") or {}).get("locked")

def test_replay_world_view_latest_staged_fact_wins(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); replay_session = session.get(RetconReplaySession, replay["id"])
    entity = replay_session.staged_world_state["current_world"]["world_entities"][0]; state = dict(replay_session.staged_world_state); state["staged_facts"] = [{"subject_type":"ENTITY","subject_id":entity["id"],"predicate":"locked","value":False},{"subject_type":"ENTITY","subject_id":entity["id"],"predicate":"locked","value":True}]; state["current_world"] = dict(state["current_world"], staged_facts=state["staged_facts"]); replay_session.staged_world_state = state
    assert ReplayWorldView(replay_session).fact("ENTITY", entity["id"], "locked") is True
    assert ReplayWorldView(replay_session).entity(entity["id"])["profile"]["locked"] is True

def test_temporal_reader_adds_only_prior_staged_cognition(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, scene, _, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); replay_session = session.get(RetconReplaySession, replay["id"])
    state = dict(replay_session.staged_world_state); state["staged_cognition"] = {"knowledge":[{"temp_id":"prior","character_id":knowledge.character_id,"status":"SUSPECTED","proposition":"prior","confidence":0.3,"source_sequence":scene.sequence-1}],"memories":[]}; replay_session.staged_world_state = state
    rows = TemporalCharacterCognitionReader().read(session, project.id, knowledge.character_id, replay_session, scene.sequence)
    assert any(row.id == "prior" and row.status == "SUSPECTED" for row in rows["knowledge"])

def test_temporal_reader_excludes_future_staged_cognition(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, scene, _, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); replay_session = session.get(RetconReplaySession, replay["id"])
    state = dict(replay_session.staged_world_state); state["staged_cognition"] = {"knowledge":[{"temp_id":"future","character_id":knowledge.character_id,"status":"KNOWN","proposition":"future","confidence":1.0,"source_sequence":scene.sequence+1}],"memories":[]}; replay_session.staged_world_state = state
    rows = TemporalCharacterCognitionReader().read(session, project.id, knowledge.character_id, replay_session, scene.sequence)
    assert all(row.id != "future" for row in rows["knowledge"])

def test_replay_context_preserves_epistemic_statuses(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, scene, _, _, _, client, _, application_id = values
    replay = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); replay_session = session.get(RetconReplaySession, replay["id"])
    state = dict(replay_session.staged_world_state); state["staged_cognition"] = {"knowledge":[{"temp_id":"suspected","character_id":knowledge.character_id,"status":"SUSPECTED","proposition":"suspected","confidence":0.4},{"temp_id":"false","character_id":knowledge.character_id,"status":"FALSE_BELIEF","proposition":"false","confidence":0.1}],"memories":[]}; replay_session.staged_world_state = state
    rows = TemporalCharacterCognitionReader().read(session, project.id, knowledge.character_id, replay_session, scene.sequence+1)
    assert {row.status for row in rows["knowledge"] if row.id in {"suspected","false"}} == {"SUSPECTED","FALSE_BELIEF"}

def test_replacement_matcher_requires_structured_fact_identity(session):
    old = CharacterKnowledge(id="old", character_id="char", proposition='ENTITY door: locked = true', status="KNOWN", source="scene")
    candidate = {"character_id":"char", "fact_identity":{"subject_type":"ENTITY","subject_id":"door","predicate":"locked","value":True}}
    assert ReplayCognitionReplacementMatcher().knowledge(old, candidate, "scene") is True

def test_replacement_matcher_rejects_different_fact(session):
    old = CharacterKnowledge(id="old", character_id="char", proposition='ENTITY door: locked = true', status="KNOWN", source="scene")
    candidate = {"character_id":"char", "fact_identity":{"subject_type":"ENTITY","subject_id":"door","predicate":"locked","value":False}}
    assert ReplayCognitionReplacementMatcher().knowledge(old, candidate, "scene") is False

def test_memory_matcher_never_uses_text_similarity(session):
    old = CharacterMemory(id="old", character_id="char", content="the door is locked", source_scene="scene")
    candidate = {"character_id":"char", "content":"the door is locked"}
    assert ReplayCognitionReplacementMatcher().memory(old, candidate, "scene") is False

def test_missing_cognition_lineage_fails_session_creation(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, scene, _, _, _, client, applied = values
    pre = SceneStateCheckpointService().capture_pre(session, project.id, scene.id); SceneStateCheckpointService().finalize(session, project.id, scene.id, pre.id); session.commit()
    result = client.post(f"/projects/{project.id}/retcon/applications/{applied['application']['id']}/replay-sessions")
    assert result.status_code == 409 and result.json()["detail"]["code"] == "COGNITION_REPLAY_COVERAGE_UNRESOLVED"

def test_checkpoint_has_no_row_until_finalize(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, scene, _, _, _, _, _ = values
    pre = SceneStateCheckpointService().capture_pre(session, project.id, scene.id); session.flush()
    assert session.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.scene_id == scene.id)) is None
    checkpoint = SceneStateCheckpointService().finalize(session, project.id, scene.id, pre.id)
    assert checkpoint.capture_protocol_version == 2 and checkpoint.pre_snapshot_id != checkpoint.post_snapshot_id

def test_commit_failure_leaves_no_snapshots_or_replacement_scene(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, _, client, _, application_id = values
    session_id = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    body = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
    while body["cursor"] < len(body["queue"]):
        assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/step").status_code == 200; body = client.get(f"/projects/{project.id}/retcon/replay-sessions/{session_id}").json()
    monkeypatch.setattr(ReplayService, "failure_injector", lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_REPLAY_COMMIT_FAILURE")) if stage == "AFTER_FORMAL_MATERIALIZATION" else None)
    assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{session_id}/commit", json={"explicit_confirmation":True}).status_code == 409
    session.expire_all(); assert session.query(Scene).filter(Scene.project_id == project.id, Scene.history_status == "STAGED").count() == 0
    assert session.query(WorldSnapshot).filter(WorldSnapshot.snapshot_type == "PRE_REPLAY_COMMIT").count() == 0

def test_plan_remains_consumed_after_replay_completed(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, analyzed, client, _, application_id = values
    sid = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]; queue = client.get(f"/projects/{project.id}/retcon/replay-sessions/{sid}").json()["queue"]
    while client.get(f"/projects/{project.id}/retcon/replay-sessions/{sid}").json()["cursor"] < len(queue): client.post(f"/projects/{project.id}/retcon/replay-sessions/{sid}/step")
    assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{sid}/commit", json={"explicit_confirmation":True}).status_code == 200
    plan = client.get(f"/projects/{project.id}/retcon/plans/{analyzed['plan']['id']}").json()["plan"]
    assert plan["consumed"] is True and plan["consumption_status"] == "REPLAY_COMPLETED" and plan["is_stale"] is False

def test_resource_mapper_uses_structured_source_scene(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, scene, _, _, _, _, _, application_id = values
    mapped = ReplayResourceMapper().map(session, session.get(RetconApplication, application_id), [scene.id])
    assert knowledge.id in mapped[scene.id]["knowledge_ids"]

@pytest.mark.parametrize("visibility,target,expected", [(ActionVisibility.PUBLIC, None, ["other"]),(ActionVisibility.PRIVATE, None, []),(ActionVisibility.COVERT, None, []),(ActionVisibility.TARGETED, "other", ["other"])])
def test_replay_observation_router_respects_visibility(visibility, target, expected):
    from app.performance import PerformanceObservationRouter
    assert PerformanceObservationRouter().recipients(visibility, ["actor", "other"], "actor", target) == expected

def test_replay_abort_does_not_change_formal_scene(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, scene, _, _, _, client, _, application_id = values
    sid = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    assert client.post(f"/projects/{project.id}/retcon/replay-sessions/{sid}/abort").status_code == 200
    session.expire_all(); assert session.get(Scene, scene.id).history_status == "ACTIVE"

def test_replay_abort_keeps_application_pending(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    sid = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    client.post(f"/projects/{project.id}/retcon/replay-sessions/{sid}/abort")
    assert session.get(RetconApplication, application_id).status == RetconApplicationStatus.APPLIED_PENDING_REPLAY

def test_replay_failure_does_not_resolve_cognition(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, _, _, _, _, client, _, application_id = values
    sid = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    while client.get(f"/projects/{project.id}/retcon/replay-sessions/{sid}").json()["cursor"] < 1: client.post(f"/projects/{project.id}/retcon/replay-sessions/{sid}/step")
    monkeypatch.setattr(ReplayService, "failure_injector", lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_REPLAY_COMMIT_FAILURE")) if stage == "AFTER_FORMAL_MATERIALIZATION" else None)
    client.post(f"/projects/{project.id}/retcon/replay-sessions/{sid}/commit", json={"explicit_confirmation":True})
    invalidation = session.scalar(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.resource_id == knowledge.id))
    assert invalidation.status == RetconCognitionInvalidationStatus.ACTIVE

def test_replay_session_current_fingerprint_changes_after_step(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    body = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); before = body["current_fingerprint"]
    body = client.post(f"/projects/{project.id}/retcon/replay-sessions/{body['id']}/step").json()
    assert body["current_fingerprint"] != before

def test_replay_queue_freezes_separate_cognition_lists(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, knowledge, _, _, _, _, client, _, application_id = values
    body = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); item = body["queue"][0]
    assert item["knowledge_ids"] == [knowledge.id] and "cognition_resource_ids" not in item

@pytest.mark.parametrize("predicate,value", [("locked", True),("locked", False),("opened", True),("opened", False),("temperature", 3),("state", "sealed")])
def test_replay_world_view_fact_overlay_variants(session, monkeypatch, predicate, value):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    body = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json(); replay_session = session.get(RetconReplaySession, body["id"]); entity = replay_session.staged_world_state["current_world"]["world_entities"][0]
    state = dict(replay_session.staged_world_state); state["staged_facts"] = [{"subject_type":"ENTITY","subject_id":entity["id"],"predicate":predicate,"value":value}]; state["current_world"] = dict(state["current_world"], staged_facts=state["staged_facts"]); replay_session.staged_world_state = state
    assert ReplayWorldView(replay_session).fact("ENTITY", entity["id"], predicate) == value

def test_replay_baseline_fingerprint_is_stable_for_same_session(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    first = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    assert first["baseline_fingerprint"] == session.get(RetconReplaySession, first["id"]).baseline_fingerprint

def test_replay_application_is_not_completed_before_commit(session, monkeypatch):
    values = replay_ready(session, monkeypatch); project, _, _, _, _, _, _, client, _, application_id = values
    sid = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()["id"]
    assert session.get(RetconApplication, application_id).status == RetconApplicationStatus.APPLIED_PENDING_REPLAY
    assert session.get(RetconReplaySession, sid).status == "READY"
