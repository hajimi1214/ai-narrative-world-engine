import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from app.db import Base
from app.main import app
import app.api as api
from app.models import CanonFact, CharacterKnowledge, Scene, WorldRevision
from test_scene_performance import approved_setup
from app.revision import StructuredReferenceScanner, RevisionStateFingerprintBuilder

@pytest.fixture()
def session():
    engine=create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool); Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db: yield db

def client_for(session, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)); return TestClient(app)

def make_revision(client, project_id, change):
    return client.post(f"/projects/{project_id}/revisions", json={"title":"revision","changes":[change]})

def test_revision_preview_is_draft_then_preview_and_does_not_mutate_target(session, monkeypatch):
    project, _, actor, _, _, _ = approved_setup(session, monkeypatch); client=client_for(session, monkeypatch)
    before=actor.goals.copy(); response=make_revision(client, project.id, {"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/goals/current","value":"new goal"})
    assert response.status_code==201 and response.json()["status"]=="DRAFT"
    preview=client.post(f"/projects/{project.id}/revisions/{response.json()['id']}/preview")
    assert preview.status_code==200 and preview.json()["status"]=="PREVIEWED" and session.get(type(actor),actor.id).goals==before

def test_merge_remove_and_revision_guards(session, monkeypatch):
    project, location, actor, _, _, _ = approved_setup(session, monkeypatch); client=client_for(session, monkeypatch)
    good=make_revision(client,project.id,{"target_type":"WORLD_ENTITY","target_id":location.id,"operation":"MERGE","path":"/profile","value":{"locked":True}}); assert client.post(f"/projects/{project.id}/revisions/{good.json()['id']}/preview").status_code==200
    for change, code in [({"target_type":"CHARACTER","target_id":actor.id,"operation":"MERGE","path":"/name","value":{}},"INVALID_MERGE_TARGET"),({"target_type":"CHARACTER","target_id":actor.id,"operation":"REMOVE","path":"/name"},"REQUIRED_FIELD_REMOVAL"),({"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/id","value":"x"},"IMMUTABLE_FIELD")]:
        assert client.post(f"/projects/{project.id}/revisions",json={"title":"x","changes":[change]}).json()["detail"]["code"]==code

def test_canon_preview_reports_knowledge_reveal_scene_and_locked_override(session, monkeypatch):
    project, _, actor, _, _, _ = approved_setup(session, monkeypatch); client=client_for(session, monkeypatch)
    canon=CanonFact(project_id=project.id,fact_type="CORE_CANON",proposition="old truth",data={},locked=True); session.add(canon); session.flush()
    knowledge=CharacterKnowledge(character_id=actor.id,proposition="old truth",status="KNOWN"); scene=Scene(project_id=project.id,sequence=1,participants=[actor.id],facts=["old truth"],result={}); session.add_all([knowledge,scene]); session.commit()
    from app.models import RevealConstraint
    session.add(RevealConstraint(project_id=project.id,canon_fact_id=canon.id,status="LOCKED",allowed_character_ids=[])); session.commit()
    revision=make_revision(client,project.id,{"target_type":"CANON_FACT","target_id":canon.id,"operation":"SET","path":"/proposition","value":"new truth"}).json()
    report=client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").json()["impact_report"]
    categories={item["category"] for item in report["impacts"]}
    assert report["author_override_required"] and {"KNOWLEDGE_DEPENDENCY","REVEAL_OR_FORESHADOWING","SCENE_HISTORY"}.issubset(categories)

def test_conflicting_paths_cross_project_and_cancelled_preview(session, monkeypatch):
    project, _, actor, _, _, _=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    conflict=client.post(f"/projects/{project.id}/revisions",json={"title":"x","changes":[{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/profile","value":{}},{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/profile/x","value":1}]}); assert conflict.json()["detail"]["code"]=="CONFLICTING_CHANGE"
    revision=make_revision(client,project.id,{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/name","value":"x"}).json(); client.post(f"/projects/{project.id}/revisions/{revision['id']}/cancel")
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").status_code==409

def test_multi_change_virtual_target_and_reference_safety(session, monkeypatch):
    project, location, actor, other, _, _=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    changes=[{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/profile/a","value":1},{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/profile/b","value":{"entity_id":location.id}}]
    revision=client.post(f"/projects/{project.id}/revisions",json={"title":"x","changes":changes}).json(); preview=client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").json()
    normalized=preview["normalized_changes"]; assert normalized[0]["target_fingerprint_after"]==normalized[1]["target_fingerprint_after"]
    bad=client.post(f"/projects/{project.id}/revisions",json={"title":"bad","changes":[{"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/profile/ref","value":{"entity_id":"missing"}}]})
    assert bad.json()["detail"]["code"]=="UNKNOWN_REFERENCE"

def test_pointer_and_target_schema_are_strict(session, monkeypatch):
    project, _, actor, _, _, _=approved_setup(session,monkeypatch); client=client_for(session,monkeypatch)
    for change, code in [({"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/name","value":None},"INVALID_TARGET_STATE"),({"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/abilities/99","value":{}},"INVALID_PATH"),({"target_type":"CHARACTER","target_id":actor.id,"operation":"SET","path":"/profile/~2bad","value":1},"INVALID_PATH")]:
        response=client.post(f"/projects/{project.id}/revisions",json={"title":"x","changes":[change]}); assert response.json()["detail"]["code"]==code

def test_impact_scanner_exact_values_keys_and_pointer_escape():
    target="id/with~chars"; value={"relationships":{target:{"note":"x"}},"text":f"owner is {target}","items":[target]}
    paths=StructuredReferenceScanner().paths(value,target)
    assert "/relationships/id~1with~0chars" in paths and "/items/0" in paths and not any(path.startswith("/text") for path in paths)

def test_revision_state_fingerprint_ignores_revision_and_prose_derivatives(session, monkeypatch):
    project, _, actor, _, _, _=approved_setup(session,monkeypatch)
    first=RevisionStateFingerprintBuilder().build(session,project.id)
    revision=WorldRevision(project_id=project.id,title="x",change_set=[],normalized_changes=[],impact_report={}); session.add(revision); session.commit()
    assert RevisionStateFingerprintBuilder().build(session,project.id)==first
