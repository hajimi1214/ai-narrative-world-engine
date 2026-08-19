import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.director import (
    DirectorCandidatePayload, DirectorConstraintChecker, DirectorModelContextSanitizer,
    DirectorCandidateEngine, DirectorProposalFactory, LLMDirectorCandidateGenerator,
    StoryGravityContextBuilder, StoryGravityEngine,
)
from app.main import app
import app.api as api
from app.models import Character, CanonFact, CanonType, Project, ProposalType, RevealConstraint, RevealStatus, Scene, SceneStatus, StoryThread, ThreadStatus, TimelineEvent, TimelineEventType, TimelineOrigin, WorldEntity
from app.ai.fake import FakeModelProvider


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


@pytest.fixture()
def world(session):
    project = Project(name="Gravity")
    session.add(project); session.flush()
    a = WorldEntity(project_id=project.id, entity_type="LOCATION", name="A")
    b = WorldEntity(project_id=project.id, entity_type="LOCATION", name="B")
    lead = Character(project_id=project.id, name="Lead", current_state={"location_id": a.id}, goals={"current": "goal"}, narrative_relevance={"score": 4})
    support = Character(project_id=project.id, name="Support", current_state={"location_id": a.id}, narrative_relevance={"score": 1})
    thread = StoryThread(project_id=project.id, title="Thread", type="MYSTERY", weight=5, progress=0.2)
    paused = StoryThread(project_id=project.id, title="Paused", type="MYSTERY", weight=999, status=ThreadStatus.PAUSED)
    session.add_all([a, b, lead, support, thread, paused]); session.commit()
    return SimpleNamespace(project=project, a=a, b=b, lead=lead, support=support, thread=thread, paused=paused)


def gravity(session, world):
    return StoryGravityEngine().build(StoryGravityContextBuilder().build(session, world.project.id))


def test_context_protocol_and_sequence(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id)
    assert context["protocol_version"] == "story-gravity-context-v1" and context["current_sequence"] == 0


def test_current_history_excludes_planned_and_superseded(session, world):
    session.add_all([Scene(project_id=world.project.id, sequence=1, status=SceneStatus.PLANNED), Scene(project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED, history_status="SUPERSEDED")]); session.commit()
    assert StoryGravityContextBuilder().build(session, world.project.id)["scenes"] == []


def test_open_thread_has_gravity(session, world):
    report = gravity(session, world)
    assert any(row["thread_id"] == world.thread.id for row in report.thread_gravity)


def test_paused_thread_is_background_only(session, world):
    report = gravity(session, world)
    assert all(row["thread_id"] != world.paused.id for row in report.thread_gravity)


def test_thread_weight_affects_score(session, world):
    other = StoryThread(project_id=world.project.id, title="Other", type="MYSTERY", weight=1)
    session.add(other); session.commit()
    rows = {row["thread_id"]: row for row in gravity(session, world).thread_gravity}
    assert rows[world.thread.id]["score_components"]["base_weight"] > rows[other.id]["score_components"]["base_weight"]


def test_thread_staleness_increases_with_sequence(session, world):
    session.add(Scene(project_id=world.project.id, sequence=5, status=SceneStatus.OCCURRED, story_threads=[])); session.commit()
    report = gravity(session, world)
    assert report.current_sequence == 5 and report.thread_gravity[0]["staleness"] == 6


def test_thread_recent_repetition_penalty(session, world):
    session.add_all([Scene(project_id=world.project.id, sequence=1, status=SceneStatus.OCCURRED, story_threads=[world.thread.id]), Scene(project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED, story_threads=[world.thread.id])]); session.commit()
    row = next(row for row in gravity(session, world).thread_gravity if row["thread_id"] == world.thread.id)
    assert row["score_components"]["recent_repetition_penalty"] < 0


def test_never_touched_thread_is_finite(session, world):
    row = next(row for row in gravity(session, world).thread_gravity if row["thread_id"] == world.thread.id)
    assert row["staleness"] == 1 and row["thread_gravity_score"] < 100


def test_progress_signal_is_bounded(session, world):
    world.thread.progress = 1.0; session.commit()
    row = next(row for row in gravity(session, world).thread_gravity if row["thread_id"] == world.thread.id)
    assert 0 <= row["score_components"]["progress_pressure"] <= 1


