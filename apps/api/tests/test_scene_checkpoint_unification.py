"""Independent Phase 7D semantic tests for pure checkpoint boundary logic."""
from types import SimpleNamespace
from datetime import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.replay import PreservedSceneStateTransitionProjector, ReplayCheckpointFormalizer, ReplayCheckpointStateBuilder
from app.historical import snapshot_fingerprint
from app.historical import SceneCheckpointIntegrityValidator


def world():
    return {"project": {"id": "p", "current_world_time": "2040-01-01T00:00:00"}, "world_entities": [{"id": "door", "profile": {"color": "red", "locked": False}}], "characters": [{"id": "a", "inventory": []}], "character_knowledge": [], "character_memories": [], "canon_facts": [], "reveal_constraints": [], "story_threads": [], "story_arcs": [], "scenes": [], "chapters": []}

@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def historical_multiscene_replay(session, monkeypatch):
    from app.models import (
        ActionVisibility, CanonFact, CharacterDecision, CharacterDecisionStatus,
        CharacterDecisionType, CharacterKnowledge, CharacterMemory, PerformanceMode,
        PerformanceStatus, ProposalStatus, ResolutionOutcome, ResolutionStatus,
        ResolverMode, Scene, SceneExecutionBinding, ScenePerformance,
        ScenePerformanceTurn, SceneProposal, SceneStatus, WorldResolution,
    )
    from app.historical import SceneStateCheckpointService
    from test_scene_performance import approved_setup

    project, location, actor, other, base_proposal, client = approved_setup(session, monkeypatch)
    location.profile = {"openable": True, "opened": False, "locked": False}
    canon = CanonFact(project_id=project.id, fact_type="CORE_CANON", proposition="the archive is sealed", data={}, locked=True)
    session.add(canon); session.flush()

    def proposal(entry_state):
        row = SceneProposal(
            project_id=project.id, context_fingerprint=base_proposal.context_fingerprint,
            proposal_type=base_proposal.proposal_type, primary_thread_id=base_proposal.primary_thread_id,
            location_id=location.id, participants=[actor.id, other.id], scene_goal="Historical boundary",
            character_motivations={}, entry_state=entry_state, planned_pressure=None,
            expected_progress={}, allowed_reveals=[], forbidden_reveals=[], required_canon=[],
            possible_outcomes=[], new_entity_requests=[], risk_flags=[],
            director_reasoning_summary="Historical integration fixture", status=ProposalStatus.APPROVED,
        )
        session.add(row); session.flush(); return row

    def execution(scene, scene_proposal, knowledge, affected, take):
        decision = CharacterDecision(
            project_id=project.id, scene_proposal_id=scene_proposal.id, character_id=actor.id,
            context_fingerprint=f"historical-{scene.sequence}", decision_type=CharacterDecisionType.OBSERVE,
            intent="observe", chosen_action="observe", motivation="historical evidence",
            goal_refs=[], knowledge_used=[{"knowledge_id": knowledge.id, "proposition": knowledge.proposition}] if affected else [],
            memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, uncertainties=[],
            refused_options=[], decision_summary="historical", status=CharacterDecisionStatus.VALID,
        )
        performance = ScenePerformance(
            project_id=project.id, scene_proposal_id=scene_proposal.id, take_number=take,
            proposal_context_fingerprint=f"historical-{scene.sequence}", mode=PerformanceMode.HEURISTIC,
            status=PerformanceStatus.COMPLETED, participant_order=[actor.id, other.id],
            active_participant_ids=[actor.id, other.id], max_turns=1, turn_count=1,
        )
        session.add_all([decision, performance]); session.flush()
        turn = ScenePerformanceTurn(
            project_id=project.id, performance_id=performance.id, sequence=1,
            actor_character_id=actor.id, actor_context_fingerprint=f"historical-{scene.sequence}",
            character_decision_id=decision.id, action_visibility=ActionVisibility.PUBLIC,
            observable_action="observe", recipient_character_ids=[other.id], requires_world_resolution=True,
            world_resolution_request={"kind": "INTERACT" if affected else "INSPECT", "description": "historical", "target_entity_id": location.id, "target_character_id": None},
            validation_result={"valid": True},
        )
        session.add(turn); session.flush()
        resolution = WorldResolution(
            project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id,
            resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint=f"historical-{scene.sequence}",
            status=ResolutionStatus.VALID, outcome=ResolutionOutcome.SUCCESS,
            outcome_summary="old success", objective_facts=[], actor_observation="old observation",
            public_observation="old public observation", recipient_character_ids=[actor.id, other.id],
            canon_fact_ids_used=[canon.id] if affected else [], world_entity_ids_used=[location.id], missing_information=[],
        )
        session.add(resolution)
        session.add(SceneExecutionBinding(project_id=project.id, scene_id=scene.id, performance_id=performance.id, active=True))
        session.flush()
        return decision, turn, resolution

    scene2 = Scene(project_id=project.id, sequence=2, participants=[actor.id, other.id], facts=[canon.proposition], result={}, status=SceneStatus.OCCURRED)
    session.add(scene2); session.flush()
    historical_knowledge = CharacterKnowledge(character_id=actor.id, proposition=canon.proposition, status="KNOWN", source=scene2.id)
    session.add(historical_knowledge); session.flush()
    proposal2 = proposal({"world_affordances": [{"kind": "INTERACT", "description": "open", "target_entity_id": location.id, "target_character_id": None}]})
    pre2 = SceneStateCheckpointService().capture_pre(session, project.id, scene2.id)
    execution(scene2, proposal2, historical_knowledge, True, 201)
    SceneStateCheckpointService().finalize(session, project.id, scene2.id, pre2.id)

    scene3 = Scene(project_id=project.id, sequence=3, participants=[actor.id, other.id], facts=["preserved"], result={}, status=SceneStatus.OCCURRED)
    session.add(scene3); session.flush()
    proposal3 = proposal({})
    pre3 = SceneStateCheckpointService().capture_pre(session, project.id, scene3.id)
    decision3, turn3, resolution3 = execution(scene3, proposal3, historical_knowledge, False, 202)
    project.current_world_time = datetime(2040, 1, 2)
    knowledge3 = CharacterKnowledge(character_id=actor.id, proposition='SCENE preserved: marker = true', status="KNOWN", source=scene3.id)
    memory3 = CharacterMemory(character_id=actor.id, content="preserved scene memory", source_scene=scene3.id)
    session.add_all([knowledge3, memory3]); session.flush()
    SceneStateCheckpointService().finalize(session, project.id, scene3.id, pre3.id)

    scene4 = Scene(project_id=project.id, sequence=4, participants=[actor.id, other.id], facts=[canon.proposition], result={}, status=SceneStatus.OCCURRED)
    session.add(scene4); session.flush()
    proposal4 = proposal({})
    pre4 = SceneStateCheckpointService().capture_pre(session, project.id, scene4.id)
    execution(scene4, proposal4, historical_knowledge, True, 203)
    SceneStateCheckpointService().finalize(session, project.id, scene4.id, pre4.id)
    session.commit()

    revision = client.post(f"/projects/{project.id}/revisions", json={
        "title": "multiscene retcon",
        "changes": [{"target_type": "CANON_FACT", "target_id": canon.id, "operation": "SET", "path": "/proposition", "value": "the archive is open"}],
    }).json()
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").status_code == 200
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id": revision["id"], "reason": "continuity"}).json()
    analyzed = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={
        "plan_id": analyzed.json()["plan"]["id"], "explicit_confirmation": True,
        "author_override": True, "author_override_reason": "checkpoint integration",
    })
    assert applied.status_code == 200, applied.text
    return SimpleNamespace(
        project=project, actor=actor, scenes=(scene2, scene3, scene4),
        preserved_ids=(decision3.id, turn3.id, resolution3.id, knowledge3.id, memory3.id), client=client,
        application_id=applied.json()["application"]["id"],
    )

