from types import SimpleNamespace
from dataclasses import replace

import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.main import app
import app.api as api
from app.autonomy import AutonomousWorldLoopService
from app.models import AutonomousRunStatus, AutonomousStepStatus, AutonomousWorldRun, AutonomousWorldStep, CausalLink, Character, CharacterDecision, CharacterKnowledge, CharacterMemory, Project, RecoveryCandidate, RetconApplication, RetconApplicationStatus, Scene, SceneCommit, ScenePerformance, ScenePerformanceTurn, SceneStateCheckpoint, StateDeltaBatch, StateDeltaItem, StoryThread, TimelineEvent, WorldEntity, WorldResolution, WorldSnapshot
from app.runtime import persisted_turns


def valid_performance_payload():
    return json.dumps({
        "decision": {
            "decision_type": "WAIT", "intent": "wait", "chosen_action": "wait",
            "motivation": "The character is cautious.", "target_character_id": None,
            "target_entity_id": None, "goal_refs": [], "knowledge_used": [],
            "memory_refs": [], "ability_refs": [], "inventory_refs": [],
            "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None,
            "expected_personal_result": None, "uncertainties": [], "refused_options": [],
            "boundary_override_reason": None, "decision_summary": "Wait.",
        },
        "action": {
            "visibility": "PUBLIC", "observable_action": "wait", "spoken_content": None,
            "requires_world_resolution": False, "world_resolution_request": None,
            "disclosure_knowledge_ids": [], "target_character_id": None,
        },
    })


class SequencedProvider:
    name = "fake"

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def generate(self, messages, model):
        from app.ai.provider import ModelResult
        item = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return ModelResult(content=item, latency_ms=0, request_id=f"fake-{self.calls}", provider="fake", model=model)


def world_action_payload(entity_id):
    payload = json.loads(valid_performance_payload())
    payload["decision"].update({
        "decision_type": "INVESTIGATE", "intent": "inspect", "chosen_action": "inspect",
        "target_entity_id": entity_id, "decision_summary": "Inspect the entity.",
    })
    payload["action"].update({
        "observable_action": "inspect the entity", "requires_world_resolution": True,
        "world_resolution_request": {"kind": "INSPECT", "description": "inspect", "target_entity_id": entity_id, "target_character_id": None},
    })
    return payload


def quiet_performance_payload(observable=None, memory_refs=None):
    payload = json.loads(valid_performance_payload())
    payload["decision"].update({"decision_type": "WAIT", "chosen_action": "wait", "memory_refs": list(memory_refs or [])})
    payload["action"].update({"observable_action": observable, "spoken_content": None})
    return payload


def fixed_candidate_generator(monkeypatch, keys):
    from app.director import DirectorCandidateEngine
    original = DirectorCandidateEngine.generate
    state = {"template": None, "calls": 0}

    def generate(self, context, gravity):
        if state["template"] is None:
            state["template"] = original(self, context, gravity)[0]
        index = min(state["calls"], len(keys) - 1)
        state["calls"] += 1
        return [replace(state["template"], candidate_key=keys[index])]

    monkeypatch.setattr(DirectorCandidateEngine, "generate", generate)
    return state


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


@pytest.fixture()
def world(session):
    project = Project(name="Autonomy")
    session.add(project); session.flush()
    location = WorldEntity(project_id=project.id, entity_type="LOCATION", name="Room")
    session.add(location); session.flush()
    actor = Character(project_id=project.id, name="Actor", current_state={"location_id": location.id}, goals={"current": "observe"}, narrative_relevance={"score": 3})
    thread = StoryThread(project_id=project.id, title="Open thread", type="MYSTERY", weight=4)
    session.add_all([actor, thread]); session.commit()
    return SimpleNamespace(project=project, location=location, actor=actor, thread=thread)


def create_service_run(session, world, budget=1):
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=budget)
    session.commit()
    return run


def test_run_schema_defaults_and_persistence(session, world):
    run = create_service_run(session, world)
    assert run.status == AutonomousRunStatus.CREATED and run.active is True and run.scene_budget == 1
    assert run.start_world_fingerprint == run.current_world_fingerprint
    assert run.start_history_fingerprint == run.current_history_fingerprint


def test_blocked_legacy_run_cancel_releases_active_lock(session, world):
    run = create_service_run(session, world)
    run.status, run.active = AutonomousRunStatus.BLOCKED, True
    AutonomousWorldLoopService().cancel(session, run.id)
    assert run.active is False


def test_run_payload_redacts_history_payloads(session, world):
    run = create_service_run(session, world)
    payload = AutonomousWorldLoopService.run_payload(run)
    assert "current_history_fingerprint" in payload
    assert "payload" not in payload and "secret" not in payload


def test_one_active_run_per_project(session, world):
    create_service_run(session, world)
    with pytest.raises(ValueError, match="AUTONOMY_RUN_ACTIVE"):
        AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=1)


def test_run_modes_and_config_are_immutable_by_service(session, world):
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=2, max_turns_per_scene=3, config={"seed": "x"})
    assert run.max_turns_per_scene == 3 and run.config == {"seed": "x"}


def test_resume_starts_created_run(session, world):
    run = create_service_run(session, world)
    AutonomousWorldLoopService().resume(session, run.id)
    assert run.status == AutonomousRunStatus.RUNNING and run.started_at is not None


def test_pause_resume_cancel_lifecycle(session, world):
    run = create_service_run(session, world); service = AutonomousWorldLoopService()
    service.pause(session, run.id, "MANUAL_PAUSE"); assert run.status == AutonomousRunStatus.PAUSED and run.active
    service.resume(session, run.id); assert run.status == AutonomousRunStatus.RUNNING
    service.cancel(session, run.id); assert run.status == AutonomousRunStatus.CANCELLED and not run.active


def test_advance_one_scene_creates_formal_pipeline(session, world):
    run = create_service_run(session, world)
    result = AutonomousWorldLoopService().advance(session, run.id, max_scenes=1, request_key="one")
    assert result["steps"][0]["status"] == "COMMITTED"
    assert session.scalar(select(func.count(Scene.id))) == 1
    assert session.scalar(select(func.count(SceneCommit.id))) == 1
    assert session.scalar(select(func.count(ScenePerformance.id))) == 1


