from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api
from app.db import Base
from app.main import app
from app.models import (
    CausalLink, Chapter, ChapterSceneBinding, NarrativeArc,
    NarrativeArcChapterBinding, NarrativeStructureRevision, NarrativeVolume,
    NarrativeVolumeArcBinding, Project, RetconApplication,
    RetconApplicationStatus, RetconImpactPlan, RetconRequest, Scene,
    SceneStateCheckpoint, StoryArc, TimelineEvent, WorldRevision,
)
from app.narrative_structure import (
    ChapterBoundaryScorer, ChapterFormationEngine, NarrativeArcFormationEngine,
    NarrativeSceneFeatureBuilder, NarrativeStructureAudit,
    NarrativeStructureConfig, NarrativeStructureService,
    NarrativeStructureSourceFingerprintBuilder, NarrativeVolumeFormationEngine,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


@pytest.fixture()
def project(session):
    value = Project(name="Structure")
    session.add(value); session.commit()
    return value


def scene(session, project, sequence, *, thread="thread-a", location="room-a", participants=None, summary=None, intent=None, history="ACTIVE"):
    value = Scene(project_id=project.id, sequence=sequence, world_time=datetime(2026, 1, 1) + timedelta(hours=sequence), location=location, participants=participants or ["character-a"], intent=intent, facts=[], result={}, summary=summary, story_threads=[thread] if thread else [], status="OCCURRED", history_status=history)
    session.add(value); session.flush()
    return value


def config(**changes):
    value = {"chapter_min_scenes": 2, "chapter_target_scenes": 3, "chapter_max_scenes": 4, "chapter_boundary_threshold": 2.0, "arc_min_chapters": 2, "arc_max_chapters": 3, "arc_boundary_threshold": 1.0, "volume_min_arcs": 2, "volume_max_arcs": 2, "volume_boundary_threshold": 1.0}
    value.update(changes)
    return value


def test_zero_scene_preview_is_deterministic(session, project):
    first = NarrativeStructureService().preview(session, project.id)
    second = NarrativeStructureService().preview(session, project.id)
    assert first == second and first["chapters"] == first["narrative_arcs"] == first["volumes"] == []


def test_zero_scene_sync_creates_empty_revision(session, project):
    revision, existing = NarrativeStructureService().sync(session, project.id)
    assert not existing and revision.source_max_sequence == 0
    NarrativeStructureAudit().audit(session, project.id)


def test_one_scene_forms_provisional_open_hierarchy(session, project):
    item = scene(session, project, 1)
    result = NarrativeStructureService().preview(session, project.id)
    assert result["chapters"][0]["status"] == "PROVISIONAL" and result["chapters"][0]["scene_ids"] == [item.id]
    assert result["narrative_arcs"][0]["status"] == "OPEN" and result["volumes"][0]["status"] == "OPEN"


def test_structured_transition_forms_chapter_boundary(session, project):
    scene(session, project, 1); scene(session, project, 2)
    scene(session, project, 3, thread="thread-b", location="room-b", participants=["character-b"])
    scene(session, project, 4, thread="thread-b", location="room-b", participants=["character-b"])
    result = NarrativeStructureService().preview(session, project.id, config())
    assert [(item["start_sequence"], item["end_sequence"]) for item in result["chapters"]] == [(1, 2), (3, 4)]


def test_min_scenes_blocks_early_soft_cut(session, project):
    scene(session, project, 1, thread="a", location="a", participants=["a"])
    scene(session, project, 2, thread="b", location="b", participants=["b"])
    result = NarrativeStructureService().preview(session, project.id, config(chapter_min_scenes=2, chapter_target_scenes=2))
    assert result["chapters"][0]["start_sequence"] == 1 and result["chapters"][0]["end_sequence"] >= 2


def test_hard_max_cuts_similar_scenes(session, project):
    for sequence in range(1, 6): scene(session, project, sequence)
    result = NarrativeStructureService().preview(session, project.id, config(chapter_min_scenes=2, chapter_target_scenes=2, chapter_max_scenes=3, chapter_boundary_threshold=99))
    assert [(item["start_sequence"], item["end_sequence"]) for item in result["chapters"]] == [(1, 3), (4, 5)]


def test_summary_and_intent_do_not_affect_source(session, project):
    item = scene(session, project, 1, summary="one", intent="first prose")
    first = NarrativeStructureService().preview(session, project.id)
    item.summary = "entirely different"; item.intent = "different intent text"; session.flush()
    second = NarrativeStructureService().preview(session, project.id)
    assert first["source_fingerprint"] == second["source_fingerprint"] and first["structure_fingerprint"] == second["structure_fingerprint"]


def test_structured_scene_change_updates_source(session, project):
    item = scene(session, project, 1)
    first = NarrativeStructureService().preview(session, project.id)
    item.location = "room-b"; session.flush()
    assert NarrativeStructureService().preview(session, project.id)["source_fingerprint"] != first["source_fingerprint"]


def test_superseded_scenes_are_excluded(session, project):
    current = scene(session, project, 1)
    scene(session, project, 2, history="SUPERSEDED")
    result = NarrativeStructureService().preview(session, project.id)
    assert result["chapters"][0]["scene_ids"] == [current.id]


def test_feature_builder_is_insertion_order_independent():
    base = {"scene_id": "scene", "sequence": 1, "world_time": None, "location_id": "room", "participant_ids": ["b", "a"], "thread_ids": ["z", "a"], "checkpoint_fingerprint": "checkpoint", "execution": {"primary_thread_id": "a", "proposal_type": "CONTINUE_THREAD"}, "timeline_events": []}
    left = NarrativeSceneFeatureBuilder().one(base | {"participant_ids": sorted(base["participant_ids"]), "thread_ids": sorted(base["thread_ids"])})
    right = NarrativeSceneFeatureBuilder().one(base | {"participant_ids": sorted(base["participant_ids"], reverse=True), "thread_ids": sorted(base["thread_ids"], reverse=True)})
    assert left["feature_fingerprint"] == right["feature_fingerprint"]


def test_config_validation_rejects_invalid_ranges(session, project):
    with pytest.raises(ValueError, match="INVALID_NARRATIVE_STRUCTURE_CONFIG"):
        NarrativeStructureService().preview(session, project.id, {"chapter_min_scenes": 5, "chapter_target_scenes": 2, "chapter_max_scenes": 3})


def test_project_config_is_used(session, project):
    project.autonomy_settings = {"narrative_structure": config(chapter_max_scenes=2, chapter_target_scenes=2)}
    for sequence in range(1, 4): scene(session, project, sequence)
    assert len(NarrativeStructureService().preview(session, project.id)["chapters"]) == 2


def test_preview_is_read_only(session, project):
    scene(session, project, 1)
    before = {model: session.scalar(select(func.count(model.id))) for model in (Chapter, NarrativeStructureRevision, NarrativeArc, NarrativeVolume, TimelineEvent, CausalLink)}
    NarrativeStructureService().preview(session, project.id)
    after = {model: session.scalar(select(func.count(model.id))) for model in before}
    assert before == after


def test_sync_is_idempotent(session, project):
    scene(session, project, 1)
    first, first_existing = NarrativeStructureService().sync(session, project.id)
    counts = tuple(session.scalar(select(func.count(model.id))) for model in (NarrativeStructureRevision, Chapter, NarrativeArc, NarrativeVolume))
    second, second_existing = NarrativeStructureService().sync(session, project.id)
    assert not first_existing and second_existing and first.id == second.id
    assert counts == tuple(session.scalar(select(func.count(model.id))) for model in (NarrativeStructureRevision, Chapter, NarrativeArc, NarrativeVolume))


def test_sync_materializes_binding_parity(session, project):
    scenes = [scene(session, project, sequence) for sequence in range(1, 4)]
    NarrativeStructureService().sync(session, project.id, config(chapter_boundary_threshold=99))
    chapter = session.scalar(select(Chapter).where(Chapter.active.is_(True)))
    bindings = session.scalars(select(ChapterSceneBinding).where(ChapterSceneBinding.chapter_id == chapter.id).order_by(ChapterSceneBinding.ordinal)).all()
    assert chapter.source_scene_ids == [item.id for item in scenes] == [item.scene_id for item in bindings]


def test_new_chapter_has_no_prose(session, project):
    scene(session, project, 1); NarrativeStructureService().sync(session, project.id)
    chapter = session.scalar(select(Chapter).where(Chapter.active.is_(True)))
    assert chapter.content is None and chapter.word_count == 0 and chapter.quality_report == {} and chapter.status == "DRAFT"


def test_hierarchy_has_exact_coverage(session, project):
    for sequence in range(1, 9): scene(session, project, sequence, thread="a" if sequence < 5 else "b", location="a" if sequence < 5 else "b")
    NarrativeStructureService().sync(session, project.id, config(chapter_max_scenes=2, chapter_target_scenes=2))
    NarrativeStructureAudit().audit(session, project.id)
    assert session.scalar(select(func.count(ChapterSceneBinding.id))) == 8


def test_arc_formation_uses_thread_ids_not_titles():
    chapters = []
    for number, thread in enumerate(["a", "a", "b", "b"], 1):
        chapters.append({"number": number, "start_sequence": number, "end_sequence": number, "features": [{"thread_ids": [thread]}]})
    arcs = NarrativeArcFormationEngine().form(chapters, NarrativeStructureConfig(arc_min_chapters=2, arc_max_chapters=4))
    assert [item["chapter_numbers"] for item in arcs] == [[1, 2], [3, 4]]


def test_volume_hard_max_forms_boundary():
    arcs = [{"number": value, "start_sequence": value, "end_sequence": value, "dominant_thread_ids": [str(value)]} for value in range(1, 4)]
    volumes = NarrativeVolumeFormationEngine().form(arcs, NarrativeStructureConfig(volume_min_arcs=2, volume_max_arcs=2))
    assert [item["arc_numbers"] for item in volumes] == [[1, 2], [3]] and volumes[-1]["status"] == "OPEN"


def test_append_preserves_sealed_chapter_id(session, project):
    for sequence in range(1, 5): scene(session, project, sequence, thread="a" if sequence <= 2 else "b", location="a" if sequence <= 2 else "b")
    service = NarrativeStructureService(); service.sync(session, project.id, config())
    sealed = session.scalar(select(Chapter).where(Chapter.active.is_(True), Chapter.structure_status == "SEALED"))
    scene(session, project, 5, thread="b", location="b")
    service.sync(session, project.id, config())
    assert session.get(Chapter, sealed.id).active is True and session.get(Chapter, sealed.id).structure_fingerprint == sealed.structure_fingerprint


def test_changed_tail_is_superseded_not_deleted(session, project):
    for sequence in range(1, 4): scene(session, project, sequence)
    service = NarrativeStructureService(); service.sync(session, project.id, config(chapter_boundary_threshold=99))
    old = session.scalar(select(Chapter).where(Chapter.active.is_(True)))
    scene(session, project, 4, thread="b", location="b", participants=["b"])
    service.sync(session, project.id, config(chapter_boundary_threshold=1))
    assert session.get(Chapter, old.id) is not None and session.get(Chapter, old.id).active is False and session.get(Chapter, old.id).structure_status == "SUPERSEDED"


def test_config_change_creates_new_revision(session, project):
    scene(session, project, 1); service = NarrativeStructureService()
    first, _ = service.sync(session, project.id, config())
    second, existing = service.sync(session, project.id, config(chapter_boundary_threshold=8))
    assert not existing and first.id != second.id and not first.active and second.active


def test_expected_source_fingerprint_blocks_stale_sync(session, project):
    scene(session, project, 1)
    with pytest.raises(ValueError, match="NARRATIVE_STRUCTURE_SOURCE_CHANGED"):
        NarrativeStructureService().sync(session, project.id, expected_source_fingerprint="stale")


def test_pending_replay_blocks_sync(session, project):
    revision = WorldRevision(project_id=project.id, title="Retcon source", status="DRAFT", change_set=[], normalized_changes=[], impact_report={})
    session.add(revision); session.flush()
    request = RetconRequest(project_id=project.id, source_revision_id=revision.id, reason="x", status="DRAFT")
    session.add(request); session.flush()
    plan = RetconImpactPlan(project_id=project.id, retcon_request_id=request.id, version=1, basis_fingerprint="x", status="READY", impact_summary={}, validation_report={})
    session.add(plan); session.flush()
    application = RetconApplication(project_id=project.id, retcon_request_id=request.id, retcon_plan_id=plan.id, source_revision_id=revision.id, status=RetconApplicationStatus.APPLIED_PENDING_REPLAY, plan_basis_fingerprint="x", pre_apply_world_fingerprint="x", cognition_summary={}, replay_summary={})
    session.add(application); session.flush()
    with pytest.raises(ValueError, match="RETCON_REPLAY_REQUIRED"):
        NarrativeStructureService().sync(session, project.id)


def test_story_arc_is_not_modified_by_sync(session, project):
    arc = StoryArc(project_id=project.id, title="Intent arc", status="ACTIVE", progress=0.25, source_scene_ids=["legacy"])
    session.add(arc); scene(session, project, 1); session.flush()
    before = (arc.status, arc.progress, list(arc.source_scene_ids))
    NarrativeStructureService().sync(session, project.id)
    assert (arc.status, arc.progress, arc.source_scene_ids) == before


def test_active_revision_partial_unique(session, project):
    kwargs = dict(project_id=project.id, active=True, protocol_version=1, source_history_fingerprint="x", source_max_sequence=0, config={}, config_fingerprint="x", rebuild_from_sequence=1, structure_fingerprint="x")
    session.add(NarrativeStructureRevision(**kwargs)); session.commit()
    session.add(NarrativeStructureRevision(**kwargs))
    with pytest.raises(IntegrityError): session.commit()


def test_api_preview_sync_current_and_revisions_are_metadata_only(session, project, monkeypatch):
    scene(session, project, 1); session.commit()
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    client = TestClient(app)
    assert client.post(f"/projects/{project.id}/narrative-structure/preview", json={}).status_code == 200
    sync = client.post(f"/projects/{project.id}/narrative-structure/sync", json={})
    assert sync.status_code == 200 and sync.json()["chapters"][0]["structure_status"] == "PROVISIONAL"
    current = client.get(f"/projects/{project.id}/narrative-structure")
    revisions = client.get(f"/projects/{project.id}/narrative-structure/revisions")
    rendered = current.text + revisions.text
    assert current.status_code == revisions.status_code == 200 and "content" not in rendered and "payload" not in rendered


def test_api_invalid_config_is_422(session, project, monkeypatch):
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    response = TestClient(app).post(f"/projects/{project.id}/narrative-structure/preview", json={"config": {"chapter_min_scenes": 4, "chapter_target_scenes": 2}})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "INVALID_NARRATIVE_STRUCTURE_CONFIG"


def test_current_reports_stale_after_structured_change(session, project):
    item = scene(session, project, 1); service = NarrativeStructureService(); service.sync(session, project.id)
    item.location = "changed"; session.flush()
    assert service.current(session, project.id)["stale"] is True


def test_current_history_sequence_gap_fails_closed(session, project):
    scene(session, project, 1); scene(session, project, 3)
    with pytest.raises(ValueError, match="NARRATIVE_STRUCTURE_HISTORY_INVALID"):
        NarrativeStructureService().preview(session, project.id)


def test_current_history_must_start_at_sequence_one(session, project):
    scene(session, project, 2)
    with pytest.raises(ValueError, match="NARRATIVE_STRUCTURE_HISTORY_INVALID"):
        NarrativeStructureService().preview(session, project.id)


def test_only_protocol_v3_checkpoint_is_source_authority(session, project):
    item = scene(session, project, 1)
    legacy = SceneStateCheckpoint(project_id=project.id, scene_id=item.id, sequence=1, pre_snapshot_id="pre-v2", post_snapshot_id="post-v2", current_scene_id=item.id, capture_protocol_version=2, version=1, active=True, checkpoint_fingerprint="v2")
    session.add(legacy); session.flush()
    first = NarrativeStructureSourceFingerprintBuilder().build(session, project.id)[0][0]
    legacy.active = False
    current = SceneStateCheckpoint(project_id=project.id, scene_id=item.id, sequence=1, pre_snapshot_id="pre-v3", post_snapshot_id="post-v3", current_scene_id=item.id, capture_protocol_version=3, version=2, active=True, checkpoint_fingerprint="v3")
    session.add(current); session.flush()
    second = NarrativeStructureSourceFingerprintBuilder().build(session, project.id)[0][0]
    assert first["checkpoint_id"] is None and second["checkpoint_id"] == current.id and second["checkpoint_fingerprint"] == "v3"


def test_active_timeline_event_changes_source_fingerprint(session, project):
    item = scene(session, project, 1)
    before = NarrativeStructureService().preview(session, project.id)["source_fingerprint"]
    event = TimelineEvent(project_id=project.id, event_type="STATE_CHANGE", source_type="STATE_DELTA_ITEM", source_id="delta", source_key="phase12:active", scene_id=item.id, sequence=1, ordinal=1, origin="NORMAL_COMMIT", active=True, target_type="WORLD_ENTITY", target_id="door", path="/profile/opened", before_value=False, after_value=True, structured_payload={}, event_fingerprint="event-active")
    session.add(event); session.flush()
    assert NarrativeStructureService().preview(session, project.id)["source_fingerprint"] != before


def test_inactive_timeline_event_is_excluded_from_source(session, project):
    item = scene(session, project, 1)
    before = NarrativeStructureService().preview(session, project.id)["source_fingerprint"]
    event = TimelineEvent(project_id=project.id, event_type="STATE_CHANGE", source_type="STATE_DELTA_ITEM", source_id="delta", source_key="phase12:inactive", scene_id=item.id, sequence=1, ordinal=1, origin="NORMAL_COMMIT", active=False, target_type="WORLD_ENTITY", target_id="door", path="/profile/opened", before_value=False, after_value=True, structured_payload={}, event_fingerprint="event-inactive")
    session.add(event); session.flush()
    assert NarrativeStructureService().preview(session, project.id)["source_fingerprint"] == before


def test_scene_feature_aggregates_only_structured_state_changes():
    row = {"scene_id": "scene", "sequence": 1, "world_time": None, "location_id": "room", "participant_ids": ["b", "a"], "thread_ids": ["thread"], "checkpoint_fingerprint": "checkpoint", "execution": {"primary_thread_id": "thread", "proposal_type": "CONTINUE_THREAD"}, "timeline_events": [
        {"id": "e2", "event_type": "STATE_CHANGE", "target_type": "STORY_THREAD", "target_id": "thread", "path": "/status"},
        {"id": "e1", "event_type": "STATE_CHANGE", "target_type": "WORLD_ENTITY", "target_id": "door", "path": "/profile/opened"},
        {"id": "scene-event", "event_type": "SCENE_OCCURRED", "target_type": "SCENE", "target_id": "scene", "path": None},
    ]}
    feature = NarrativeSceneFeatureBuilder().one(row)
    assert feature["state_change_count"] == 2 and feature["thread_state_event_ids"] == ["e2"]
    assert feature["state_change_targets"] == ["STORY_THREAD:thread", "WORLD_ENTITY:door"]


def test_location_transition_is_pressure_not_absolute_boundary():
    left = {"primary_thread_id": "t", "thread_ids": ["t"], "location_id": "a", "participant_ids": ["c"], "proposal_type": "CONTINUE_THREAD", "state_change_count": 0, "thread_state_event_ids": [], "world_time": None}
    right = left | {"location_id": "b"}
    score = ChapterBoundaryScorer().score(left, right, 2, NarrativeStructureConfig(chapter_boundary_threshold=99))
    assert 0 < score["score"] < 99 and score["reason_codes"] == ["LOCATION_TRANSITION"]


def test_world_time_gap_uses_world_time_not_created_at():
    left = {"primary_thread_id": None, "thread_ids": [], "location_id": None, "participant_ids": [], "proposal_type": None, "state_change_count": 0, "thread_state_event_ids": [], "world_time": "2026-01-01T00:00:00"}
    right = left | {"world_time": "2026-01-03T00:00:00"}
    score = ChapterBoundaryScorer().score(left, right, 1, NarrativeStructureConfig())
    assert "WORLD_TIME_GAP" in score["reason_codes"]


def test_arc_boundary_threshold_controls_soft_cut():
    chapters = [{"number": number, "start_sequence": number, "end_sequence": number, "features": [{"thread_ids": [thread], "location_id": "room", "thread_state_event_ids": []}]} for number, thread in enumerate(["a", "a", "b", "b"], 1)]
    low = NarrativeArcFormationEngine().form(chapters, NarrativeStructureConfig(arc_min_chapters=2, arc_max_chapters=4, arc_boundary_threshold=1))
    high = NarrativeArcFormationEngine().form(chapters, NarrativeStructureConfig(arc_min_chapters=2, arc_max_chapters=4, arc_boundary_threshold=99))
    assert [row["chapter_numbers"] for row in low] == [[1, 2], [3, 4]] and len(high) == 1


def test_volume_boundary_threshold_controls_soft_cut():
    arcs = [{"number": number, "status": "SEALED" if number < 4 else "OPEN", "start_sequence": number, "end_sequence": number, "dominant_thread_ids": [thread]} for number, thread in enumerate(["a", "a", "b", "b"], 1)]
    low = NarrativeVolumeFormationEngine().form(arcs, NarrativeStructureConfig(volume_min_arcs=2, volume_max_arcs=4, volume_boundary_threshold=1))
    high = NarrativeVolumeFormationEngine().form(arcs, NarrativeStructureConfig(volume_min_arcs=2, volume_max_arcs=4, volume_boundary_threshold=99))
    assert [row["arc_numbers"] for row in low] == [[1, 2], [3, 4]] and len(high) == 1


def test_chapter_content_is_not_source_authority(session, project):
    scene(session, project, 1)
    legacy = Chapter(project_id=project.id, number=99, content="first prose", word_count=2, status="DRAFT", active=False, structure_status="LEGACY")
    session.add(legacy); session.flush()
    before = NarrativeStructureService().preview(session, project.id)
    legacy.content = "rewritten prose"; legacy.word_count = 99; session.flush()
    assert NarrativeStructureService().preview(session, project.id) == before


def test_summary_only_change_keeps_sync_idempotent(session, project):
    item = scene(session, project, 1, summary="before")
    service = NarrativeStructureService(); revision, _ = service.sync(session, project.id)
    item.summary = "after"; session.flush()
    repeated, existing = service.sync(session, project.id)
    assert existing and repeated.id == revision.id


def test_source_max_sequence_tracks_current_history(session, project):
    for sequence in range(1, 4): scene(session, project, sequence)
    result = NarrativeStructureService().preview(session, project.id)
    assert result["source_max_sequence"] == 3


def test_materialized_revision_uses_preview_semantic_fingerprint(session, project):
    for sequence in range(1, 4): scene(session, project, sequence)
    service = NarrativeStructureService(); preview = service.preview(session, project.id, config())
    revision, _ = service.sync(session, project.id, config())
    assert revision.structure_fingerprint == preview["structure_fingerprint"]


def test_current_before_first_sync_is_empty_metadata(session, project):
    scene(session, project, 1)
    current = NarrativeStructureService().current(session, project.id)
    assert current["revision"] is None and current["chapters"] == [] and current["stale"] is False


def test_api_expected_source_fingerprint_mismatch_is_409(session, project, monkeypatch):
    scene(session, project, 1); session.commit()
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    response = TestClient(app).post(f"/projects/{project.id}/narrative-structure/sync", json={"expected_source_fingerprint": "stale"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "NARRATIVE_STRUCTURE_SOURCE_CHANGED"


def test_sync_mutates_only_phase12_tables(session, project):
    scene(session, project, 1); session.flush()
    frozen = (Scene, TimelineEvent, CausalLink, SceneStateCheckpoint, StoryArc)
    before = {model: session.scalar(select(func.count(model.id))) for model in frozen}
    NarrativeStructureService().sync(session, project.id)
    after = {model: session.scalar(select(func.count(model.id))) for model in frozen}
    assert after == before


@pytest.mark.parametrize("model", [Chapter, NarrativeArc, NarrativeVolume])
def test_active_structure_number_partial_unique(session, project, model):
    scene(session, project, 1); NarrativeStructureService().sync(session, project.id)
    current = session.scalar(select(model).where(model.project_id == project.id, model.active.is_(True)))
    values = {column.name: getattr(current, column.name) for column in model.__table__.columns if column.name != "id" and not column.primary_key}
    values["structure_revision_id"] = current.structure_revision_id
    session.add(model(**values))
    with pytest.raises(IntegrityError): session.commit()


@pytest.mark.parametrize("collision", ["ordinal", "scene"])
def test_chapter_scene_binding_uniqueness(session, project, collision):
    first = scene(session, project, 1); second = scene(session, project, 2)
    NarrativeStructureService().sync(session, project.id, config(chapter_boundary_threshold=99))
    chapter = session.scalar(select(Chapter).where(Chapter.active.is_(True)))
    if collision == "ordinal":
        third = scene(session, project, 3)
        duplicate = ChapterSceneBinding(chapter_id=chapter.id, scene_id=third.id, ordinal=1, scene_sequence=3)
    else:
        duplicate = ChapterSceneBinding(chapter_id=chapter.id, scene_id=first.id, ordinal=3, scene_sequence=1)
    session.add(duplicate)
    with pytest.raises(IntegrityError): session.commit()


def test_audit_rejects_binding_json_divergence(session, project):
    scene(session, project, 1); scene(session, project, 2)
    NarrativeStructureService().sync(session, project.id, config(chapter_boundary_threshold=99))
    chapter = session.scalar(select(Chapter).where(Chapter.active.is_(True)))
    chapter.source_scene_ids = list(reversed(chapter.source_scene_ids)); session.flush()
    with pytest.raises(ValueError, match="NARRATIVE_STRUCTURE_BINDING_INVALID"):
        NarrativeStructureAudit().audit(session, project.id)


def test_structure_sync_is_project_isolated(session, project):
    other = Project(name="Other"); session.add(other); session.flush()
    own_scene = scene(session, project, 1); other_scene = scene(session, other, 1)
    NarrativeStructureService().sync(session, project.id)
    chapter = session.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True)))
    assert chapter.source_scene_ids == [own_scene.id] and other_scene.id not in chapter.source_scene_ids


def test_structure_status_columns_are_non_native_varchar():
    assert Chapter.__table__.c.structure_status.type.native_enum is False
    assert NarrativeArc.__table__.c.status.type.native_enum is False
    assert NarrativeVolume.__table__.c.status.type.native_enum is False


def test_revision_history_is_retained_and_inactivated(session, project):
    scene(session, project, 1); service = NarrativeStructureService()
    first, _ = service.sync(session, project.id, config())
    second, _ = service.sync(session, project.id, config(chapter_boundary_threshold=7))
    rows = session.scalars(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project.id)).all()
    assert len(rows) == 2 and first.active is False and second.active is True


def test_rebuilt_tail_records_supersession_link(session, project):
    for sequence in range(1, 4): scene(session, project, sequence)
    service = NarrativeStructureService(); service.sync(session, project.id, config(chapter_boundary_threshold=99))
    old = session.scalar(select(Chapter).where(Chapter.active.is_(True)))
    scene(session, project, 4, thread="b", location="b", participants=["b"])
    service.sync(session, project.id, config(chapter_boundary_threshold=1))
    replacement = session.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True), Chapter.supersedes_chapter_id == old.id))
    assert replacement is not None and old.active is False
