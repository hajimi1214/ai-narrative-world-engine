import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from app.db import Base
from app.main import app
import app.api as api
from test_scene_performance import approved_setup
from app.revision import RevisionPatchEngine
from app.execution_trace import RecoveryPolicy, TraceSanitizer
from app.models import ExecutionStage, ExecutionTrace, ExecutionStatus
from app.execution_trace import ExecutionTraceRecorder
from app.settings import get_settings
from app.model_router import ModelRouter

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

def test_patch_engine_honors_rfc6901_escaped_keys():
    value={"profile":{"a/b":{"x~y":1}}}
    before, after=RevisionPatchEngine().apply(value,"SET","/profile/a~1b/x~0y",2)
    assert (before,after,value["profile"]["a/b"]["x~y"]) == (1,2,2)

def test_trace_policy_and_sanitizer_do_not_retain_secrets():
    assert RecoveryPolicy.resolve("MODEL_UPSTREAM_ERROR")[:2] == (True,False)
    assert RecoveryPolicy.resolve("MODEL_OUTPUT_INVALID")[:2] == (False,True)
    assert TraceSanitizer.clean({"prompt":"hidden","api_key":"hidden","safe":{"headers":"x","code":"OK"}}) == {"safe":{"code":"OK"}}

@pytest.mark.parametrize("kind",["PRE_REVISION","POST_REVISION","ROLLBACK_POINT"])
def test_manual_system_snapshot_types_are_forbidden(session,monkeypatch,kind):
    project,_,_,_,_,_=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    response=client.post(f"/projects/{project.id}/snapshots",json={"snapshot_type":kind})
    assert response.status_code==409 and response.json()["detail"]["code"]=="SYSTEM_SNAPSHOT_TYPE_FORBIDDEN"

def test_trace_single_attempt_transitions_in_place(session,monkeypatch):
    project,_,_,_,_,_=approved_setup(session,monkeypatch)
    trace=ExecutionTraceRecorder().start(session,project_id=project.id,stage=ExecutionStage.CHARACTER_ACTOR,source_type="CHARACTER",source_id="source",provider="openai_compatible",model="model",input_fingerprint="input")
    ExecutionTraceRecorder().fail(trace,"MODEL_TIMEOUT",upstream_status=503); session.commit()
    rows=session.query(ExecutionTrace).all()
    assert len(rows)==1 and rows[0].status==ExecutionStatus.FAILED and rows[0].provider=="openai_compatible" and rows[0].input_fingerprint=="input"

def test_trace_security_normalizes_key_variants():
    payload={"API-KEY":"x","Authorization Header":"x","raw response":"x","safe":"ok"}
    assert TraceSanitizer.clean(payload)=={"safe":"ok"}

def test_recovery_policy_world_context_stale():
    assert RecoveryPolicy.resolve("WORLD_CONTEXT_STALE") == (True,False,["RETRY","ABORT"])

def test_model_router_uses_settings_fallback(session,monkeypatch):
    project,_,_,_,_,_=approved_setup(session,monkeypatch)
    route=ModelRouter().resolve(session,project.id,get_settings(),"CHARACTER")
    assert route.model==get_settings().ai_character_model

def test_applied_revision_cannot_preview_or_cancel(session,monkeypatch):
    project,_,actor,_,_,_=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    revision=client.post(f"/projects/{project.id}/revisions",json={"title":"apply","changes":[{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/name","value":"changed"}]}).json()
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").status_code==200
    applied=client.post(f"/projects/{project.id}/revisions/{revision['id']}/apply",json={})
    assert applied.status_code==200, applied.json()
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").json()["detail"]["code"]=="REVISION_ALREADY_APPLIED"
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/cancel").json()["detail"]["code"]=="REVISION_ALREADY_APPLIED"
    snapshots=client.get(f"/projects/{project.id}/snapshots").json()
    assert {item["snapshot_type"] for item in snapshots}>={"PRE_REVISION","POST_REVISION"}
    assert all(item["source_revision_id"]==revision["id"] for item in snapshots if item["snapshot_type"]!="BASELINE")

def test_trace_status_query_alias(session,monkeypatch):
    project,_,_,_,_,_=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    trace=ExecutionTraceRecorder().start(session,project_id=project.id,stage=ExecutionStage.CHARACTER_ACTOR,source_type="CHARACTER",source_id="source")
    ExecutionTraceRecorder().block(trace,"MODEL_OUTPUT_INVALID"); session.commit()
    rows=client.get(f"/projects/{project.id}/execution-traces",params={"status":"BLOCKED"}).json()
    assert len(rows)==1 and rows[0]["error_code"]=="MODEL_OUTPUT_INVALID" and rows[0]["available_actions"]==["RETRY", "ABORT"]