def test_scene_budget_completes_run(session, world):
    run = create_service_run(session, world, budget=2)
    AutonomousWorldLoopService().advance(session, run.id, max_scenes=2, request_key="two")
    assert run.status == AutonomousRunStatus.COMPLETED and run.committed_scene_count == 2 and not run.active
    assert session.scalar(select(func.count(Scene.id))) == 2


def test_advance_idempotency_does_not_create_duplicate_scene(session, world):
    run = create_service_run(session, world, budget=3); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, max_scenes=1, request_key="retry", request_offset=0)
    second = service.advance(session, run.id, max_scenes=1, request_key="retry", request_offset=0)
    assert first["existing"] is False and second["existing"] is True
    assert session.scalar(select(func.count(Scene.id))) == 1


def test_step_audit_links_scene_and_checkpoint(session, world):
    run = create_service_run(session, world); AutonomousWorldLoopService().advance(session, run.id, request_key="audit")
    step = session.scalar(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id))
    assert step.status == AutonomousStepStatus.COMMITTED and step.proposal_id and step.performance_id and step.scene_id and step.checkpoint_id


def test_step_world_fingerprint_continuity(session, world):
    run = create_service_run(session, world, budget=2); AutonomousWorldLoopService().advance(session, run.id, max_scenes=2, request_key="continuity")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    assert steps[0].world_fingerprint_after == steps[1].world_fingerprint_before


def test_advance_commits_each_completed_scene(session, world):
    run = create_service_run(session, world, budget=2)
    AutonomousWorldLoopService().advance(session, run.id, max_scenes=2, request_key="durable")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    assert [step.status for step in steps] == [AutonomousStepStatus.COMMITTED, AutonomousStepStatus.COMMITTED]


def test_persisted_turn_reader_orders_by_sequence_and_id(session, world):
    run = create_service_run(session, world)
    AutonomousWorldLoopService().advance(session, run.id, request_key="turns")
    performance = session.scalar(select(ScenePerformance))
    turns = persisted_turns(session, performance.id)
    assert [turn.sequence for turn in turns] == list(range(1, len(turns) + 1))


def test_new_scene_rebuilds_context_and_sequence(session, world):
    run = create_service_run(session, world, budget=2); AutonomousWorldLoopService().advance(session, run.id, max_scenes=2, request_key="sequence")
    assert [scene.sequence for scene in session.scalars(select(Scene).order_by(Scene.sequence)).all()] == [1, 2]


def test_no_direct_world_mutation_on_run_controls(session, world):
    before = world.actor.current_state.copy(); run = create_service_run(session, world); service = AutonomousWorldLoopService(); service.pause(session, run.id); service.resume(session, run.id); service.cancel(session, run.id)
    session.refresh(world.actor); assert world.actor.current_state == before


def test_cross_project_run_isolation(session, world):
    other = Project(name="Other"); session.add(other); session.commit()
    run = create_service_run(session, world); api.SessionLocal = sessionmaker(bind=session.bind, expire_on_commit=False)
    response = TestClient(app).get(f"/projects/{other.id}/autonomous-runs/{run.id}")
    assert response.status_code == 404


def test_run_status_api_is_metadata_only(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    run = create_service_run(session, world)
    response = TestClient(app).get(f"/projects/{world.project.id}/autonomous-runs/{run.id}")
    assert response.status_code == 200 and "current_world_fingerprint" in response.json()["run"] and "payload" not in response.text


def test_run_create_api(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    response = TestClient(app).post(f"/projects/{world.project.id}/autonomous-runs", json={"scene_budget": 1})
    assert response.status_code == 201 and response.json()["status"] == "CREATED"


def test_run_advance_api(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False)); run = create_service_run(session, world)
    response = TestClient(app).post(f"/projects/{world.project.id}/autonomous-runs/{run.id}/advance", json={"max_scenes": 1, "idempotency_key": "api"})
    assert response.status_code == 200 and response.json()["steps"][0]["status"] == "COMMITTED"


def test_manual_director_is_blocked_while_run_active(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False)); create_service_run(session, world)
    assert TestClient(app).post(f"/projects/{world.project.id}/director/dry-run").status_code == 409


def test_run_cancel_api(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False)); run = create_service_run(session, world)
    response = TestClient(app).post(f"/projects/{world.project.id}/autonomous-runs/{run.id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"


def test_retcon_pending_blocks_advance(session, world):
    # Use the persisted application state, rather than a no-op normal project.
    application = RetconApplication(project_id=world.project.id, retcon_request_id="pending-request", retcon_plan_id="pending-plan", source_revision_id="pending-revision", status=RetconApplicationStatus.APPLIED_PENDING_REPLAY, plan_basis_fingerprint="basis", pre_apply_world_fingerprint="pre")
    session.add(application); session.commit()
    service = AutonomousWorldLoopService()
    with pytest.raises(ValueError, match="RETCON_REPLAY_REQUIRED"):
        service.create_run(session, world.project.id, scene_budget=1)


def test_pending_replay_blocks_existing_run_progression(session, world):
    run = create_service_run(session, world)
    session.add(RetconApplication(project_id=world.project.id, retcon_request_id="pending-request-2", retcon_plan_id="pending-plan-2", source_revision_id="pending-revision-2", status=RetconApplicationStatus.APPLIED_PENDING_REPLAY, plan_basis_fingerprint="basis", pre_apply_world_fingerprint="pre")); session.commit()
    service = AutonomousWorldLoopService()
    with pytest.raises(ValueError, match="RETCON_REPLAY_REQUIRED"):
        service.advance(session, run.id, request_key="pending")
    with pytest.raises(ValueError, match="RETCON_REPLAY_REQUIRED"):
        service.resume(session, run.id)


def test_new_thread_candidate_is_not_materialized(session, world):
    world.thread.status = "RESOLVED"; session.commit(); run = create_service_run(session, world)
    # Existing active actor still permits a gravity candidate; no new StoryThread is created by the loop.
    before = session.scalar(select(func.count(StoryThread.id))); AutonomousWorldLoopService().advance(session, run.id, request_key="thread")
    assert session.scalar(select(func.count(StoryThread.id))) == before


def test_run_budget_is_hard_bound(session, world):
    run = create_service_run(session, world, budget=1); AutonomousWorldLoopService().advance(session, run.id, max_scenes=5, request_key="bound")
    assert session.scalar(select(func.count(Scene.id))) == 1


def test_budget_clipped_request_retry_is_idempotent(session, world):
    run = create_service_run(session, world, budget=1); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, max_scenes=5, request_key="budget-retry")
    step_id = first["steps"][0]["id"]
    second = service.advance(session, run.id, max_scenes=5, request_key="budget-retry")
    assert second["existing"] is True and [item["id"] for item in second["steps"]] == [step_id]
    assert session.scalar(select(func.count(AutonomousWorldStep.id))) == 1