def test_character_goal_signal(session, world):
    row = next(row for row in gravity(session, world).character_gravity if row["character_id"] == world.lead.id)
    assert row["score_components"]["goal_pressure"] > 0


def test_character_absence_signal(session, world):
    session.add(Scene(project_id=world.project.id, sequence=4, status=SceneStatus.OCCURRED, participants=[])); session.commit()
    row = next(row for row in gravity(session, world).character_gravity if row["character_id"] == world.lead.id)
    assert row["score_components"]["absence"] > 0


def test_character_overuse_penalty(session, world):
    session.add_all([Scene(project_id=world.project.id, sequence=1, status=SceneStatus.OCCURRED, participants=[world.lead.id]), Scene(project_id=world.project.id, sequence=2, status=SceneStatus.OCCURRED, participants=[world.lead.id])]); session.commit()
    row = next(row for row in gravity(session, world).character_gravity if row["character_id"] == world.lead.id)
    assert row["score_components"]["overuse_penalty"] < 0


def test_context_has_structured_belief_metadata(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id)
    assert "belief_conflict_count" in context["characters"][0]


def test_report_fingerprint_is_deterministic(session, world):
    assert gravity(session, world).gravity_fingerprint == gravity(session, world).gravity_fingerprint


def test_summary_is_not_gravity_authority(session, world):
    scene = Scene(project_id=world.project.id, sequence=1, status=SceneStatus.OCCURRED, summary="one", story_threads=[world.thread.id]); session.add(scene); session.commit()
    before = gravity(session, world).gravity_fingerprint
    scene.summary = "different prose"; session.commit()
    assert gravity(session, world).gravity_fingerprint == before


def test_candidate_key_is_semantic(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context)
    candidates = DirectorCandidateEngine().generate(context, report)
    assert candidates and all("|" in candidate.candidate_key for candidate in candidates)


def test_candidate_ranking_is_stable(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context); engine = DirectorCandidateEngine()
    assert [item.candidate_key for item in engine.rank(engine.generate(context, report))] == [item.candidate_key for item in engine.rank(engine.generate(context, report))]


def test_continue_thread_candidate_exists(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context)
    assert any(item.proposal_type == ProposalType.CONTINUE_THREAD.value for item in DirectorCandidateEngine().generate(context, report))


def test_character_candidate_exists(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context)
    assert any(item.proposal_type == ProposalType.CHARACTER_DRIVEN.value for item in DirectorCandidateEngine().generate(context, report))


def test_new_thread_is_fallback_only(session, world):
    world.thread.status = ThreadStatus.RESOLVED; session.commit()
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context)
    context["characters"] = []; report = StoryGravityEngine().build(context)
    assert any(item.proposal_type == ProposalType.NEW_THREAD.value for item in DirectorCandidateEngine().generate(context, report))


def test_available_reveal_candidate_is_authorized(session, world):
    fact = CanonFact(project_id=world.project.id, fact_type=CanonType.SECRET_CANON, proposition="secret")
    session.add(fact); session.flush(); session.add(RevealConstraint(project_id=world.project.id, canon_fact_id=fact.id, status=RevealStatus.AVAILABLE, allowed_character_ids=[world.lead.id])); session.commit()
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context)
    candidate = next(item for item in DirectorCandidateEngine().generate(context, report) if item.proposal_type == ProposalType.REVEAL.value)
    assert candidate.participant_ids == (world.lead.id,)


def test_locked_reveal_is_not_candidate(session, world):
    fact = CanonFact(project_id=world.project.id, fact_type=CanonType.SECRET_CANON, proposition="secret")
    session.add(fact); session.flush(); session.add(RevealConstraint(project_id=world.project.id, canon_fact_id=fact.id, status=RevealStatus.LOCKED, allowed_character_ids=[world.lead.id])); session.commit()
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context)
    assert not any(item.proposal_type == ProposalType.REVEAL.value for item in DirectorCandidateEngine().generate(context, report))


def test_factory_preserves_autonomy(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context); candidate = DirectorCandidateEngine().select(DirectorCandidateEngine().generate(context, report)); payload = DirectorProposalFactory().create(world.project.id, context, report, candidate)
    assert "required_action" not in json.dumps(payload) and len(payload["possible_outcomes"]) >= 2


