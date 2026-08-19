"""Phase 9 runtime compatibility and temporal mind integration."""
import json
from types import SimpleNamespace
from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.character_mind import CharacterMemoryRetriever, ReplayCharacterMindViewBuilder, ReplayCognitionUsageProvider, ReplaySceneMetadataProvider, memory_source_bucket
from app.models import ActionVisibility, CausalEdgeKind, CausalLink, CausalRelationType, CausalResourceType, CharacterDecision, CharacterKnowledge, CharacterMemory, RetconReplaySession, Scene
from app.performance import PerformanceActionConstraintChecker, PerformanceActionPayload
from test_character_mind import context, decision, seed, session


def test_normal_performance_checker_uses_recalled_knowledge_id(session):
    project, _location, actor, _other, _client, proposal = seed(session)
    row = CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: opened = true", status="KNOWN", confidence=1)
    session.add(row); session.commit()
    ctx = context(session, project, actor, proposal)
    current = decision(project, actor, proposal, ctx)
    action = PerformanceActionPayload(visibility=ActionVisibility.PUBLIC, observable_action="inspect", spoken_content=None, requires_world_resolution=False, world_resolution_request=None, disclosure_knowledge_ids=[row.id], target_character_id=None)
    assert PerformanceActionConstraintChecker().validate(session, ctx, proposal, current, action).valid


def test_normal_performance_dormant_disclosure_is_blocked(session):
    project, _location, actor, _other, _client, proposal = seed(session)
    for index in range(32):
        session.add(CharacterKnowledge(character_id=actor.id, proposition=f"ENTITY e{index}: open = true", status="KNOWN", confidence=10))
    dormant = CharacterKnowledge(character_id=actor.id, proposition="ENTITY dormant: open = true", status="KNOWN", confidence=0)
    session.add(dormant); session.commit()
    ctx = context(session, project, actor, proposal)
    current = decision(project, actor, proposal, ctx)
    action = PerformanceActionPayload(visibility=ActionVisibility.PUBLIC, observable_action="disclose", spoken_content=None, requires_world_resolution=False, world_resolution_request=None, disclosure_knowledge_ids=[dormant.id], target_character_id=None)
    report = PerformanceActionConstraintChecker().validate(session, ctx, proposal, current, action)
    assert any(issue.code == "KNOWLEDGE_LEAK" for issue in report.issues)


def test_replay_mind_is_temporal_bounded_and_excludes_future(session, monkeypatch):
    from app.models import RetconReplaySession, Scene
    from test_retcon_replay import historical_replay_world
    project, scene, actor, proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    replay_session = session.get(RetconReplaySession, created.json()["id"])
    view = ReplayCharacterMindViewBuilder().build(session, replay_session, scene, proposal, actor.id)
    assert len(view["knowledge"]) <= 32 and len(view["memories"]) <= 12
    assert all(item["proposition"] != "future secret" for item in view["knowledge"])
    assert all(item["content"] != "future memory" for item in view["memories"])
    assert all("knowledge_id" in item and "fact_identity" in item for item in view["knowledge"])
    assert all({"memory_id", "content", "source_scene_id", "happened_at"}.issubset(item) for item in view["memories"])


def test_replay_mind_build_is_read_only(session, monkeypatch):
    from app.models import CausalLink, RetconReplaySession, TimelineEvent
    from app.versioning import WorldSnapshotBuilder
    from test_retcon_replay import historical_replay_world
    project, scene, actor, proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    replay_session = session.get(RetconReplaySession, created.json()["id"])
    before = (session.query(CharacterKnowledge).count(), session.query(CharacterMemory).count(), session.query(CausalLink).count(), session.query(TimelineEvent).count(), WorldSnapshotBuilder().build(session, project.id)[1])
    ReplayCharacterMindViewBuilder().build(session, replay_session, scene, proposal, actor.id)
    after = (session.query(CharacterKnowledge).count(), session.query(CharacterMemory).count(), session.query(CausalLink).count(), session.query(TimelineEvent).count(), WorldSnapshotBuilder().build(session, project.id)[1])
    assert before == after