def test_committed_request_retry_does_not_extend_window(session, world):
    run = create_service_run(session, world, budget=5); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, max_scenes=2, request_key="two-retry")
    assert len(first["steps"]) == 2
    second = service.advance(session, run.id, max_scenes=2, request_key="two-retry")
    assert second["existing"] is True and len(second["steps"]) == 2
    assert session.scalar(select(func.count(AutonomousWorldStep.id))) == 2


def test_provider_retry_continues_same_step_proposal_and_performance(session, world, monkeypatch):
    from app.ai.errors import MODEL_TIMEOUT, ModelProviderError
    provider = SequencedProvider([ModelProviderError(MODEL_TIMEOUT), valid_performance_payload()])
    monkeypatch.setattr("app.runtime.get_model_provider", lambda *args, **kwargs: provider)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=1, max_turns_per_scene=1, performance_mode="LLM")
    session.commit(); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, max_scenes=1, request_key="provider-retry")
    paused = session.get(AutonomousWorldStep, first["steps"][0]["id"])
    identity = (paused.id, paused.ordinal, paused.proposal_id, paused.performance_id)
    assert paused.status == AutonomousStepStatus.PAUSED and run.status == AutonomousRunStatus.PAUSED
    assert paused.error_code == MODEL_TIMEOUT and run.last_error_code == MODEL_TIMEOUT
    assert session.scalar(select(func.count(Scene.id))) == 0
    service.resume(session, run.id)
    second = service.advance(session, run.id, max_scenes=1, request_key="provider-retry")
    committed = session.get(AutonomousWorldStep, second["steps"][0]["id"])
    assert (committed.id, committed.ordinal, committed.proposal_id, committed.performance_id) == identity
    assert committed.status == AutonomousStepStatus.COMMITTED
    assert session.scalar(select(func.count(Scene.id))) == 1


def test_partial_request_retry_continues_exact_paused_offset(session, world, monkeypatch):
    from app.ai.errors import MODEL_TIMEOUT, ModelProviderError
    provider = SequencedProvider([valid_performance_payload(), ModelProviderError(MODEL_TIMEOUT), valid_performance_payload()])
    monkeypatch.setattr("app.runtime.get_model_provider", lambda *args, **kwargs: provider)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=2, max_turns_per_scene=1, performance_mode="LLM")
    session.commit(); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, max_scenes=2, request_key="partial-retry")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.request_offset)).all()
    assert [step.status for step in steps] == [AutonomousStepStatus.COMMITTED, AutonomousStepStatus.PAUSED]
    paused_identity = (steps[1].id, steps[1].proposal_id, steps[1].performance_id)
    service.resume(session, run.id)
    second = service.advance(session, run.id, max_scenes=2, request_key="partial-retry")
    final = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.request_offset)).all()
    assert second["existing"] is True and len(final) == 2
    assert (final[1].id, final[1].proposal_id, final[1].performance_id) == paused_identity
    assert [step.status for step in final] == [AutonomousStepStatus.COMMITTED, AutonomousStepStatus.COMMITTED]
    assert session.scalar(select(func.count(Scene.id))) == 2


def test_second_provider_failure_stops_request_without_next_offset(session, world, monkeypatch):
    from app.ai.errors import MODEL_TIMEOUT, ModelProviderError
    provider = SequencedProvider([valid_performance_payload(), ModelProviderError(MODEL_TIMEOUT), ModelProviderError(MODEL_TIMEOUT)])
    monkeypatch.setattr("app.runtime.get_model_provider", lambda *args, **kwargs: provider)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=4, max_turns_per_scene=1, performance_mode="LLM")
    session.commit(); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, max_scenes=3, request_key="second-failure")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.request_offset)).all()
    assert [step.status for step in steps] == [AutonomousStepStatus.COMMITTED, AutonomousStepStatus.PAUSED]
    assert [item["id"] for item in first["steps"]] == [step.id for step in steps]
    assert run.status == AutonomousRunStatus.PAUSED and run.last_error_code == MODEL_TIMEOUT
    service.resume(session, run.id)
    retry = service.advance(session, run.id, max_scenes=3, request_key="second-failure")
    steps_after = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.request_offset)).all()
    assert retry["existing"] is True and len(retry["steps"]) == 2
    assert [step.id for step in steps_after] == [step.id for step in steps]
    assert steps_after[1].status == AutonomousStepStatus.PAUSED
    assert run.status == AutonomousRunStatus.PAUSED and run.last_error_code == MODEL_TIMEOUT
    assert session.scalar(select(func.count(Scene.id))) == 1


def test_resumed_third_stagnant_step_stops_before_next_offset(session, world, monkeypatch):
    from app.ai.errors import MODEL_TIMEOUT, ModelProviderError
    provider = SequencedProvider([
        valid_performance_payload(),
        valid_performance_payload(),
        ModelProviderError(MODEL_TIMEOUT),
        valid_performance_payload(),
    ])
    fixed_candidate_generator(monkeypatch, ["resumed-stagnation"])
    monkeypatch.setattr("app.runtime.get_model_provider", lambda *args, **kwargs: provider)
    run = AutonomousWorldLoopService().create_run(
        session,
        world.project.id,
        scene_budget=4,
        max_turns_per_scene=1,
        performance_mode="LLM",
    )
    session.commit(); service = AutonomousWorldLoopService()
    service.advance(session, run.id, max_scenes=4, request_key="resumed-stagnation")
    paused = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.request_offset)).all()
    assert [step.status for step in paused] == [AutonomousStepStatus.COMMITTED, AutonomousStepStatus.COMMITTED, AutonomousStepStatus.PAUSED]
    paused_identity = (paused[2].id, paused[2].proposal_id, paused[2].performance_id)

    service.resume(session, run.id)
    result = service.advance(session, run.id, max_scenes=4, request_key="resumed-stagnation")
    final = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.request_offset)).all()
    assert len(result["steps"]) == 3 and len(final) == 3
    assert (final[2].id, final[2].proposal_id, final[2].performance_id) == paused_identity
    assert final[2].status == AutonomousStepStatus.COMMITTED
    assert run.status == AutonomousRunStatus.PAUSED and run.stop_reason == "STAGNATION_GUARD"
    assert session.scalar(select(func.count(Scene.id))) == 3


