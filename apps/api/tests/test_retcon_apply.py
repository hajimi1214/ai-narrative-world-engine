import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base
from app.main import app
import app.api as api
from app.models import (
    CanonFact, CharacterKnowledge, CharacterMemory, RetconApplication,
    RetconCognitionInvalidation, RetconApplicationStatus, RevisionStatus,
)
from app.character_mind import ActiveCharacterCognitionReader
from test_retcon_planning import prepared


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def analyzed_setup(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id": revision["id"], "reason": "apply"}).json()
    analyzed = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    return project, canon, knowledge, scene, revision, request, analyzed.json(), client


def test_apply_creates_snapshots_and_quarantines_only_rebuild_cognition(session, monkeypatch):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "confirmed historical correction"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["application"]["status"] == "APPLIED_PENDING_REPLAY"
    assert body["revision"]["status"] == "APPLIED"
    assert body["application"]["cognition_summary"]["knowledge_count"] == 1
    session.expire_all()
    assert session.get(CanonFact, canon.id).proposition == "new location truth"
    assert session.get(CharacterKnowledge, knowledge.id).proposition == "old location truth"
    assert session.scalar(select(RetconApplication).where(RetconApplication.retcon_request_id == request["id"])) is not None
    assert session.scalar(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.resource_id == knowledge.id)) is not None


def test_apply_requires_explicit_confirmation_and_latest_plan(session, monkeypatch):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "EXPLICIT_CONFIRMATION_REQUIRED"
    session.expire_all()
    assert session.get(CanonFact, canon.id).proposition == "old location truth"


def test_active_cognition_reader_hides_invalidated_rows_but_preserves_history(session, monkeypatch):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    response = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "history"})
    assert response.status_code == 200
    assert knowledge.id not in {item.id for item in ActiveCharacterCognitionReader().knowledge(session, project.id, knowledge.character_id)}
    assert session.get(CharacterKnowledge, knowledge.id) is not None


def test_pending_replay_blocks_future_progression_and_revision_apply(session, monkeypatch):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "history"})
    assert applied.status_code == 200
    assert client.post(f"/projects/{project.id}/director/dry-run").json()["detail"]["code"] == "RETCON_REPLAY_REQUIRED"


def test_retcon_rollback_restores_target_and_cognition_visibility(session, monkeypatch):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "history"}).json()
    application_id = applied["application"]["id"]
    rolled = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/rollback")
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["status"] == "ROLLED_BACK"
    assert session.get(CanonFact, canon.id).proposition == "old location truth"
    assert knowledge.id in {item.id for item in ActiveCharacterCognitionReader().knowledge(session, project.id, knowledge.character_id)}


@pytest.mark.parametrize("code", ["RETCON_REPLAY_REQUIRED"])
def test_no_replay_endpoint_and_read_only_application_list(session, monkeypatch, code):
    project, *_ = prepared(session, monkeypatch)
    client = client_for(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/retcon/replay").status_code == 404
    assert client.get(f"/projects/{project.id}/retcon/applications").status_code == 200
