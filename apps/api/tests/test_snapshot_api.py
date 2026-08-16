from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.main import app
import app.api as api

def test_snapshot_aggregates_director_context(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", test_session)
    client = TestClient(app)

    project = client.post("/projects", json={"name": "World", "creation_mode": "AUTONOMOUS"}).json()
    project_id = project["id"]
    character = client.post(f"/projects/{project_id}/characters", json={"name": "Lin", "current_state": {"location": "Harbor"}, "goals": {"current": "Leave"}}).json()
    client.post(f"/characters/{character['id']}/knowledge", json={"proposition": "The gate is watched", "status": "SUSPECTED", "confidence": 0.6})
    client.post(f"/projects/{project_id}/canon", json={"fact_type": "WORLD_FACT", "proposition": "The gate exists"})
    client.post(f"/projects/{project_id}/scenes", json={"sequence": 1, "summary": "Lin sees the gate", "status": "OCCURRED"})
    client.post(f"/projects/{project_id}/writing-bibles", json={"version": 1, "active": True, "rules": {"pov": "third"}})
    client.post(f"/projects/{project_id}/anti-ai-bibles", json={"version": 1, "active": True, "disabled_expressions": ["suddenly"]})

    snapshot = client.get(f"/projects/{project_id}/snapshot").json()
    assert snapshot["project"]["id"] == project_id
    assert snapshot["active_writing_bible"]["version"] == 1
    assert snapshot["active_anti_ai_bible"]["version"] == 1
    assert snapshot["active_characters"][0]["name"] == "Lin"
    assert snapshot["character_knowledge_summary"][0]["status"] == "SUSPECTED"
    assert snapshot["recent_scenes"][0]["sequence"] == 1

def test_locked_canon_returns_conflict(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    client = TestClient(app)
    project_id = client.post("/projects", json={"name": "World"}).json()["id"]
    canon = client.post(f"/projects/{project_id}/canon", json={"fact_type": "CORE_CANON", "proposition": "Fixed", "locked": True}).json()
    response = client.patch(f"/canon/{canon['id']}", json={"proposition": "Changed"})
    assert response.status_code == 409