def project(pre, post, new=None, invalidated=()): return PreservedSceneStateTransitionProjector().project(pre, post, new or world(), invalidated)

def test_existing_nested_row_change_is_projected():
    old = world(); after = world(); after["world_entities"][0]["profile"]["locked"] = True; new = world(); assert project(old, after, new)["world_entities"][0]["profile"]["locked"] is True
def test_unrelated_replay_state_is_preserved():
    old = world(); after = world(); after["world_entities"][0]["profile"]["locked"] = True; new = world(); new["world_entities"][0]["profile"]["color"] = "blue"; out = project(old, after, new); assert out["world_entities"][0]["profile"] == {"color": "blue", "locked": True}
def test_added_scene_row_is_projected():
    old = world(); after = world(); after["scenes"] = [{"id": "s3", "status": "OCCURRED", "history_status": "ACTIVE"}]; assert project(old, after)["scenes"][0]["id"] == "s3"
def test_added_knowledge_row_is_projected():
    old = world(); after = world(); after["character_knowledge"] = [{"id": "k3", "source": "s3"}]; assert project(old, after)["character_knowledge"] == [{"id": "k3", "source": "s3"}]
def test_added_memory_row_is_projected():
    old = world(); after = world(); after["character_memories"] = [{"id": "m3", "source_scene": "s3"}]; assert project(old, after)["character_memories"] == [{"id": "m3", "source_scene": "s3"}]