def test_recovery_adoption_resumes_same_step_and_keeps_provenance(session, world, monkeypatch):
    class InvalidPerformer:
        def perform(self, context):
            payload = json.loads(valid_performance_payload())
            payload["action"].update({"visibility": "TARGETED", "target_character_id": "missing-character"})
            return payload, None

    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", InvalidPerformer)
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=1, max_turns_per_scene=1)
    session.commit(); service = AutonomousWorldLoopService()
    first = service.advance(session, run.id, request_key="recover-adopt")
    step = session.get(AutonomousWorldStep, first["steps"][0]["id"])
    identity = (step.id, step.ordinal, step.proposal_id, step.performance_id)
    assert step.status == AutonomousStepStatus.PAUSED and len(step.recovery_candidate_ids) == 1
    candidate_id = step.recovery_candidate_ids[0]
    assert session.get(RecoveryCandidate, candidate_id).status == "OPEN"

    client = TestClient(app)
    edited = client.post(f"/projects/{world.project.id}/recovery-candidates/{candidate_id}/edit", json={
        "base_version": 1,
        "changes": [
            {"operation": "SET", "path": "/action/visibility", "value": "PUBLIC"},
            {"operation": "SET", "path": "/action/target_character_id", "value": None},
        ],
    })
    assert edited.status_code == 200 and edited.json()["candidate"]["status"] == "VALIDATED"
    adopted = client.post(f"/projects/{world.project.id}/recovery-candidates/{candidate_id}/adopt")
    assert adopted.status_code == 200 and adopted.json()["candidate"]["status"] == "ADOPTED"

    session.expire_all()
    service.resume(session, run.id)
    second = service.advance(session, run.id, request_key="recover-adopt")
    committed = session.get(AutonomousWorldStep, second["steps"][0]["id"])
    assert (committed.id, committed.ordinal, committed.proposal_id, committed.performance_id) == identity
    assert committed.status == AutonomousStepStatus.COMMITTED
    assert committed.recovery_candidate_ids == [candidate_id]
    assert session.scalar(select(func.count(Scene.id))) == 1


@pytest.mark.parametrize(
    ("resolver_payload", "expected_status", "expected_reason"),
    [
        ({"outcome": "UNRESOLVED", "outcome_summary": "Unknown.", "objective_facts": [], "state_effects": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [], "resolution_basis_summary": None, "missing_information": ["A formal fact is required."]}, "UNRESOLVED", "WORLD_INFORMATION_MISSING"),
        ({"outcome": "SUCCESS", "outcome_summary": "Invalid scope.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": "outside-scope", "predicate": "opened", "value": True}], "state_effects": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [], "resolution_basis_summary": "structured", "missing_information": []}, "REJECTED", "WORLD_RESOLUTION_REJECTED"),
    ],
)
def test_world_resolution_nonvalid_outcomes_are_durable(session, world, monkeypatch, resolver_payload, expected_status, expected_reason):
    class Performer:
        def perform(self, context): return world_action_payload(world.location.id), None
    class Resolver:
        def resolve(self, context): return resolver_payload, None
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", Performer)
    monkeypatch.setattr("app.runtime.HeuristicWorldResolver", Resolver)
    before = dict(world.location.profile or {})
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=1, max_turns_per_scene=1)
    session.commit()
    result = AutonomousWorldLoopService().advance(session, run.id, request_key=f"world-{expected_status.lower()}")
    resolution = session.scalar(select(WorldResolution).where(WorldResolution.project_id == world.project.id))
    step = session.get(AutonomousWorldStep, result["steps"][0]["id"])
    assert getattr(resolution.status, "value", resolution.status) == expected_status
    assert step.status == AutonomousStepStatus.PAUSED and step.stop_reason == expected_reason
    assert len(step.recovery_candidate_ids) == 1 and session.get(RecoveryCandidate, step.recovery_candidate_ids[0])
    assert session.scalar(select(func.count(Scene.id))) == 0
    session.refresh(world.location); assert world.location.profile == before


def test_real_state_delta_rejection_blocks_run_without_scene(session, world, monkeypatch):
    class Performer:
        def perform(self, context): return world_action_payload(world.location.id), None
    class Resolver:
        def resolve(self, context):
            effect = lambda operation: {"effect_kind": "STATE_CHANGE", "target_type": "WORLD_ENTITY", "target_id": world.location.id, "domain": "WORLD_ENTITY_PROFILE", "operation": operation, "path": "/profile/opened", "value": True, "reason": "structured test", "evidence": {"kind": "INSPECT", "target_entity_id": world.location.id}}
            return {"outcome": "SUCCESS", "outcome_summary": "Conflicting effects.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": world.location.id, "predicate": "opened", "value": True}], "state_effects": [effect("SET"), effect("UPSERT")], "actor_observation": "Observed.", "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [world.location.id], "resolution_basis_summary": "structured", "missing_information": []}, None
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", Performer)
    monkeypatch.setattr("app.runtime.HeuristicWorldResolver", Resolver)
    world.location.profile = {**(world.location.profile or {}), "opened": False}; session.commit()
    before = dict(world.location.profile or {})
    run = create_service_run(session, world)
    result = AutonomousWorldLoopService().advance(session, run.id, request_key="delta-reject")
    step = session.get(AutonomousWorldStep, result["steps"][0]["id"])
    batch = session.scalar(select(StateDeltaBatch).where(StateDeltaBatch.source_resolution_id.is_not(None)))
    assert getattr(batch.status, "value", batch.status) == "REJECTED"
    assert step.status == AutonomousStepStatus.BLOCKED and run.status == AutonomousRunStatus.BLOCKED and run.active is False
    assert session.scalar(select(func.count(Scene.id))) == 0
    session.refresh(world.location); assert world.location.profile == before