def test_factory_writes_gravity_metadata(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context); candidate = DirectorCandidateEngine().select(DirectorCandidateEngine().generate(context, report)); payload = DirectorProposalFactory().create(world.project.id, context, report, candidate)
    assert payload["entry_state"]["director_meta"]["gravity_fingerprint"] == report.gravity_fingerprint


def test_model_sanitizer_hides_memory_and_prose(session, world):
    safe = DirectorModelContextSanitizer().sanitize({"project": {"id": world.project.id, "story_seed": "seed"}, "characters": [], "story_threads": [], "recent_scene_signatures": []})
    assert "content" not in json.dumps(safe) and "summary" not in json.dumps(safe)


def test_ai_contract_forbids_self_score():
    with pytest.raises(Exception): DirectorCandidatePayload.model_validate({"proposal_type": "NEW_THREAD", "scene_goal": "x", "planned_pressure": "y", "reasoning_summary": "z", "score": 1000})


def test_ai_contract_requires_reasoning():
    with pytest.raises(Exception): DirectorCandidatePayload.model_validate({"proposal_type": "NEW_THREAD", "scene_goal": "x", "planned_pressure": "y"})


def test_ai_foreign_reference_blocks(session, world):
    payload = DirectorCandidatePayload(proposal_type=ProposalType.CONTINUE_THREAD, primary_thread_id="foreign", scene_goal="x", planned_pressure="y", reasoning_summary="z")
    context = StoryGravityContextBuilder().build(session, world.project.id)
    assert "INVALID_GRAVITY_REFERENCE" in LLMDirectorCandidateGenerator().validate_references(payload, context)


def test_ai_foreign_participant_blocks(session, world):
    payload = DirectorCandidatePayload(proposal_type=ProposalType.CHARACTER_DRIVEN, participants=["foreign"], scene_goal="x", planned_pressure="y", reasoning_summary="z")
    context = StoryGravityContextBuilder().build(session, world.project.id)
    assert "INVALID_GRAVITY_REFERENCE" in LLMDirectorCandidateGenerator().validate_references(payload, context)


def test_ai_puppeteering_blocks(session, world):
    payload = DirectorCandidatePayload(proposal_type=ProposalType.CHARACTER_DRIVEN, participants=[world.lead.id], scene_goal="x", planned_pressure="y", expected_progress={"required_action": "win"}, reasoning_summary="z")
    context = StoryGravityContextBuilder().build(session, world.project.id)
    assert "DIRECTOR_CHARACTER_PUPPETEERING" in LLMDirectorCandidateGenerator().validate_references(payload, context)


