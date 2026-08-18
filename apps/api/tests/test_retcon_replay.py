import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base
from app.main import app
import app.api as api
from app.models import RetconReplaySession, ReplaySceneRun, RetconApplication, RetconApplicationStatus, Scene, WorldSnapshot, SceneStateCheckpoint
from app.historical import SceneStateCheckpointService
from app.historical import TemporalCharacterCognitionReader
from app.character_mind import ActiveCharacterCognitionReader
from app.models import RetconCognitionInvalidation, RetconCognitionInvalidationStatus
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

def test_create_replay_session_requires_pending_application(session, monkeypatch):
    project, *_unused, revision, client = __import__("test_retcon_planning", fromlist=["prepared"]).prepared(session, monkeypatch)
    result = client.post(f"/projects/{project.id}/retcon/applications/not-an-application/replay-sessions")
    assert result.status_code == 409

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
    assert result.status_code == 201, result.text
    assert scene.id not in {item["scene_id"] for item in result.json()["queue"]}