def test_removed_row_is_removed():
    old = world(); old["characters"].append({"id": "removed"}); after = world(); assert not any(x["id"] == "removed" for x in project(old, after)["characters"])
def test_project_top_level_transition_is_projected():
    old = world(); after = world(); after["project"]["current_world_time"] = "2040-01-02T00:00:00"; assert project(old, after)["project"]["current_world_time"].endswith("02T00:00:00")
def test_project_unrelated_value_survives():
    old = world(); after = world(); after["project"]["current_world_time"] = "later"; new = world(); new["project"]["id"] = "p2"; assert project(old, after, new)["project"]["id"] == "p2"
def test_invalidated_added_knowledge_is_filtered():
    old = world(); after = world(); after["character_knowledge"] = [{"id": "k3"}]; assert not project(old, after, invalidated=("k3",))["character_knowledge"]
def test_invalidated_added_memory_is_filtered():
    old = world(); after = world(); after["character_memories"] = [{"id": "m3"}]; assert not project(old, after, invalidated=("m3",))["character_memories"]
def test_invalidated_cognition_already_in_new_pre_is_filtered():
    old = world(); after = world(); new = world(); new["character_knowledge"] = [{"id": "k-old"}]; new["character_memories"] = [{"id": "m-old"}]
    result = project(old, after, new, invalidated=("k-old", "m-old"))
    assert result["character_knowledge"] == [] and result["character_memories"] == []
def test_projection_does_not_mutate_inputs():
    old = world(); after = world(); new = world(); project(old, after, new); assert old["project"]["id"] == new["project"]["id"] == "p"
def test_projection_is_deterministic():
    old = world(); after = world(); after["world_entities"].append({"id": "z"}); assert project(old, after) == project(old, after)
def test_projection_preserves_canon_rows_not_touched():
    old = world(); old["canon_facts"] = [{"id": "c", "proposition": "x"}]; after = world(); after["canon_facts"] = old["canon_facts"]; new = world(); new["canon_facts"] = [{"id": "c", "proposition": "retcon"}]; assert project(old, after, new)["canon_facts"][0]["proposition"] == "retcon"
def test_projection_preserves_entity_added_by_replay():
    old = world(); after = world(); new = world(); new["world_entities"].append({"id": "replay"}); assert any(x["id"] == "replay" for x in project(old, after, new)["world_entities"])
def test_projection_handles_empty_tables():
    assert project({"project": {}}, {"project": {}}, {"project": {}, "scenes": []})["scenes"] == []