def test_gravity_api_is_read_only(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    before = session.scalar(select(func.count(Scene.id)))
    response = TestClient(app).get(f"/projects/{world.project.id}/director/gravity")
    assert response.status_code == 200 and session.scalar(select(func.count(Scene.id))) == before


def test_gravity_api_hides_secret_proposition(session, world, monkeypatch):
    fact = CanonFact(project_id=world.project.id, fact_type=CanonType.SECRET_CANON, proposition="DO NOT LEAK")
    session.add(fact); session.commit(); monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    assert "DO NOT LEAK" not in TestClient(app).get(f"/projects/{world.project.id}/director/gravity").text


def test_ai_director_is_candidate_only_with_fake_provider(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    response = {"proposal_type": "CONTINUE_THREAD", "primary_thread_id": world.thread.id, "location_id": world.a.id, "participants": [world.lead.id], "scene_goal": "Create an opportunity", "planned_pressure": "External pressure", "expected_progress": {"thread": world.thread.id}, "allowed_reveals": [], "required_knowledge": {}, "possible_outcomes": ["wait", "act"], "reasoning_summary": "structured candidate"}
    monkeypatch.setattr(api, "get_model_provider", lambda settings, provider=None, base_url=None: FakeModelProvider(json.dumps(response)))
    result = TestClient(app).post(f"/projects/{world.project.id}/director/ai-dry-run")
    assert result.status_code == 200 and result.json()["authority"] == "CANDIDATE_ONLY"
    assert session.scalar(select(func.count(Scene.id))) == 0


def test_dry_run_returns_selected_candidate(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    response = TestClient(app).post(f"/projects/{world.project.id}/director/dry-run")
    assert response.status_code == 201 and response.json()["selected_candidate"]["candidate_key"]


def test_dry_run_does_not_create_scene(session, world, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False)); before = session.scalar(select(func.count(Scene.id)))
    TestClient(app).post(f"/projects/{world.project.id}/director/dry-run")
    assert session.scalar(select(func.count(Scene.id))) == before


def test_director_context_current_history_fingerprint_exists(session, world):
    assert StoryGravityContextBuilder().build(session, world.project.id)["current_sequence"] == 0


def test_state_change_is_consequence_signal(session, world):
    event = TimelineEvent(project_id=world.project.id, event_type=TimelineEventType.STATE_CHANGE, source_type="STATE_DELTA_ITEM", source_id="item", source_key="event", sequence=1, ordinal=1, origin=TimelineOrigin.NORMAL_COMMIT, target_type="CHARACTER", target_id=world.lead.id, path="/physical_state/hurt", before_value=False, after_value=True, structured_payload={}, event_fingerprint="event-fp", active=True)
    session.add(event); session.commit()
    assert gravity(session, world).consequence_pressure[0]["target_id"] == world.lead.id


def test_inactive_state_change_is_ignored(session, world):
    event = TimelineEvent(project_id=world.project.id, event_type=TimelineEventType.STATE_CHANGE, source_type="STATE_DELTA_ITEM", source_id="item", source_key="event", sequence=1, ordinal=1, origin=TimelineOrigin.NORMAL_COMMIT, target_type="CHARACTER", target_id=world.lead.id, path="/physical_state/hurt", before_value=False, after_value=True, structured_payload={}, event_fingerprint="event-fp", active=False)
    session.add(event); session.commit()
    assert not gravity(session, world).consequence_pressure


def test_latest_target_path_wins(session, world):
    for seq, after in ((1, True), (3, False)):
        session.add(TimelineEvent(project_id=world.project.id, event_type=TimelineEventType.STATE_CHANGE, source_type="STATE_DELTA_ITEM", source_id=str(seq), source_key=f"event-{seq}", sequence=seq, ordinal=1, origin=TimelineOrigin.NORMAL_COMMIT, target_type="CHARACTER", target_id=world.lead.id, path="/physical_state/hurt", before_value=not after, after_value=after, structured_payload={}, event_fingerprint=f"event-fp-{seq}", active=True))
    session.commit(); signals = gravity(session, world).consequence_pressure
    assert len([row for row in signals if row["path"] == "/physical_state/hurt"]) == 1


def test_candidate_engine_does_not_mutate_thread(session, world):
    before = world.thread.progress; context = StoryGravityContextBuilder().build(session, world.project.id); DirectorCandidateEngine().generate(context, StoryGravityEngine().build(context)); assert world.thread.progress == before


def test_candidate_engine_does_not_create_entities(session, world):
    before = session.scalar(select(func.count(WorldEntity.id))); context = StoryGravityContextBuilder().build(session, world.project.id); DirectorCandidateEngine().generate(context, StoryGravityEngine().build(context)); assert session.scalar(select(func.count(WorldEntity.id))) == before


def test_context_has_no_prose_summary(session, world):
    assert "summary" not in json.dumps(StoryGravityContextBuilder().build(session, world.project.id))


def test_context_has_no_memory_content(session, world):
    assert "content" not in json.dumps(StoryGravityContextBuilder().build(session, world.project.id))


def test_report_current_sequence_uses_scene_sequence(session, world):
    session.add(Scene(project_id=world.project.id, sequence=7, status=SceneStatus.OCCURRED)); session.commit(); assert gravity(session, world).current_sequence == 7


def test_candidate_selection_is_not_random(session, world):
    context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context); engine = DirectorCandidateEngine(); assert engine.select(engine.generate(context, report)).candidate_key == engine.select(engine.generate(context, report)).candidate_key


def test_transition_type_is_available_for_split_locations(session, world):
    world.support.current_state = {"location_id": world.b.id}; session.commit(); context = StoryGravityContextBuilder().build(session, world.project.id); report = StoryGravityEngine().build(context); report.relationship_pressure.append({"path": "/relationships/x"})
    candidates = DirectorCandidateEngine().generate(context, report)
    assert any(item.proposal_type in {ProposalType.RELATIONSHIP.value, ProposalType.TRANSITION.value} for item in candidates)
