"""Phase 9 runtime compatibility and temporal mind integration."""
from sqlalchemy import select

from app.character_mind import ReplayCharacterMindViewBuilder
from app.models import ActionVisibility, CharacterDecision, CharacterKnowledge, CharacterMemory
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