def test_replay_usage_requires_prior_typed_sequence(session):
    project, _location, actor, _other, _client, _proposal = seed(session)
    knowledge_row = CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: opened = true", status="KNOWN")
    memory_row = CharacterMemory(character_id=actor.id, content="door opened")
    session.add_all([knowledge_row, memory_row]); session.flush()
    session.add_all([
        CausalLink(project_id=project.id, cause_type=CausalResourceType.CHARACTER_KNOWLEDGE, cause_id=knowledge_row.id, effect_type=CausalResourceType.CHARACTER_DECISION, effect_id="future-decision", edge_kind=CausalEdgeKind.CAUSAL, relation_type=CausalRelationType.KNOWLEDGE_INFORMED_DECISION, sequence=8, active=True, source_key="future-k", link_fingerprint="future-k"),
        CausalLink(project_id=project.id, cause_type=CausalResourceType.CHARACTER_MEMORY, cause_id=memory_row.id, effect_type=CausalResourceType.CHARACTER_DECISION, effect_id="prior-decision", edge_kind=CausalEdgeKind.CAUSAL, relation_type=CausalRelationType.MEMORY_INFORMED_DECISION, sequence=2, active=True, source_key="prior-m", link_fingerprint="prior-m"),
        CausalLink(project_id=project.id, cause_type=CausalResourceType.CHARACTER_MEMORY, cause_id=memory_row.id, effect_type=CausalResourceType.CHARACTER_DECISION, effect_id="unknown-decision", edge_kind=CausalEdgeKind.CAUSAL, relation_type=CausalRelationType.MEMORY_INFORMED_DECISION, sequence=None, active=True, source_key="unknown-m", link_fingerprint="unknown-m"),
    ]); session.commit()
    replay = RetconReplaySession(project_id=project.id, queue=[], staged_world_state={})
    usage = ReplayCognitionUsageProvider(session, replay, 3)
    assert usage.get(CausalResourceType.CHARACTER_KNOWLEDGE, knowledge_row.id) == (0, -1)
    assert usage.get(CausalResourceType.CHARACTER_MEMORY, memory_row.id) == (1, 2)
    assert usage.get(CausalResourceType.CHARACTER_KNOWLEDGE, memory_row.id) == (0, -1)


def test_replay_staged_memory_uses_prior_source_sequence_metadata(session):
    project, _location, actor, other, _client, _proposal = seed(session)
    replay = RetconReplaySession(project_id=project.id, queue=[], staged_world_state={"current_world": {"scenes": [{"id": "scene-2", "sequence": 2, "location": "tomb", "participants": [other.id], "story_threads": ["thread-A"]}]}, "scene_results": {"scene-2": {"sequence": 2, "situation": {"location": "tomb", "participants": [other.id]}}}})
    metadata = ReplaySceneMetadataProvider(replay, 3)
    staged = SimpleNamespace(id="replay-memory:2", character_id=actor.id, content="tomb memory", importance=0, emotional_weight=0, confidence=0, distortion={}, happened_at=None, source_scene=None, source_sequence=2)
    selected = CharacterMemoryRetriever().retrieve(session, project.id, [staged], {"entity_ids": (), "character_ids": (other.id,), "participant_ids": (other.id,), "thread_ids": ("thread-A",), "item_ids": (), "location_ids": ("tomb",)}, usage_provider=ReplayCognitionUsageProvider(session, replay, 3), current_sequence=3, scene_provider=lambda key: metadata.by_scene(key) or metadata.by_sequence_value(key if isinstance(key, int) else None))
    assert selected and selected[0]["memory_id"] == staged.id and selected[0]["source_scene_sequence"] == 2
    assert metadata.by_scene("scene-2")["story_threads"] == ["thread-A"]
    assert metadata.by_sequence_value(4) is None