def test_checkpoint_payload_has_canonical_keys():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_facts": [], "staged_cognition": {}}); assert set(ReplayCheckpointStateBuilder().build(session)) == set(ReplayCheckpointStateBuilder.KEYS)
def test_checkpoint_fact_overlay_updates_entity_profile():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_facts": [{"subject_type": "ENTITY", "subject_id": "door", "predicate": "locked", "value": True}], "staged_cognition": {}}); assert ReplayCheckpointStateBuilder().build(session)["world_entities"][0]["profile"]["locked"] is True
def test_checkpoint_ignores_internal_queue():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "queue": ["secret"], "cursor": 4}); assert "queue" not in ReplayCheckpointStateBuilder().build(session)
def test_checkpoint_staged_knowledge_uses_temp_id():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_cognition": {"knowledge": [{"temp_id": "tmp-k", "character_id": "a"}]}}); assert ReplayCheckpointStateBuilder().build(session)["character_knowledge"][0]["id"] == "tmp-k"
def test_checkpoint_staged_memory_uses_temp_id():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_cognition": {"memories": [{"temp_id": "tmp-m", "character_id": "a"}]}}); assert ReplayCheckpointStateBuilder().build(session)["character_memories"][0]["id"] == "tmp-m"
def test_checkpoint_missing_world_uses_schema_defaults():
    session = SimpleNamespace(staged_world_state={}); payload = ReplayCheckpointStateBuilder().build(session); assert payload["scenes"] == [] and payload["project"] == {}
def test_checkpoint_builder_is_pure():
    state = {"current_world": world(), "staged_facts": []}; session = SimpleNamespace(staged_world_state=state); ReplayCheckpointStateBuilder().build(session); assert state == session.staged_world_state
def test_checkpoint_formalizer_is_pure_and_deterministic():
    from copy import deepcopy
    from app.models import Scene, SceneStatus
    payload = world(); payload["scenes"] = [{"id": "old", "project_id": "p", "sequence": 2, "history_status": "ACTIVE"}]
    payload["character_knowledge"] = [{"id": "replay-knowledge:1", "temp_id": "replay-knowledge:1"}]
    replacement = Scene(id="new", project_id="p", sequence=2, participants=[], facts=[], result={}, story_threads=[], status=SceneStatus.OCCURRED, history_status="ACTIVE")
    formal = {"id": "formal-k", "character_id": "a", "proposition": "x", "status": "KNOWN", "source": "new"}
    before = deepcopy(payload); formalizer = ReplayCheckpointFormalizer()
    first = formalizer.formalize(payload, 2, include_current=True, replacements={"old": (2, replacement)}, knowledge_by_temp={"replay-knowledge:1": formal}, memory_by_temp={})
    second = formalizer.formalize(payload, 2, include_current=True, replacements={"old": (2, replacement)}, knowledge_by_temp={"replay-knowledge:1": formal}, memory_by_temp={})
    assert payload == before and first == second
    assert [row["id"] for row in first["scenes"]] == ["new"] and first["character_knowledge"] == [formal]
def test_snapshot_fingerprint_ignores_mapping_order():
    assert snapshot_fingerprint({"b": 2, "a": 1}) == snapshot_fingerprint({"a": 1, "b": 2})
def test_snapshot_fingerprint_changes_state():
    assert snapshot_fingerprint({"a": 1}) != snapshot_fingerprint({"a": 2})
def test_snapshot_fingerprint_is_string():
    assert snapshot_fingerprint({"a": 1}).startswith("world-snapshot-v1:")

@pytest.mark.parametrize("predicate,value", [("locked", True), ("opened", True), ("color", "blue"), ("count", 2), ("security", {"alarm": False})])
def test_checkpoint_fact_overlay_preserves_structured_value(predicate, value):
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_facts": [{"subject_type": "ENTITY", "subject_id": "door", "predicate": predicate, "value": value}], "staged_cognition": {}})
    assert ReplayCheckpointStateBuilder().build(session)["world_entities"][0]["profile"][predicate] == value