def test_two_world_resolutions_keep_turn_sequence_continuous(session, world, monkeypatch):
    class Performer:
        def perform(self, context): return world_action_payload(world.location.id), None
    class Resolver:
        calls = 0
        def resolve(self, context):
            Resolver.calls += 1
            predicate = f"observed_{Resolver.calls}"
            effect = {"effect_kind": "STATE_CHANGE", "target_type": "WORLD_ENTITY", "target_id": world.location.id, "domain": "WORLD_ENTITY_PROFILE", "operation": "SET", "path": f"/profile/{predicate}", "value": True, "reason": "structured test", "evidence": {"kind": "INSPECT", "target_entity_id": world.location.id}}
            return {"outcome": "SUCCESS", "outcome_summary": "Observed.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": world.location.id, "predicate": predicate, "value": True}], "state_effects": [effect], "actor_observation": "Observed.", "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [world.location.id], "resolution_basis_summary": "structured", "missing_information": []}, None
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", Performer)
    monkeypatch.setattr("app.runtime.HeuristicWorldResolver", Resolver)
    world.location.profile = {**(world.location.profile or {}), "observed_1": False, "observed_2": False}; session.commit()
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=1, max_turns_per_scene=2)
    session.commit()
    result = AutonomousWorldLoopService().advance(session, run.id, request_key="two-resolutions")
    step = session.get(AutonomousWorldStep, result["steps"][0]["id"])
    turns = session.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == step.performance_id).order_by(ScenePerformanceTurn.sequence)).all()
    resolutions = session.scalars(select(WorldResolution).where(WorldResolution.performance_id == step.performance_id).order_by(WorldResolution.created_at, WorldResolution.id)).all()
    assert [turn.sequence for turn in turns] == [1, 2]
    assert len(resolutions) == 2 and all(getattr(item.status, "value", item.status) == "VALID" for item in resolutions)
    assert session.get(ScenePerformance, step.performance_id).turn_count == 2
    assert step.status == AutonomousStepStatus.COMMITTED and step.resolution_count == 2


def test_api_baseline_block_is_durable_in_fresh_session(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    run = create_service_run(session, world)
    world.thread.weight = 11; session.commit()
    response = TestClient(app).post(f"/projects/{world.project.id}/autonomy/runs/{run.id}/resume")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "AUTONOMY_BASELINE_CHANGED"
    with sessionmaker(bind=session.bind, expire_on_commit=False)() as fresh:
        persisted = fresh.get(AutonomousWorldRun, run.id)
        assert persisted.status == AutonomousRunStatus.BLOCKED and persisted.active is False and persisted.stop_reason == "AUTONOMY_BASELINE_CHANGED"


def test_step_request_offset_distinguishes_requests(session, world):
    run = create_service_run(session, world, budget=2); service = AutonomousWorldLoopService(); service.advance(session, run.id, request_key="offset", request_offset=0); service.advance(session, run.id, request_key="offset", request_offset=1)
    assert session.scalar(select(func.count(AutonomousWorldStep.id))) == 2


def test_run_status_contains_no_model_transcript(session, world):
    run = create_service_run(session, world); payload = AutonomousWorldLoopService().get_status(session, run.id)
    assert "prompt" not in str(payload).lower() and "output" not in str(payload).lower()


def test_formal_world_changes_only_after_scene_commit(session, world):
    run = create_service_run(session, world); before = run.current_world_fingerprint; AutonomousWorldLoopService().advance(session, run.id, request_key="formal")
    assert run.current_world_fingerprint != "" and before != run.current_world_fingerprint


def test_step_finalization_failure_rolls_back_scene(session, world):
    run = create_service_run(session, world)
    service = AutonomousWorldLoopService()
    def inject(stage, *_):
        if stage == "AFTER_SCENE_COMMIT_BEFORE_STEP_FINALIZATION":
            raise RuntimeError("inject")
    service.failure_injector = inject
    with pytest.raises(ValueError, match="AUTONOMY_SCENE_FAILED"):
        service.advance(session, run.id, request_key="finalize-fail")
    service.failure_injector = None
    with sessionmaker(bind=session.bind, expire_on_commit=False)() as fresh:
        assert fresh.scalar(select(func.count(Scene.id))) == 0
        assert fresh.scalar(select(func.count(SceneCommit.id))) == 0
        assert fresh.scalar(select(func.count(SceneStateCheckpoint.id))) == 0
        assert fresh.scalar(select(func.count(CharacterKnowledge.id))) == 0
        assert fresh.scalar(select(func.count(CharacterMemory.id))) == 0
        assert fresh.scalar(select(func.count(TimelineEvent.id))) == 0
        assert fresh.scalar(select(func.count(CausalLink.id))) == 0
        assert fresh.get(AutonomousWorldRun, run.id).status == AutonomousRunStatus.PAUSED


def test_scene_commit_materialization_failure_is_atomic_in_fresh_session(session, world, monkeypatch):
    from app.scene_commit import SceneCommitService
    monkeypatch.setattr(SceneCommitService, "failure_injector", staticmethod(lambda stage: (_ for _ in ()).throw(RuntimeError("injected")) if stage == "AFTER_SCENE_COMMIT_MATERIALIZATION" else None))
    run = create_service_run(session, world)
    with pytest.raises(ValueError, match="AUTONOMY_SCENE_FAILED"):
        AutonomousWorldLoopService().advance(session, run.id, request_key="scene-commit-fail")
    with sessionmaker(bind=session.bind, expire_on_commit=False)() as fresh:
        for model in (Scene, SceneCommit, SceneStateCheckpoint, CharacterKnowledge, CharacterMemory, TimelineEvent, CausalLink):
            assert fresh.scalar(select(func.count(model.id))) == 0
        persisted = fresh.get(AutonomousWorldRun, run.id)
        assert persisted.status == AutonomousRunStatus.PAUSED and persisted.committed_scene_count == 0


def test_later_failure_preserves_prior_durable_scenes(session, world):
    run = create_service_run(session, world, budget=3)
    service = AutonomousWorldLoopService(); calls = {"count": 0}
    def inject(stage, *_):
        if stage == "AFTER_SCENE_COMMIT_BEFORE_STEP_FINALIZATION":
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("third")
    service.failure_injector = inject
    with pytest.raises(ValueError, match="AUTONOMY_SCENE_FAILED"):
        service.advance(session, run.id, max_scenes=3, request_key="three")
    service.failure_injector = None
    with sessionmaker(bind=session.bind, expire_on_commit=False)() as fresh:
        assert fresh.scalar(select(func.count(Scene.id))) == 2
        steps = fresh.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
        assert [step.status for step in steps] == [AutonomousStepStatus.COMMITTED, AutonomousStepStatus.COMMITTED]
        persisted = fresh.get(AutonomousWorldRun, run.id)
        assert persisted.committed_scene_count == 2 and persisted.status == AutonomousRunStatus.PAUSED


def test_three_scene_e2e_has_checkpoint_and_causal_continuity(session, world):
    from app.causal_ledger import CurrentCausalLedgerAudit
    from app.historical import CurrentHistoryCheckpointAudit
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=3, config={"stagnation_limit": 0})
    session.commit()
    result = AutonomousWorldLoopService().advance(session, run.id, max_scenes=3, request_key="three-success")
    assert [item["status"] for item in result["steps"]] == ["COMMITTED", "COMMITTED", "COMMITTED"]
    scenes = session.scalars(select(Scene).where(Scene.project_id == world.project.id, Scene.history_status == "ACTIVE").order_by(Scene.sequence)).all()
    assert [scene.sequence for scene in scenes] == [1, 2, 3]
    checkpoints = [session.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.scene_id == scene.id, SceneStateCheckpoint.active.is_(True))) for scene in scenes]
    snapshots = [(session.get(WorldSnapshot, item.pre_snapshot_id), session.get(WorldSnapshot, item.post_snapshot_id)) for item in checkpoints]
    assert snapshots[0][1].state_fingerprint == snapshots[1][0].state_fingerprint
    assert snapshots[1][1].state_fingerprint == snapshots[2][0].state_fingerprint
    from app.snapshot_storage import SnapshotPayloadResolver
    resolver = SnapshotPayloadResolver()
    assert resolver.materialize(session, snapshots[0][1]) == resolver.materialize(session, snapshots[1][0])
    assert resolver.materialize(session, snapshots[1][1]) == resolver.materialize(session, snapshots[2][0])
    CurrentHistoryCheckpointAudit().audit(session, world.project.id)
    CurrentCausalLedgerAudit().audit(session, world.project.id)
    for scene in scenes:
        assert session.scalar(select(func.count(TimelineEvent.id)).where(TimelineEvent.scene_id == scene.id, TimelineEvent.event_type == "SCENE_OCCURRED", TimelineEvent.active.is_(True))) == 1


