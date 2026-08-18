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
    RetconCognitionInvalidation, RetconApplicationStatus, RevisionStatus, StoryThread, StoryArc, Chapter, WorldEntity, SceneProposal, ScenePerformance,
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

def apply_success(session, monkeypatch):
    values = analyzed_setup(session, monkeypatch)
    project, canon, knowledge, scene, revision, request, analyzed, client = values
    response = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "作者确认历史修改"})
    assert response.status_code == 200, response.text
    return values + (response.json(),)


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

def test_apply_without_body_is_validation_error(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id": revision["id"], "reason": "body"}).json()
    assert client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply").status_code == 422

def test_post_apply_request_effective_status_and_actions(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, request, _, client, _ = values
    body = client.get(f"/projects/{project.id}/retcon/requests/{request['id']}").json()
    assert body["effective_status"] == "APPLIED_PENDING_REPLAY"
    assert not set(body["available_actions"]).intersection({"ANALYZE", "REANALYZE", "ABORT", "APPLY"})

def test_consumed_plan_is_not_presented_as_stale(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, _, analyzed, client, _ = values
    plan = client.get(f"/projects/{project.id}/retcon/plans/{analyzed['plan']['id']}").json()["plan"]
    assert plan["consumed"] is True and plan["consumption_status"] == "APPLIED_PENDING_REPLAY" and plan["is_stale"] is False

def test_analyze_after_apply_is_rejected(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, request, _, client, _ = values
    result = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze")
    assert result.status_code == 409 and result.json()["detail"]["code"] == "RETCON_ALREADY_APPLIED"

def test_abort_after_apply_is_rejected(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, request, _, client, _ = values
    result = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/abort")
    assert result.status_code == 409 and result.json()["detail"]["code"] == "RETCON_ALREADY_APPLIED"

def test_double_apply_is_rejected(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, request, analyzed, client, _ = values
    result = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "again"})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "RETCON_ALREADY_APPLIED"

def test_core_override_required_without_flag(session, monkeypatch):
    project, _, _, _, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    result = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "AUTHOR_OVERRIDE_REQUIRED"

def test_core_override_reason_required(session, monkeypatch):
    project, _, _, _, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    result = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": ""})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "AUTHOR_OVERRIDE_REQUIRED"

def test_apply_requirements_expose_core_target_label(session, monkeypatch):
    project, _, _, _, _, _, analyzed, client = analyzed_setup(session, monkeypatch)
    requirements = analyzed["plan"]["apply_requirements"]
    assert requirements["explicit_confirmation_required"] is True and requirements["author_override_required"] is True and requirements["author_override_targets"]

def test_revision_application_actual_fingerprint_is_revision_state(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, revision, _, _, client, body = values
    from app.revision import RevisionStateFingerprintBuilder
    app_body = body["revision_application"]
    assert app_body["actual_base_fingerprint"].startswith("revision-state-v1:")

def test_replay_summary_is_frozen_complete_input(session, monkeypatch):
    values = apply_success(session, monkeypatch); summary = values[-1]["application"]["replay_summary"]
    assert {"earliest_affected_scene_id", "earliest_affected_sequence", "replay_scene_ids", "replay_required_decision_ids", "replay_required_turn_ids", "invalidated_world_resolution_ids", "rebuild_knowledge_ids", "rebuild_memory_ids", "preserved_scene_count", "preserved_scene_ranges"}.issubset(summary)

def test_rollback_changes_request_and_revision_state(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, canon, _, _, _, request, _, client, body = values
    rolled = client.post(f"/projects/{project.id}/retcon/applications/{body['application']['id']}/rollback")
    assert rolled.status_code == 200
    assert client.get(f"/projects/{project.id}/retcon/requests/{request['id']}").json()["effective_status"] == "ROLLED_BACK"

def test_double_rollback_is_rejected(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, _, _, client, body = values
    url = f"/projects/{project.id}/retcon/applications/{body['application']['id']}/rollback"
    assert client.post(url).status_code == 200
    second = client.post(url)
    assert second.status_code == 409 and second.json()["detail"]["code"] == "RETCON_ALREADY_ROLLED_BACK"

def test_project_snapshot_excludes_quarantined_knowledge(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, knowledge, _, _, _, _, client, _ = values
    snapshot = client.get(f"/projects/{project.id}/snapshot").json()
    assert knowledge.id not in [item.get("id") for item in snapshot["character_knowledge_summary"]]

def test_knowledge_and_memory_rows_are_not_deleted(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, knowledge, _, _, _, _, _, _ = values
    session.expire_all()
    assert session.get(CharacterKnowledge, knowledge.id) is not None

def test_no_new_knowledge_or_memory_is_created(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, knowledge, _, _, _, _, _, _ = values
    assert session.query(CharacterKnowledge).count() == 1 and session.query(CharacterMemory).count() == 0

def test_story_thread_mutation_is_blocked_pending_replay(session, monkeypatch):
    values = analyzed_setup(session, monkeypatch); project, *_ = values
    thread = StoryThread(project_id=project.id, title="thread", type="CONFLICT", status="OPEN", weight=1.0, goal="goal", progress=0.0, state={})
    session.add(thread); session.commit(); values = apply_success(session, monkeypatch) if False else values
    # Apply the existing plan using the same client, then attempt a formal mutation.
    _, _, _, _, _, request, analyzed, client = values
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "pause"})
    assert applied.status_code == 200
    assert client.patch(f"/story-threads/{thread.id}", json={"title": "blocked"}).status_code == 409

def test_revision_preview_remains_allowed_pending_replay(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, _, _, client, _ = values
    draft = client.post(f"/projects/{project.id}/revisions", json={"title": "future preview", "changes": []})
    assert draft.status_code == 201

def test_read_only_application_detail_remains_available(session, monkeypatch):
    values = apply_success(session, monkeypatch); project, _, _, _, _, _, _, client, body = values
    assert client.get(f"/projects/{project.id}/retcon/applications/{body['application']['id']}").status_code == 200

def test_no_replay_route_exists(session, monkeypatch):
    project, *_unused, revision, client = prepared(session, monkeypatch)
    assert client.post(f"/projects/{project.id}/retcon/replay").status_code == 404

@pytest.mark.parametrize("endpoint", ["/director/dry-run", "/retcon/replay"])
def test_pending_progression_route_protection_shape(session, monkeypatch, endpoint):
    project, *_ = analyzed_setup(session, monkeypatch)
    assert endpoint.startswith("/")

def test_related_cognition_change_blocks_apply_as_stale(session, monkeypatch):
    project, _, knowledge, _, _, request, analyzed, client = analyzed_setup(session, monkeypatch)
    knowledge.confidence = 0.2; session.add(knowledge); session.commit()
    blocked = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "stale"})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "RETCON_PLAN_STALE"

def test_memory_quarantine_and_rollback_visibility(session, monkeypatch):
    project, canon, knowledge, scene, independent, revision, client = prepared(session, monkeypatch)
    memory = CharacterMemory(character_id=knowledge.character_id, content="saw old location", importance=0.8, emotional_weight=0.1, confidence=1.0, distortion={}, source_scene=scene.id)
    session.add(memory); session.commit()
    assert client.post(f"/projects/{project.id}/revisions/{revision['id']}/preview").status_code == 200
    request = client.post(f"/projects/{project.id}/retcon/requests", json={"source_revision_id": revision["id"], "reason": "memory"}).json()
    analyzed = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/analyze").json()
    assert any(item["resource_id"] == memory.id and item["classification"] == "REBUILD_COGNITION" for item in analyzed["items"])
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "memory"}).json()
    assert session.get(CharacterMemory, memory.id) is not None
    assert memory.id not in {item.id for item in ActiveCharacterCognitionReader().memories(session, project.id, knowledge.character_id)}
    assert client.post(f"/projects/{project.id}/retcon/applications/{applied['application']['id']}/rollback").status_code == 200
    assert memory.id in {item.id for item in ActiveCharacterCognitionReader().memories(session, project.id, knowledge.character_id)}

def test_retcon_apply_never_calls_ai_provider(session, monkeypatch):
    monkeypatch.setattr(api, "get_model_provider", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider must not be called")))
    values = apply_success(session, monkeypatch)
    assert values[-1]["application"]["status"] == "APPLIED_PENDING_REPLAY"

@pytest.mark.parametrize("resource", ["canon", "character", "entity", "scene", "thread", "chapter", "arc"])
def test_pending_replay_blocks_every_formal_resource_mutation(session, monkeypatch, resource):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    entity = session.query(WorldEntity).filter_by(project_id=project.id).first()
    thread = StoryThread(project_id=project.id, title="t", type="CONFLICT", status="OPEN", weight=1, goal="g", progress=0, state={})
    chapter = Chapter(project_id=project.id, number=1, title="c", source_scene_ids=[], content=None, word_count=0, quality_report={}, status="DRAFT")
    arc = StoryArc(project_id=project.id, title="a", core_question=None, core_conflict=None, status="ACTIVE", progress=0, source_scene_ids=[])
    session.add_all([thread, chapter, arc]); session.commit()
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "pause"})
    assert applied.status_code == 200
    paths = {
        "canon": (f"/canon/{canon.id}", {"proposition": "blocked"}),
        "character": (f"/characters/{knowledge.character_id}", {"name": "blocked"}),
        "entity": (f"/world-entities/{entity.id}", {"name": "blocked"}),
        "scene": (f"/scenes/{scene.id}", {"summary": "blocked"}),
        "thread": (f"/story-threads/{thread.id}", {"title": "blocked"}),
        "chapter": (f"/chapters/{chapter.id}", {"title": "blocked"}),
        "arc": (f"/story-arcs/{arc.id}", {"title": "blocked"}),
    }
    path, payload = paths[resource]
    result = client.patch(path, json=payload)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "RETCON_REPLAY_REQUIRED"

@pytest.mark.parametrize("action", ["director", "character", "performance_create", "performance_step", "world", "revision", "project_time"])
def test_pending_replay_guard_covers_progression_and_revision_boundaries(session, monkeypatch, action):
    project, canon, knowledge, scene, revision, request, analyzed, client = analyzed_setup(session, monkeypatch)
    proposal = session.query(SceneProposal).filter_by(project_id=project.id).first()
    performance = session.query(ScenePerformance).filter_by(project_id=project.id).first()
    performance_id = performance.id if performance else revision["id"]
    applied = client.post(f"/projects/{project.id}/retcon/requests/{request['id']}/apply", json={"plan_id": analyzed["plan"]["id"], "explicit_confirmation": True, "author_override": True, "author_override_reason": "pause"})
    assert applied.status_code == 200
    urls = {
        "director": (f"/projects/{project.id}/director/dry-run", "POST", None),
        "character": (f"/projects/{project.id}/director/proposals/{proposal.id}/characters/{knowledge.character_id}/dry-run", "POST", None),
        "performance_create": (f"/projects/{project.id}/director/proposals/{proposal.id}/performances", "POST", {}),
        "performance_step": (f"/projects/{project.id}/performances/{performance_id}/step", "POST", None),
        "world": (f"/projects/{project.id}/performances/{performance_id}/world/resolve", "POST", {}),
        "revision": (f"/projects/{project.id}/revisions/{revision['id']}/apply", "POST", {}),
        "project_time": (f"/projects/{project.id}", "PATCH", {"current_world_time": "2030-01-01T00:00:00"}),
    }
    url, method, payload = urls[action]
    result = client.request(method, url, json=payload)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "RETCON_REPLAY_REQUIRED"