def test_replay_checkpoint_final_integrity_uses_completed_session(session, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession
    from test_retcon_replay import historical_replay_world
    from app.models import Project, RetconReplaySession, ReplaySceneRun, SceneStateCheckpoint
    project, _scene, _actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    while created["cursor"] < len(created["queue"]):
        created = client.post(f"/projects/{project.id}/retcon/replay-sessions/{created['id']}/step").json()
    locked_projects = []
    original_scalar = OrmSession.scalar
    def tracking_scalar(db, statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", [])
        if getattr(statement, "_for_update_arg", None) is not None and any(item.get("entity") is Project for item in descriptions):
            locked_projects.append(project.id)
        return original_scalar(db, statement, *args, **kwargs)
    monkeypatch.setattr(OrmSession, "scalar", tracking_scalar)
    response = client.post(f"/projects/{project.id}/retcon/replay-sessions/{created['id']}/commit", json={"explicit_confirmation": True})
    assert response.status_code == 200, response.text
    assert locked_projects == [project.id]
    replay_session = session.get(RetconReplaySession, created["id"])
    assert replay_session.status == "COMPLETED"
    checkpoints = session.scalars(select(SceneStateCheckpoint).where(SceneStateCheckpoint.source_replay_session_id == replay_session.id)).all()
    assert checkpoints
    for checkpoint in checkpoints:
        post = session.get(__import__("app.models", fromlist=["WorldSnapshot"]).WorldSnapshot, checkpoint.post_snapshot_id)
        formal_scene = next((row for row in post.payload.get("scenes", []) if row.get("id") == checkpoint.scene_id), None)
        assert formal_scene and formal_scene.get("status") == "OCCURRED" and formal_scene.get("history_status") == "ACTIVE", formal_scene
        SceneCheckpointIntegrityValidator().validate_integrity(session, checkpoint)


def test_after_checkpoint_materialization_api_failure_rolls_back_all_rows(session, monkeypatch):
    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from app.replay import ReplayService
    from app.models import CharacterKnowledge, CharacterMemory, RetconApplication, RetconCognitionInvalidation, RetconReplaySession, Scene, SceneExecutionBinding, SceneStateCheckpoint, WorldSnapshot
    fixture = historical_multiscene_replay(session, monkeypatch)
    project, client, application_id = fixture.project, fixture.client, fixture.application_id
    scene2, scene3, scene4 = fixture.scenes
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions").json()
    while created["cursor"] < len(created["queue"]):
        created = client.post(f"/projects/{project.id}/retcon/replay-sessions/{created['id']}/step").json()
    models = (Scene, WorldSnapshot, SceneStateCheckpoint, SceneExecutionBinding, CharacterKnowledge, CharacterMemory)
    counts = {model: session.scalar(select(func.count(model.id))) for model in models}
    old_checkpoint = session.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.scene_id == scene3.id, SceneStateCheckpoint.active.is_(True)))
    old_checkpoint_state = (old_checkpoint.id, old_checkpoint.version, old_checkpoint.active)
    old_bindings = {row.scene_id: row.id for row in session.scalars(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id.in_([scene2.id, scene3.id, scene4.id]), SceneExecutionBinding.active.is_(True))).all()}
    invalidation_states = {row.id: row.status for row in session.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == application_id)).all()}
    monkeypatch.setattr(ReplayService, "failure_injector", staticmethod(lambda stage: (_ for _ in ()).throw(RuntimeError("TEST_CHECKPOINT_FAILURE")) if stage == "AFTER_CHECKPOINT_MATERIALIZATION" else None))
    response = client.post(f"/projects/{project.id}/retcon/replay-sessions/{created['id']}/commit", json={"explicit_confirmation": True})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "REPLAY_COMMIT_FAILED"
    with sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)() as fresh:
        assert {model: fresh.scalar(select(func.count(model.id))) for model in models} == counts
        checkpoint = fresh.get(SceneStateCheckpoint, old_checkpoint_state[0])
        assert (checkpoint.version, checkpoint.active) == old_checkpoint_state[1:]
        assert fresh.scalar(select(func.count(SceneStateCheckpoint.id)).where(SceneStateCheckpoint.scene_id == scene3.id)) == 1
        assert all(fresh.get(Scene, scene_id).history_status == "ACTIVE" for scene_id in (scene2.id, scene3.id, scene4.id))
        assert all(fresh.get(SceneExecutionBinding, binding_id).active is True for binding_id in old_bindings.values())
        assert {row.id: row.status for row in fresh.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == application_id)).all()} == invalidation_states
        assert fresh.get(RetconApplication, application_id).status == "APPLIED_PENDING_REPLAY"
        assert fresh.get(RetconReplaySession, created["id"]).status != "COMPLETED"


