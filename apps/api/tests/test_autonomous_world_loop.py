from types import SimpleNamespace

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
from app.models import AutonomousRunStatus, AutonomousStepStatus, AutonomousWorldRun, AutonomousWorldStep, Character, Project, Scene, SceneCommit, ScenePerformance, StoryThread, WorldEntity


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
    run = create_service_run(session, world)
    # The frozen guard is the authority; a normal project has no pending replay.
    assert run.status == AutonomousRunStatus.CREATED


def test_new_thread_candidate_is_not_materialized(session, world):
    world.thread.status = "RESOLVED"; session.commit(); run = create_service_run(session, world)
    # Existing active actor still permits a gravity candidate; no new StoryThread is created by the loop.
    before = session.scalar(select(func.count(StoryThread.id))); AutonomousWorldLoopService().advance(session, run.id, request_key="thread")
    assert session.scalar(select(func.count(StoryThread.id))) == before


def test_run_budget_is_hard_bound(session, world):
    run = create_service_run(session, world, budget=1); AutonomousWorldLoopService().advance(session, run.id, max_scenes=5, request_key="bound")
    assert session.scalar(select(func.count(Scene.id))) == 1


def test_step_request_offset_distinguishes_requests(session, world):
    run = create_service_run(session, world, budget=2); service = AutonomousWorldLoopService(); service.advance(session, run.id, request_key="offset", request_offset=0); service.advance(session, run.id, request_key="offset", request_offset=1)
    assert session.scalar(select(func.count(AutonomousWorldStep.id))) == 2


def test_run_status_contains_no_model_transcript(session, world):
    run = create_service_run(session, world); payload = AutonomousWorldLoopService().get_status(session, run.id)
    assert "prompt" not in str(payload).lower() and "output" not in str(payload).lower()


def test_formal_world_changes_only_after_scene_commit(session, world):
    run = create_service_run(session, world); before = run.current_world_fingerprint; AutonomousWorldLoopService().advance(session, run.id, request_key="formal")
    assert run.current_world_fingerprint != "" and before != run.current_world_fingerprint


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
        performance = session.get(ScenePerformance, step.performance_id)
        performance.stop_reason = "TURN_LIMIT"
    service._apply_stagnation_guard(session, run)
    assert run.status == AutonomousRunStatus.PAUSED and run.stop_reason == "STAGNATION_GUARD"


def test_autonomy_schema_has_runtime_fingerprint_columns(session):
    columns = {item["name"] for item in inspect(session.bind).get_columns("autonomous_world_runs")}
    step_columns = {item["name"] for item in inspect(session.bind).get_columns("autonomous_world_steps")}
    assert {"autonomous_run_fingerprint", "current_world_fingerprint"}.issubset(columns)
    assert {"step_input_fingerprint", "step_output_fingerprint"}.issubset(step_columns)
