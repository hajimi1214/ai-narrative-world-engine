from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Chapter, NarrativeArc, NarrativeStructureSceneFeature, Project, Scene
from app.narrative_structure import NarrativeStructureService
from app.narrative_structure import NarrativeStructureAudit
from app.narrative_structure_projection import (
    NarrativeStructureProjectionAudit, NarrativeStructureProjectionService,
)
from app.scaling import ProjectHistoryProjectionService


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def make_scene(db, project, sequence, *, location="room-a", thread="thread-a"):
    item = Scene(
        project_id=project.id, sequence=sequence,
        world_time=datetime(2026, 1, 1) + timedelta(hours=sequence),
        location=location, participants=["character-a"], facts=[], result={},
        story_threads=[thread], status="OCCURRED", history_status="ACTIVE",
    )
    db.add(item); db.flush()
    return item


def history_then_structure(db, project, scene):
    ProjectHistoryProjectionService().sync_after_scene_commit(db, project.id, scene.id)
    NarrativeStructureProjectionService().sync_after_scene_commit(db, project.id, scene.id)


def test_first_scene_append_builds_ready_feature_projection(session):
    project = Project(name="D2"); session.add(project); session.flush()
    scene = make_scene(session, project, 1)
    history_then_structure(session, project, scene)
    status = NarrativeStructureProjectionService().status(session, project.id)
    assert status["status"] == "READY", repr(status)
    assert status["built_through_sequence"] == 1
    assert status["scene_feature_count"] == 1
    NarrativeStructureProjectionAudit().audit(session, project.id)


def test_incremental_append_matches_authoritative_feature_contract(session):
    project = Project(name="D2"); session.add(project); session.flush()
    first = make_scene(session, project, 1)
    history_then_structure(session, project, first)
    second = make_scene(session, project, 2, location="room-b", thread="thread-b")
    history_then_structure(session, project, second)
    rows = session.scalars(select(NarrativeStructureSceneFeature).where(
        NarrativeStructureSceneFeature.project_id == project.id,
    ).order_by(NarrativeStructureSceneFeature.sequence)).all()
    assert [item.scene_id for item in rows] == [first.id, second.id]
    assert NarrativeStructureProjectionService().status(session, project.id)["built_through_sequence"] == 2
    NarrativeStructureProjectionAudit().audit(session, project.id)


def test_missing_projection_after_history_marks_dirty(session):
    project = Project(name="D2"); session.add(project); session.flush()
    first = make_scene(session, project, 1)
    ProjectHistoryProjectionService().sync_after_scene_commit(session, project.id, first.id)
    second = make_scene(session, project, 2)
    ProjectHistoryProjectionService().sync_after_scene_commit(session, project.id, second.id)
    NarrativeStructureProjectionService().sync_after_scene_commit(session, project.id, second.id)
    status = NarrativeStructureProjectionService().status(session, project.id)
    assert status["status"] == "DIRTY" and status["dirty_from_sequence"] == 1


def test_explicit_sync_adopts_full_projection_and_config_change_stales_fast_path(session):
    project = Project(name="D2"); session.add(project); session.flush()
    scene = make_scene(session, project, 1)
    revision, _ = NarrativeStructureService().sync(session, project.id)
    status = NarrativeStructureProjectionService().status(session, project.id)
    assert status["status"] == "READY" and status["active_revision_id"] == revision.id
    project.autonomy_settings = {"narrative_structure": {"chapter_max_scenes": 7}}
    session.flush()
    assert not NarrativeStructureProjectionService().status(session, project.id)["fast_path_available"]
    rebuilt, existing = NarrativeStructureService().sync(session, project.id)
    assert not existing and rebuilt.id != revision.id
    assert NarrativeStructureProjectionService().status(session, project.id)["fast_path_available"]


@pytest.mark.parametrize("column,value", [
    ("location_id", "tampered"),
    ("participant_ids", ["tampered"]),
    ("thread_ids", ["tampered"]),
    ("checkpoint_fingerprint", "tampered"),
])
def test_projection_audit_detects_feature_tamper(session, column, value):
    project = Project(name="D2"); session.add(project); session.flush()
    scene = make_scene(session, project, 1)
    NarrativeStructureService().sync(session, project.id)
    feature = session.scalar(select(NarrativeStructureSceneFeature).where(
        NarrativeStructureSceneFeature.project_id == project.id,
    ))
    setattr(feature, column, value); session.flush()
    with pytest.raises(ValueError, match="NARRATIVE_STRUCTURE_PROJECTION_INTEGRITY_INVALID"):
        NarrativeStructureProjectionAudit().audit(session, project.id)


def test_replay_history_change_marks_projection_dirty(session):
    project = Project(name="D2"); session.add(project); session.flush()
    make_scene(session, project, 1)
    NarrativeStructureService().sync(session, project.id)
    NarrativeStructureProjectionService().sync_after_history_change(session, project.id, 1, "REPLAY_HISTORY_CHANGED")
    status = NarrativeStructureProjectionService().status(session, project.id)
    assert status["status"] == "DIRTY" and status["dirty_reason"] == "REPLAY_HISTORY_CHANGED"