def test_real_stagnation_stops_after_third_committed_scene(session, world, monkeypatch):
    class QuietPerformer:
        def perform(self, context): return quiet_performance_payload(), None
    fixed_candidate_generator(monkeypatch, ["same-candidate"])
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", QuietPerformer)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=4, max_turns_per_scene=2)
    session.commit()
    service = AutonomousWorldLoopService()
    result = service.advance(session, run.id, max_scenes=4, request_key="real-stagnation")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    assert len(result["steps"]) == 3 and len(steps) == 3, [
        (step.ordinal, step.candidate_key, step.delta_batch_ids, session.get(ScenePerformance, step.performance_id).stop_reason)
        for step in steps
    ]
    assert all(step.status == AutonomousStepStatus.COMMITTED and step.candidate_key == "same-candidate" and not step.delta_batch_ids for step in steps)
    assert all(step.stop_reason == "QUIESCENT" for step in steps)
    assert all(session.get(ScenePerformance, step.performance_id).stop_reason == "SCENE_COMMITTED" for step in steps)
    assert run.status == AutonomousRunStatus.PAUSED and run.stop_reason == "STAGNATION_GUARD"
    assert session.scalar(select(func.count(Scene.id))) == 3
    retry = service.advance(session, run.id, max_scenes=4, request_key="real-stagnation")
    assert retry["existing"] is True and [item["id"] for item in retry["steps"]] == [step.id for step in steps]
    assert session.scalar(select(func.count(Scene.id))) == 3


def test_new_request_while_stagnated_is_rejected(session, world, monkeypatch):
    class QuietPerformer:
        def perform(self, context): return quiet_performance_payload(), None
    fixed_candidate_generator(monkeypatch, ["same-candidate"])
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", QuietPerformer)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=4, max_turns_per_scene=2)
    session.commit(); service = AutonomousWorldLoopService()
    service.advance(session, run.id, max_scenes=4, request_key="stagnated")
    with pytest.raises(ValueError, match="STAGNATION_GUARD"):
        service.advance(session, run.id, max_scenes=1, request_key="brand-new")
    assert session.scalar(select(func.count(Scene.id))) == 3


def test_candidate_change_resets_real_stagnation(session, world, monkeypatch):
    class QuietPerformer:
        def perform(self, context): return quiet_performance_payload(), None
    fixed_candidate_generator(monkeypatch, ["candidate-a", "candidate-a", "candidate-b"])
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", QuietPerformer)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=4, max_turns_per_scene=2)
    session.commit()
    AutonomousWorldLoopService().advance(session, run.id, max_scenes=3, request_key="candidate-reset")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    assert [step.candidate_key for step in steps] == ["candidate-a", "candidate-a", "candidate-b"]
    assert run.status == AutonomousRunStatus.RUNNING and run.stop_reason is None
    assert session.scalar(select(func.count(Scene.id))) == 3


def test_state_delta_progress_resets_real_stagnation(session, world, monkeypatch):
    class Performer:
        calls = 0
        def perform(self, context):
            Performer.calls += 1
            return (world_action_payload(world.location.id) if Performer.calls == 2 else quiet_performance_payload()), None
    class Resolver:
        def resolve(self, context):
            effect = {"effect_kind": "STATE_CHANGE", "target_type": "WORLD_ENTITY", "target_id": world.location.id, "domain": "WORLD_ENTITY_PROFILE", "operation": "SET", "path": "/profile/opened", "value": True, "reason": "structured progress", "evidence": {"kind": "INSPECT", "target_entity_id": world.location.id}}
            return {"outcome": "SUCCESS", "outcome_summary": "Opened.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": world.location.id, "predicate": "opened", "value": True}], "state_effects": [effect], "actor_observation": "The entity opens.", "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [world.location.id], "resolution_basis_summary": "structured", "missing_information": []}, None
    world.location.profile = {**(world.location.profile or {}), "opened": False}; session.commit()
    fixed_candidate_generator(monkeypatch, ["same-progress-candidate"])
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", Performer)
    monkeypatch.setattr("app.runtime.HeuristicWorldResolver", Resolver)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=4, max_turns_per_scene=1)
    session.commit()
    AutonomousWorldLoopService().advance(session, run.id, max_scenes=3, request_key="progress-reset")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    assert len(steps) == 3 and len(steps[1].delta_batch_ids) == 1
    assert session.scalar(select(func.count(StateDeltaItem.id)).where(StateDeltaItem.batch_id == steps[1].delta_batch_ids[0])) == 1
    assert run.status == AutonomousRunStatus.RUNNING and run.stop_reason is None