def test_sqlite_checkpoint_active_and_version_uniqueness(session):
    from sqlalchemy.exc import IntegrityError
    from app.models import Project, Scene, SceneStateCheckpoint, WorldSnapshot

    project = Project(name="checkpoint constraints"); session.add(project); session.flush()
    scene = Scene(project_id=project.id, sequence=1, facts=[], result={}); session.add(scene); session.flush()
    pre = WorldSnapshot(project_id=project.id, snapshot_type="PRE_SCENE_STATE", state_fingerprint="pre", payload={})
    post = WorldSnapshot(project_id=project.id, snapshot_type="POST_SCENE_STATE", state_fingerprint="post", payload={})
    session.add_all([pre, post]); session.flush()
    first = SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=1, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, version=1, active=True)
    session.add(first); session.commit()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=1, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, version=2, active=True))
        session.flush()
    first.active = False; session.flush()
    second = SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=1, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, version=2, active=True)
    session.add(second); session.commit()
    assert session.query(SceneStateCheckpoint).filter_by(scene_id=scene.id, active=True).count() == 1
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=1, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, version=2, active=False))
        session.flush()


def test_real_multiscene_replay_has_formal_continuous_boundaries(session, monkeypatch):
    import json
    from sqlalchemy import select
    from app.historical import CurrentSceneCheckpointResolver, ReplayBaselineBuilder
    from app.historical import CurrentHistoryCheckpointAudit
    from app.models import CharacterDecision, CharacterKnowledge, CharacterMemory, RetconReplaySession, ReplaySceneRun, Scene, ScenePerformanceTurn, WorldResolution, WorldSnapshot

    fixture = historical_multiscene_replay(session, monkeypatch)
    scene2, scene3, scene4 = fixture.scenes
    created = fixture.client.post(
        f"/projects/{fixture.project.id}/retcon/applications/{fixture.application_id}/replay-sessions"
    )
    assert created.status_code == 201, created.text
    state = created.json()
    assert [item["mode"] for item in state["queue"]] == ["REPLAY", "VALIDATE_PRESERVED", "REPLAY"]
    while state["cursor"] < len(state["queue"]):
        stepped = fixture.client.post(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}/step")
        assert stepped.status_code == 200, stepped.text
        state = fixture.client.get(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}").json()
    committed = fixture.client.post(
        f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}/commit",
        json={"explicit_confirmation": True},
    )
    assert committed.status_code == 200, committed.text
    session.expire_all()

    runs = {run.original_scene_id: run for run in session.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == state["id"])).all() if run.replacement_scene_id}
    replacement2 = session.get(Scene, runs[scene2.id].replacement_scene_id)
    replacement4 = session.get(Scene, runs[scene4.id].replacement_scene_id)
    assert session.get(Scene, scene2.id).history_status == "SUPERSEDED" and replacement2.history_status == "ACTIVE"
    assert session.get(Scene, scene3.id).history_status == "ACTIVE"
    assert session.get(Scene, scene4.id).history_status == "SUPERSEDED" and replacement4.history_status == "ACTIVE"

    resolver = CurrentSceneCheckpointResolver()
    checkpoint2 = resolver.current(session, fixture.project.id, replacement2.id)
    checkpoint3 = resolver.current(session, fixture.project.id, scene3.id)
    checkpoint4 = resolver.current(session, fixture.project.id, replacement4.id)
    post2 = session.get(WorldSnapshot, checkpoint2.post_snapshot_id)
    pre3 = session.get(WorldSnapshot, checkpoint3.pre_snapshot_id)
    post3 = session.get(WorldSnapshot, checkpoint3.post_snapshot_id)
    pre4 = session.get(WorldSnapshot, checkpoint4.pre_snapshot_id)
    summarize = lambda payload: [(row.get("id"), row.get("sequence"), row.get("status"), row.get("history_status"), row.get("summary")) for row in payload.get("scenes", [])]
    differing23 = {key: ((summarize(post2.payload), summarize(pre3.payload)) if key == "scenes" else (post2.payload.get(key), pre3.payload.get(key))) for key in post2.payload.keys() | pre3.payload.keys() if post2.payload.get(key) != pre3.payload.get(key)}
    differing34 = {key: ((summarize(post3.payload), summarize(pre4.payload)) if key == "scenes" else (post3.payload.get(key), pre4.payload.get(key))) for key in post3.payload.keys() | pre4.payload.keys() if post3.payload.get(key) != pre4.payload.get(key)}
    assert post2.state_fingerprint == pre3.state_fingerprint and post2.payload == pre3.payload, differing23
    assert post3.state_fingerprint == pre4.state_fingerprint and post3.payload == pre4.payload, differing34

    knowledge2 = session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.replay_session_id == state["id"], CharacterKnowledge.source == replacement2.id)).all()
    memories2 = session.scalars(select(CharacterMemory).where(CharacterMemory.replay_session_id == state["id"], CharacterMemory.source_scene == replacement2.id)).all()
    assert knowledge2 and memories2
    assert {row.id for row in knowledge2}.issubset({row["id"] for row in post2.payload["character_knowledge"]})
    assert {row.id for row in memories2}.issubset({row["id"] for row in post2.payload["character_memories"]})
    formal_knowledge = next(row for row in post2.payload["character_knowledge"] if row["id"] == knowledge2[0].id)
    formal_memory = next(row for row in post2.payload["character_memories"] if row["id"] == memories2[0].id)
    assert formal_knowledge["source"] == replacement2.id and formal_knowledge["replay_session_id"] == state["id"]
    assert formal_memory["source_scene"] == replacement2.id and formal_memory["replay_session_id"] == state["id"]
    serialized = json.dumps([post2.payload, pre3.payload, post3.payload, pre4.payload], sort_keys=True)
    for forbidden in ("replay-knowledge:", "replay-memory:", '"temp_id"', '"source_turn_temp_id"', '"source_resolution_temp_id"', '"fact_identity"', '"reason"'):
        assert forbidden not in serialized
    assert next(row for row in post2.payload["scenes"] if row["id"] == replacement2.id)["history_status"] == "ACTIVE"
    assert next(row for row in pre3.payload["scenes"] if row["id"] == replacement2.id)["history_status"] == "ACTIVE"
    assert all(row["id"] != scene2.id for row in post2.payload["scenes"])
    assert all(row["id"] != replacement4.id for row in post2.payload["scenes"])
    assert fixture.preserved_ids[3] in {row["id"] for row in post3.payload["character_knowledge"]}
    assert fixture.preserved_ids[4] in {row["id"] for row in post3.payload["character_memories"]}
    assert session.get(CharacterDecision, fixture.preserved_ids[0]) and session.get(ScenePerformanceTurn, fixture.preserved_ids[1]) and session.get(WorldResolution, fixture.preserved_ids[2])
    future_cognition = set(runs[scene4.id].new_knowledge_ids or []) | set(runs[scene4.id].new_memory_ids or [])
    assert future_cognition.isdisjoint({row["id"] for row in post2.payload["character_knowledge"] + post2.payload["character_memories"]})
    assert post3.payload["project"]["current_world_time"].startswith("2040-01-02")
    assert checkpoint3.version == 2 and checkpoint3.active is True
    baseline, _ = ReplayBaselineBuilder().build(session, fixture.project.id, scene3.id)
    assert baseline == pre3.payload
    replay_session = session.get(RetconReplaySession, state["id"])
    assert replay_session.pre_commit_snapshot_id and replay_session.post_commit_snapshot_id
    scene_snapshot_ids = {checkpoint2.pre_snapshot_id, checkpoint2.post_snapshot_id, checkpoint3.pre_snapshot_id, checkpoint3.post_snapshot_id, checkpoint4.pre_snapshot_id, checkpoint4.post_snapshot_id}
    assert replay_session.pre_commit_snapshot_id not in scene_snapshot_ids and replay_session.post_commit_snapshot_id not in scene_snapshot_ids
    CurrentHistoryCheckpointAudit().audit(session, fixture.project.id)
    checkpoint3.checkpoint_fingerprint = "corrupt-latest"
    session.flush()
    with pytest.raises(ValueError, match="SCENE_CHECKPOINT_INTEGRITY_INVALID"):
        ReplayBaselineBuilder().build(session, fixture.project.id, scene3.id)
