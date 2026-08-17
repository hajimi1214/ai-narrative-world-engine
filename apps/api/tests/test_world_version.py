import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from app.db import Base
from app.main import app
import app.api as api
from test_scene_performance import approved_setup

@pytest.fixture()
def session():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool); Base.metadata.create_all(engine)
    with sessionmaker(bind=engine,autoflush=False,expire_on_commit=False)() as db: yield db
def client_for(session,monkeypatch): monkeypatch.setattr(api,"SessionLocal",sessionmaker(bind=session.bind,autoflush=False,expire_on_commit=False)); return TestClient(app)
def test_baseline_snapshot_excludes_rehearsal_and_content(session,monkeypatch):
    project,_,_,_,_,_=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch); body=client.post(f"/projects/{project.id}/snapshots",json={"snapshot_type":"BASELINE"}).json()
    assert body["snapshot_type"]=="BASELINE" and "scene_performances" not in body["payload"] and "chapters" in body["payload"]
def test_model_config_rejects_secrets(session,monkeypatch):
    project,_,_,_,_,_=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    assert client.put(f"/projects/{project.id}/model-config",json={"character_model":"x"}).status_code==200
    assert client.put(f"/projects/{project.id}/model-config",json={"api_key":"bad"}).status_code==400