def test_scene_one_consequence_is_in_scene_two_autonomy_gravity(session, world, monkeypatch):
    from app.director import DirectorCandidateEngine
    original_generate = DirectorCandidateEngine.generate
    generated_inputs = []
    class Performer:
        calls = 0
        def perform(self, context):
            Performer.calls += 1
            return (world_action_payload(world.location.id) if Performer.calls == 1 else quiet_performance_payload()), None
    class Resolver:
        def resolve(self, context):
            effect = {"effect_kind": "STATE_CHANGE", "target_type": "WORLD_ENTITY", "target_id": world.location.id, "domain": "WORLD_ENTITY_PROFILE", "operation": "SET", "path": "/profile/opened", "value": True, "reason": "structured consequence", "evidence": {"kind": "INSPECT", "target_entity_id": world.location.id}}
            return {"outcome": "SUCCESS", "outcome_summary": "Opened.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": world.location.id, "predicate": "opened", "value": True}], "state_effects": [effect], "actor_observation": "The entity opens.", "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [world.location.id], "resolution_basis_summary": "structured", "missing_information": []}, None
    def capture_generate(self, context, gravity):
        generated_inputs.append((context, gravity))
        return original_generate(self, context, gravity)
    world.location.profile = {**(world.location.profile or {}), "opened": False}; session.commit()
    monkeypatch.setattr(DirectorCandidateEngine, "generate", capture_generate)
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", Performer)
    monkeypatch.setattr("app.runtime.HeuristicWorldResolver", Resolver)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=2, max_turns_per_scene=1)
    session.commit(); AutonomousWorldLoopService().advance(session, run.id, max_scenes=2, request_key="story-feedback")
    state_event = session.scalar(select(TimelineEvent).where(TimelineEvent.project_id == world.project.id, TimelineEvent.event_type == "STATE_CHANGE", TimelineEvent.active.is_(True)))
    assert state_event and state_event.scene_id and state_event.target_id == world.location.id and state_event.path == "/profile/opened"
    assert len(generated_inputs) == 2
    second_context, second_gravity = generated_inputs[1]
    assert any(row["id"] == state_event.id and row["target_id"] == world.location.id and row["path"] == "/profile/opened" for row in second_context["state_changes"])
    assert any(row["id"] == state_event.id and row["pressure_score"] > 0 for row in second_gravity.consequence_pressure)


def test_scene_one_memory_is_recalled_and_causes_scene_two_decision(session, world, monkeypatch):
    recalled = []
    class MemoryAwarePerformer:
        calls = 0
        def perform(self, context):
            MemoryAwarePerformer.calls += 1
            if MemoryAwarePerformer.calls == 1:
                return quiet_performance_payload(observable="A brass bell rings once."), None
            memory_ids = [item["memory_id"] for item in context.get("memories", [])]
            recalled.extend(memory_ids)
            return quiet_performance_payload(memory_refs=memory_ids[:1]), None
    fixed_candidate_generator(monkeypatch, ["memory-feedback"])
    monkeypatch.setattr("app.runtime.HeuristicCharacterPerformer", MemoryAwarePerformer)
    run = AutonomousWorldLoopService().create_run(session, world.project.id, scene_budget=2, max_turns_per_scene=1)
    session.commit(); AutonomousWorldLoopService().advance(session, run.id, max_scenes=2, request_key="memory-feedback")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    memory = session.scalar(select(CharacterMemory).where(CharacterMemory.source_scene == steps[0].scene_id, CharacterMemory.character_id == world.actor.id, CharacterMemory.content == "A brass bell rings once."))
    assert memory and recalled == [memory.id]
    scene_two_turn = session.scalar(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == steps[1].performance_id).order_by(ScenePerformanceTurn.sequence))
    decision = session.get(CharacterDecision, scene_two_turn.character_decision_id)
    assert decision.memory_refs == [memory.id]
    edge = session.scalar(select(CausalLink).where(CausalLink.project_id == world.project.id, CausalLink.cause_id == memory.id, CausalLink.effect_id == decision.id, CausalLink.relation_type == "MEMORY_INFORMED_DECISION", CausalLink.active.is_(True)))
    assert edge is not None


def test_resume_checks_zero_commit_history_boundary(session, world):
    run = create_service_run(session, world)
    world.thread.weight = 9
    with pytest.raises(ValueError, match="AUTONOMY_BASELINE_CHANGED"):
        AutonomousWorldLoopService().resume(session, run.id)
    assert run.status == AutonomousRunStatus.BLOCKED and run.active is False


def test_state_delta_block_releases_active_run(session, world):
    run = create_service_run(session, world)
    step = AutonomousWorldStep(project_id=world.project.id, run_id=run.id, ordinal=1, status=AutonomousStepStatus.RUNNING, request_key="blocked", request_offset=0, stage="STATE_DELTA", scene_sequence_before=0, world_fingerprint_before=run.current_world_fingerprint)
    session.add(step); session.flush()
    AutonomousWorldLoopService()._blocked(step, run, "STATE_DELTA_REJECTED", stage="STATE_DELTA")
    assert step.status == AutonomousStepStatus.BLOCKED and run.status == AutonomousRunStatus.BLOCKED and not run.active


def test_history_fingerprint_is_part_of_run_fingerprint(session, world):
    run = create_service_run(session, world)
    before = run.autonomous_run_fingerprint
    run.current_history_fingerprint = "history-changed"
    AutonomousWorldLoopService()._refresh_run_fingerprint(session, run)
    assert run.autonomous_run_fingerprint != before


def test_step_payload_excludes_recovery_payload_content(session, world):
    run = create_service_run(session, world)
    step = AutonomousWorldStep(project_id=world.project.id, run_id=run.id, ordinal=1, status=AutonomousStepStatus.PAUSED, request_key="safe", request_offset=0, stage="PERFORMANCE", scene_sequence_before=0, world_fingerprint_before=run.current_world_fingerprint, recovery_candidate_ids=["candidate"])
    payload = AutonomousWorldLoopService.step_payload(step)
    assert "recovery_candidate_ids" not in payload and "error_detail" not in payload