def test_replay_staged_same_source_sequence_obeys_diversity_cap(session):
    project, _location, actor, _other, _client, _proposal = seed(session)
    replay = SimpleNamespace(project_id=project.id, queue=[], staged_world_state={})
    memories = [SimpleNamespace(id=f"replay-memory:{index}", character_id=actor.id, content=f"memory {index}", importance=0, emotional_weight=0, confidence=0, distortion={}, happened_at=None, source_scene=None, source_sequence=2) for index in range(8)]
    selected = CharacterMemoryRetriever().retrieve(session, project.id, memories, {"entity_ids": (), "character_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()}, usage_provider=ReplayCognitionUsageProvider(session, replay, 3), current_sequence=3)
    assert sum(item.get("source_scene_sequence") == 2 for item in selected) == 3
    assert all(memory_source_bucket(item) == "sequence:2" for item in selected)


def test_replay_staged_strong_same_source_can_escape_diversity_cap(session):
    project, _location, actor, other, _client, _proposal = seed(session)
    replay = SimpleNamespace(project_id=project.id, queue=[], staged_world_state={})
    memories = [SimpleNamespace(id=f"replay-memory:strong:{index}", character_id=actor.id, content=f"memory {index}", importance=0, emotional_weight=0, confidence=0, distortion={"location_id": "tomb", "participant_ids": [other.id]}, happened_at=None, source_scene=None, source_sequence=2) for index in range(4)]
    cues = {"entity_ids": (), "character_ids": (), "participant_ids": (other.id,), "thread_ids": (), "item_ids": (), "location_ids": ("tomb",)}
    selected = CharacterMemoryRetriever().retrieve(session, project.id, memories, cues, usage_provider=ReplayCognitionUsageProvider(session, replay, 3), current_sequence=3, scene_provider=lambda _source: {"sequence": 2, "location": "tomb", "participants": [other.id], "story_threads": []})
    assert len(selected) == 4


def test_replay_scene_metadata_inherits_missing_story_threads(session):
    project, _location, actor, _other, _client, _proposal = seed(session)
    replay = RetconReplaySession(project_id=project.id, queue=[], staged_world_state={"current_world": {"scenes": [{"id": "scene-2", "sequence": 2, "location": "tomb", "participants": [], "story_threads": ["thread-A"]}]}, "scene_results": {"scene-2": {"sequence": 2, "situation": {"location": "tomb", "participants": []}}}})
    metadata = ReplaySceneMetadataProvider(replay, 3)
    assert metadata.by_scene("scene-2")["story_threads"] == ["thread-A"]


def test_replay_scene_metadata_explicit_empty_threads_and_future_isolation(session):
    project, _location, actor, _other, _client, _proposal = seed(session)
    replay = RetconReplaySession(project_id=project.id, queue=[], staged_world_state={"current_world": {"scenes": [{"id": "scene-2", "sequence": 2, "location": "tomb", "participants": [], "story_threads": ["thread-A"]}, {"id": "scene-4", "sequence": 4, "location": "future", "participants": [], "story_threads": ["future-thread"]}]}, "scene_results": {"scene-2": {"sequence": 2, "situation": {"story_threads": []}}}})
    metadata = ReplaySceneMetadataProvider(replay, 3)
    assert metadata.by_scene("scene-2")["story_threads"] == []
    assert metadata.by_scene("scene-4") is None and metadata.by_sequence_value(4) is None


def test_replay_scene_metadata_merge_is_order_independent(session):
    project, _location, actor, _other, _client, _proposal = seed(session)
    def build(scenes, results):
        replay = RetconReplaySession(project_id=project.id, queue=[], staged_world_state={"current_world": {"scenes": scenes}, "scene_results": results})
        return ReplaySceneMetadataProvider(replay, 3).by_scene("scene-2")
    base = {"id": "scene-2", "sequence": 2, "location": "tomb", "participants": [actor.id], "story_threads": ["thread-A"]}
    first = build([base], {"scene-2": {"sequence": 2, "situation": {"location": "tomb", "participants": [actor.id]}}})
    second = build([{**base}], {"scene-2": {"situation": {"participants": [actor.id], "location": "tomb"}, "sequence": 2}})
    assert first == second


def test_replay_usage_replaces_old_replayed_link_without_double_count(session):
    project, _location, actor, _other, _client, _proposal = seed(session)
    memory_row = CharacterMemory(character_id=actor.id, content="prior memory")
    old_scene = Scene(project_id=project.id, sequence=2, status="OCCURRED", history_status="ACTIVE")
    session.add_all([memory_row, old_scene]); session.flush()
    session.add(CausalLink(project_id=project.id, cause_type=CausalResourceType.CHARACTER_MEMORY, cause_id=memory_row.id, effect_type=CausalResourceType.CHARACTER_DECISION, effect_id="old-decision", edge_kind=CausalEdgeKind.CAUSAL, relation_type=CausalRelationType.MEMORY_INFORMED_DECISION, scene_id=old_scene.id, sequence=2, active=True, source_key="old-replayed-m", link_fingerprint="old-replayed-m")); session.commit()
    replay = RetconReplaySession(project_id=project.id, queue=[{"scene_id": old_scene.id, "sequence": 2, "mode": "REPLAY"}], staged_world_state={"scene_results": {old_scene.id: {"sequence": 2, "decisions": [{"decision": {"knowledge_used": [], "memory_refs": [memory_row.id]}}]}}})
    usage = ReplayCognitionUsageProvider(session, replay, 3)
    assert usage.get(CausalResourceType.CHARACTER_MEMORY, memory_row.id) == (1, 2)
    replay.queue = [{"scene_id": old_scene.id, "sequence": 2, "mode": "VALIDATE_PRESERVED"}]
    replay.staged_world_state = {}
    preserved = ReplayCognitionUsageProvider(session, replay, 3)
    assert preserved.get(CausalResourceType.CHARACTER_MEMORY, memory_row.id) == (1, 2)


def test_multiscene_replay_formalizes_staged_cognition_and_phase8_edges(session, monkeypatch):
    from app.models import ReplaySceneRun, RetconReplaySession, SceneProposal
    from test_scene_checkpoint_unification import historical_multiscene_replay
    fixture = historical_multiscene_replay(session, monkeypatch)
    scene2, scene3, _scene4 = fixture.scenes
    old_decision3 = session.get(CharacterDecision, fixture.preserved_ids[0])
    proposal3 = session.get(SceneProposal, old_decision3.scene_proposal_id)
    location_id = proposal3.location_id
    proposal3.entry_state = {"replay_prerequisites": {"required_entity_facts": [{"entity_id": location_id, "predicate": "opened", "expected": False}]}}
    session.add(proposal3); session.commit()
    created = fixture.client.post(f"/projects/{fixture.project.id}/retcon/applications/{fixture.application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    state = created.json()
    while state["cursor"] < len(state["queue"]):
        stepped = fixture.client.post(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}/step")
        assert stepped.status_code == 200, stepped.text
        state = fixture.client.get(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}").json()
    replay_session = session.get(RetconReplaySession, state["id"])
    staged2 = replay_session.staged_world_state["scene_results"][scene2.id]
    staged3 = replay_session.staged_world_state["scene_results"][scene3.id]
    staged_decision3 = staged3["decisions"][0]["decision"]
    temp_knowledge_id = staged_decision3["knowledge_used"][0]["knowledge_id"]
    temp_memory_id = staged_decision3["memory_refs"][0]
    assert temp_knowledge_id.startswith("replay-knowledge:")
    assert temp_memory_id.startswith("replay-memory:")
    committed = fixture.client.post(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}/commit", json={"explicit_confirmation": True})
    assert committed.status_code == 200, committed.text
    session.expire_all()
    runs = {row.original_scene_id: row for row in session.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == state["id"])).all()}
    run2, run3 = runs[scene2.id], runs[scene3.id]
    knowledge_index = next(index for index, item in enumerate(staged2["knowledge"]) if item["temp_id"] == temp_knowledge_id)
    memory_index = next(index for index, item in enumerate(staged2["memories"]) if item["temp_id"] == temp_memory_id)
    formal_knowledge = session.get(CharacterKnowledge, run2.new_knowledge_ids[knowledge_index])
    formal_memory = session.get(CharacterMemory, run2.new_memory_ids[memory_index])
    formal_decision3 = session.get(CharacterDecision, run3.new_decision_ids[0])
    assert formal_decision3.knowledge_used[0] == {"knowledge_id": formal_knowledge.id, "proposition": formal_knowledge.proposition, "accepted_statuses": [formal_knowledge.status.value]}
    assert formal_decision3.memory_refs[0] == formal_memory.id
    assert formal_knowledge.character_id == formal_decision3.character_id == formal_memory.character_id
    assert "replay-knowledge:" not in json.dumps(formal_decision3.knowledge_used)
    assert "replay-memory:" not in json.dumps(formal_decision3.memory_refs)
    assert session.scalar(select(CausalLink).where(CausalLink.cause_id == formal_knowledge.id, CausalLink.effect_id == formal_decision3.id, CausalLink.relation_type == CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalLink.active.is_(True)))
    assert session.scalar(select(CausalLink).where(CausalLink.cause_id == formal_memory.id, CausalLink.effect_id == formal_decision3.id, CausalLink.relation_type == CausalRelationType.MEMORY_INFORMED_DECISION, CausalLink.active.is_(True)))


def test_replay_cognition_mapping_failure_is_atomic(session, monkeypatch):
    from app.models import RetconApplication, SceneProposal, SceneStateCheckpoint, TimelineEvent
    from test_scene_checkpoint_unification import historical_multiscene_replay
    fixture = historical_multiscene_replay(session, monkeypatch)
    scene2, scene3, _scene4 = fixture.scenes
    old_decision3 = session.get(CharacterDecision, fixture.preserved_ids[0])
    proposal3 = session.get(SceneProposal, old_decision3.scene_proposal_id)
    proposal3.entry_state = {"replay_prerequisites": {"required_entity_facts": [{"entity_id": proposal3.location_id, "predicate": "opened", "expected": False}]}}
    session.add(proposal3); session.commit()
    state = fixture.client.post(f"/projects/{fixture.project.id}/retcon/applications/{fixture.application_id}/replay-sessions").json()
    while state["cursor"] < len(state["queue"]):
        stepped = fixture.client.post(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}/step")
        assert stepped.status_code == 200, stepped.text
        state = fixture.client.get(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}").json()
    replay_session = session.get(RetconReplaySession, state["id"])
    staged = deepcopy(replay_session.staged_world_state)
    other_id = next(value for value in scene2.participants if value != fixture.actor.id)
    staged["scene_results"][scene2.id]["knowledge"][0]["character_id"] = other_id
    replay_session.staged_world_state = staged; session.add(replay_session); session.commit()
    models = (Scene, CharacterDecision, CharacterKnowledge, CharacterMemory, SceneStateCheckpoint, TimelineEvent, CausalLink)
    before = {model: session.scalar(select(func.count(model.id))) for model in models}
    response = fixture.client.post(f"/projects/{fixture.project.id}/retcon/replay-sessions/{state['id']}/commit", json={"explicit_confirmation": True})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "REPLAY_COGNITION_REFERENCE_INVALID"
    with sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)() as fresh:
        assert {model: fresh.scalar(select(func.count(model.id))) for model in models} == before
        assert fresh.get(RetconReplaySession, state["id"]).status != "COMPLETED"
        assert fresh.get(RetconApplication, fixture.application_id).status == "APPLIED_PENDING_REPLAY"
        assert all(fresh.get(Scene, row.id).history_status == "ACTIVE" for row in fixture.scenes)