def test_ready_projection_sync_advances_open_tail_with_full_preview_parity(session):
    project = Project(name="D2", autonomy_settings={"narrative_structure": {
        "chapter_min_scenes": 1, "chapter_target_scenes": 1, "chapter_max_scenes": 2,
        "chapter_boundary_threshold": 0, "arc_min_chapters": 1, "arc_max_chapters": 2,
        "arc_boundary_threshold": 0, "volume_min_arcs": 1, "volume_max_arcs": 2,
        "volume_boundary_threshold": 0,
    }})
    session.add(project); session.flush()
    first = make_scene(session, project, 1, location="a", thread="a")
    revision, _ = NarrativeStructureService().sync(session, project.id)
    sealed = session.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True)))
    second = make_scene(session, project, 2, location="b", thread="b")
    history_then_structure(session, project, second)
    updated, existing = NarrativeStructureService().sync(session, project.id)
    assert updated.id == revision.id and not existing and updated.protocol_version == 2
    assert session.get(Chapter, sealed.id).active is True
    preview = NarrativeStructureService().preview(session, project.id, updated.config)
    actual = NarrativeStructureService().payload(session, updated)
    assert [(row["start_sequence"], row["end_sequence"]) for row in actual["chapters"]] == [
        (row["start_sequence"], row["end_sequence"]) for row in preview["chapters"]
    ]
    assert [(row["start_sequence"], row["end_sequence"], row["dominant_thread_ids"], row["structure_metadata"]) for row in actual["volumes"]] == [
        (row["start_sequence"], row["end_sequence"], row["dominant_thread_ids"], row["structure_metadata"]) for row in preview["volumes"]
    ]
    NarrativeStructureAudit().audit(session, project.id)


def test_ready_tail_sync_does_not_build_full_source(session, monkeypatch):
    project = Project(name="D2")
    session.add(project); session.flush()
    first = make_scene(session, project, 1)
    revision, _ = NarrativeStructureService().sync(session, project.id)
    second = make_scene(session, project, 2)
    history_then_structure(session, project, second)

    def legacy_path_used(*_args, **_kwargs):
        raise AssertionError("FULL_STRUCTURE_SOURCE_USED")

    monkeypatch.setattr("app.narrative_structure.NarrativeStructureSourceFingerprintBuilder.build", legacy_path_used)
    updated, existing = NarrativeStructureService().sync(session, project.id)
    assert updated.id == revision.id and not existing and updated.source_max_sequence == 2


def test_v2_sync_retry_is_idempotent_without_legacy_source_rebuild(session, monkeypatch):
    project = Project(name="D2")
    session.add(project); session.flush()
    first = make_scene(session, project, 1)
    revision, _ = NarrativeStructureService().sync(session, project.id)
    second = make_scene(session, project, 2)
    history_then_structure(session, project, second)
    revision, changed = NarrativeStructureService().sync(session, project.id)
    assert not changed and revision.protocol_version == 2

    def legacy_path_used(*_args, **_kwargs):
        raise AssertionError("FULL_STRUCTURE_SOURCE_USED")

    monkeypatch.setattr("app.narrative_structure.NarrativeStructureSourceFingerprintBuilder.build", legacy_path_used)
    retried, existing = NarrativeStructureService().sync(session, project.id)
    assert existing and retried.id == revision.id


def test_repeated_tail_append_matches_full_formation_at_each_boundary(session):
    project = Project(name="D2", autonomy_settings={"narrative_structure": {
        "chapter_min_scenes": 1, "chapter_target_scenes": 2, "chapter_max_scenes": 3,
        "chapter_boundary_threshold": 1, "arc_min_chapters": 1, "arc_max_chapters": 2,
        "arc_boundary_threshold": 1, "volume_min_arcs": 1, "volume_max_arcs": 2,
        "volume_boundary_threshold": 1,
    }})
    session.add(project); session.flush()
    first = make_scene(session, project, 1, location="a", thread="a")
    revision, _ = NarrativeStructureService().sync(session, project.id)
    for sequence in range(2, 9):
        item = make_scene(
            session, project, sequence,
            location="a" if sequence in {2, 5, 6} else "b",
            thread="a" if sequence in {2, 5, 6} else "b",
        )
        history_then_structure(session, project, item)
        revision, changed = NarrativeStructureService().sync(session, project.id)
        assert not changed
        preview = NarrativeStructureService().preview(session, project.id, revision.config)
        actual = NarrativeStructureService().payload(session, revision)
        assert [
            (row["number"], row["structure_status"], row["start_sequence"], row["end_sequence"], row["source_scene_ids"])
            for row in actual["chapters"]
        ] == [
            (row["number"], row["status"], row["start_sequence"], row["end_sequence"], row["scene_ids"])
            for row in preview["chapters"]
        ]
        assert [
            (row["number"], row["status"], row["start_sequence"], row["end_sequence"], row["dominant_thread_ids"])
            for row in actual["narrative_arcs"]
        ] == [
            (row["number"], row["status"], row["start_sequence"], row["end_sequence"], row["dominant_thread_ids"])
            for row in preview["narrative_arcs"]
        ]
        assert [
            (row["number"], row["status"], row["start_sequence"], row["end_sequence"], row["dominant_thread_ids"])
            for row in actual["volumes"]
        ] == [
            (row["number"], row["status"], row["start_sequence"], row["end_sequence"], row["dominant_thread_ids"])
            for row in preview["volumes"]
        ]
        arcs = session.scalars(select(NarrativeArc).where(
            NarrativeArc.project_id == project.id, NarrativeArc.active.is_(True),
        ).order_by(NarrativeArc.number)).all()
        assert [(row.number, row.structure_fingerprint, row.structure_revision_id) for row in arcs] == [
            (row["number"], row["structure_fingerprint"], revision.id) for row in preview["narrative_arcs"]
        ], sequence
        NarrativeStructureAudit().audit(session, project.id)