def test_scene_failure_does_not_create_committed_step(session, world):
    run = create_service_run(session, world)
    service = AutonomousWorldLoopService()
    service.failure_injector = lambda stage, *_: (_ for _ in ()).throw(RuntimeError("fail")) if stage == "AFTER_SCENE_COMMIT_BEFORE_STEP_FINALIZATION" else None
    with pytest.raises(ValueError):
        service.advance(session, run.id, request_key="no-committed")
    service.failure_injector = None
    assert not session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id, AutonomousWorldStep.status == AutonomousStepStatus.COMMITTED)).all()


def test_terminal_run_active_invariant_after_budget(session, world):
    run = create_service_run(session, world)
    AutonomousWorldLoopService().advance(session, run.id, request_key="terminal")
    assert run.status == AutonomousRunStatus.COMPLETED and run.active is False


def test_step_world_before_matches_run_start(session, world):
    run = create_service_run(session, world)
    AutonomousWorldLoopService().advance(session, run.id, request_key="boundary")
    step = session.scalar(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id))
    assert step.world_fingerprint_before == run.start_world_fingerprint


@pytest.mark.parametrize("status", [AutonomousRunStatus.COMPLETED, AutonomousRunStatus.FAILED, AutonomousRunStatus.CANCELLED, AutonomousRunStatus.BLOCKED, AutonomousRunStatus.PAUSED])
def test_run_status_active_contract(session, world, status):
    run = create_service_run(session, world)
    run.status = status
    run.active = status == AutonomousRunStatus.PAUSED
    if status == AutonomousRunStatus.BLOCKED:
        AutonomousWorldLoopService().cancel(session, run.id)
    assert run.active is (status == AutonomousRunStatus.PAUSED)


def test_step_and_run_are_project_scoped(session, world):
    run = create_service_run(session, world); AutonomousWorldLoopService().advance(session, run.id, request_key="scope")
    step = session.scalar(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id)); assert step.project_id == world.project.id


def test_run_list_api_filters_project(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False)); create_service_run(session, world)
    response = TestClient(app).get(f"/projects/{world.project.id}/autonomous-runs")
    assert response.status_code == 200 and len(response.json()) == 1


def test_standard_autonomy_route_alias_is_supported(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    response = TestClient(app).post(f"/projects/{world.project.id}/autonomy/runs", json={"scene_budget": 1, "idempotency_key": "create-alias"})
    assert response.status_code == 201 and response.json()["status"] == "CREATED"


def test_create_idempotency_returns_same_run(session, world):
    service = AutonomousWorldLoopService()
    first = service.create_run(session, world.project.id, scene_budget=2, client_request_id="same-request")
    session.commit()
    second = service.create_run(session, world.project.id, scene_budget=2, client_request_id="same-request")
    assert second.id == first.id


def test_invalid_advance_limit_is_rejected_without_step(session, world):
    run = create_service_run(session, world)
    with pytest.raises(ValueError, match="INVALID_ADVANCE_LIMIT"):
        AutonomousWorldLoopService().advance(session, run.id, max_scenes=21)
    assert session.scalar(select(func.count(AutonomousWorldStep.id))) == 0


def test_terminal_run_cannot_advance(session, world):
    run = create_service_run(session, world)
    service = AutonomousWorldLoopService(); service.cancel(session, run.id)
    with pytest.raises(ValueError, match="CANCELLED"):
        service.advance(session, run.id)


def test_pause_api_uses_user_reason_and_resume_alias(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    run = create_service_run(session, world)
    client = TestClient(app)
    assert client.post(f"/projects/{world.project.id}/autonomy/runs/{run.id}/pause", json={}).json()["status"] == "PAUSED"
    resumed = client.post(f"/projects/{world.project.id}/autonomy/runs/{run.id}/resume")
    assert resumed.json()["status"] == "RUNNING"


def test_steps_api_is_audit_only(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    run = create_service_run(session, world); AutonomousWorldLoopService().advance(session, run.id, request_key="steps-api")
    response = TestClient(app).get(f"/projects/{world.project.id}/autonomy/runs/{run.id}/steps")
    assert response.status_code == 200 and response.json()[0]["status"] == "COMMITTED"
    assert "prompt" not in response.text.lower() and "raw_output" not in response.text.lower()


def test_no_valid_candidate_pauses_without_scene(session, world, monkeypatch):
    monkeypatch.setattr("app.autonomy.DirectorCandidateEngine.generate", lambda *args, **kwargs: [])
    run = create_service_run(session, world)
    result = AutonomousWorldLoopService().advance(session, run.id, request_key="no-candidate")
    assert result["run"]["status"] == "PAUSED" and result["run"]["stop_reason"] == "NO_VALID_DIRECTOR_CANDIDATE"
    assert session.scalar(select(func.count(Scene.id))) == 0


def test_run_fingerprint_excludes_created_at_and_uuid(session, world):
    service = AutonomousWorldLoopService(); first = service.create_run(session, world.project.id, scene_budget=1, client_request_id="fingerprint-a")
    expected = first.autonomous_run_fingerprint
    assert expected == service._run_fingerprint(first, [])


def test_step_fingerprints_are_persisted(session, world):
    run = create_service_run(session, world); AutonomousWorldLoopService().advance(session, run.id, request_key="fingerprints")
    step = session.scalar(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id))
    assert step.step_input_fingerprint and step.step_output_fingerprint and step.step_input_fingerprint != step.step_output_fingerprint


def test_stagnation_guard_pauses_after_three_no_delta_steps(session, world):
    run = create_service_run(session, world, budget=4); service = AutonomousWorldLoopService()
    service.advance(session, run.id, max_scenes=3, request_key="stagnation")
    steps = session.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
    for step in steps:
        step.candidate_key = "same-semantic-candidate"
        step.delta_batch_ids = []
        step.resolution_count = 0
        step.stop_reason = "TURN_LIMIT"
    service._apply_stagnation_guard(session, run)
    assert run.status == AutonomousRunStatus.PAUSED and run.stop_reason == "STAGNATION_GUARD"


def test_autonomy_schema_has_runtime_fingerprint_columns(session):
    columns = {item["name"] for item in inspect(session.bind).get_columns("autonomous_world_runs")}
    step_columns = {item["name"] for item in inspect(session.bind).get_columns("autonomous_world_steps")}
    assert {"autonomous_run_fingerprint", "current_world_fingerprint"}.issubset(columns)
    assert {"step_input_fingerprint", "step_output_fingerprint"}.issubset(step_columns)
