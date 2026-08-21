from datetime import datetime, timedelta
from uuid import uuid4

import json

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.ai.fake import FakeModelProvider
from app.models import (
    Chapter, ChapterQualityAssessment, ChapterStructureStatus, ChapterWriterDraft,
    NarrativeArc, NarrativeStructureProjectionStatus,
    NarrativeStructureRevision, NarrativeStructureSceneFeature, Project,
    ProjectNarrativeStructureProjection, Scene,
)
from app.narrative_structure import NarrativeStructureService
from app.narrative_structure import NarrativeStructureAudit
from app.narrative_structure_projection import (
    NarrativeStructureProjectionAudit, NarrativeStructureProjectionService,
)
from app.quality import QualityGateService
from app.scaling import ProjectHistoryProjectionService
from app.writer import WriterProjectionService


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


def test_explicit_scene_append_keeps_full_snapshot_out_of_projection_path(session, monkeypatch):
    from app.versioning import WorldSnapshotBuilder
    project = Project(name="D2"); session.add(project); session.flush()
    first = make_scene(session, project, 1)
    revision, _ = NarrativeStructureService().sync(session, project.id)
    second = make_scene(session, project, 2, location="room-b", thread="thread-b")
    ProjectHistoryProjectionService().sync_after_scene_commit(session, project.id, second.id)

    def full_snapshot_forbidden(*_args, **_kwargs):
        raise AssertionError("FULL_FORMAL_SNAPSHOT_USED")

    monkeypatch.setattr(WorldSnapshotBuilder, "build", full_snapshot_forbidden)
    NarrativeStructureProjectionService().sync_after_scene_commit(session, project.id, second.id)
    current = NarrativeStructureService().current(session, project.id)
    assert current["revision"]["id"] == revision.id
    assert current["revision"]["source_max_sequence"] == 2


def test_scene_append_failure_marks_projection_dirty_without_touching_scene(session, monkeypatch):
    project = Project(name="D2"); session.add(project); session.flush()
    first = make_scene(session, project, 1)
    NarrativeStructureService().sync(session, project.id)
    second = make_scene(session, project, 2)
    ProjectHistoryProjectionService().sync_after_scene_commit(session, project.id, second.id)

    def fail_tail(*_args, **_kwargs):
        raise RuntimeError("simulated projection failure")

    monkeypatch.setattr(NarrativeStructureProjectionService, "_append_structure", fail_tail)
    NarrativeStructureProjectionService().sync_after_scene_commit(session, project.id, second.id)
    status = NarrativeStructureProjectionService().status(session, project.id)
    assert session.get(Scene, second.id).history_status == "ACTIVE"
    assert status["status"] == "DIRTY"
    assert status["dirty_reason"].startswith("APPEND_FAILED:")


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
    assert updated.id == revision.id and existing and updated.protocol_version == 2
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
    assert updated.id == revision.id and existing and updated.source_max_sequence == 2


def test_v2_sync_retry_is_idempotent_without_legacy_source_rebuild(session, monkeypatch):
    project = Project(name="D2")
    session.add(project); session.flush()
    first = make_scene(session, project, 1)
    revision, _ = NarrativeStructureService().sync(session, project.id)
    second = make_scene(session, project, 2)
    history_then_structure(session, project, second)
    revision, changed = NarrativeStructureService().sync(session, project.id)
    assert changed and revision.protocol_version == 2

    def legacy_path_used(*_args, **_kwargs):
        raise AssertionError("FULL_STRUCTURE_SOURCE_USED")

    monkeypatch.setattr("app.narrative_structure.NarrativeStructureSourceFingerprintBuilder.build", legacy_path_used)
    retried, existing = NarrativeStructureService().sync(session, project.id)
    assert existing and retried.id == revision.id


def test_history_suffix_rebuild_touching_sealed_structure_fails_closed(session):
    project = Project(name="D2", autonomy_settings={"narrative_structure": {
        "chapter_min_scenes": 1, "chapter_target_scenes": 1, "chapter_max_scenes": 2,
        "chapter_boundary_threshold": 0, "arc_min_chapters": 1, "arc_max_chapters": 2,
        "arc_boundary_threshold": 0, "volume_min_arcs": 1, "volume_max_arcs": 2,
        "volume_boundary_threshold": 0,
    }})
    session.add(project); session.flush()
    for sequence in range(1, 7):
        make_scene(session, project, sequence, location="a" if sequence % 2 else "b", thread="a" if sequence % 2 else "b")
    revision, _ = NarrativeStructureService().sync(session, project.id)
    prefix = session.scalar(select(Chapter).where(
        Chapter.project_id == project.id, Chapter.active.is_(True), Chapter.end_sequence == 2,
    ))
    writer_payload = json.dumps({
        "chapter_title": "Sealed prefix", "prose": "The first boundary holds.",
        "scene_coverage": list(prefix.source_scene_ids), "source_refs": [],
        "pov_character_id": None,
    })
    draft = WriterProjectionService().render(
        session, prefix.id, {"pov_mode": "OBJECTIVE"},
        provider=FakeModelProvider(writer_payload), model="fixture-writer",
    )
    WriterProjectionService().adopt(session, draft.id)
    assessment = QualityGateService().assess(
        session, prefix.id, {"config": {"require_critic": False}},
    )
    QualityGateService().approve(session, assessment.id)
    protected = {
        "chapter_id": prefix.id,
        "draft_id": draft.id,
        "assessment_id": assessment.id,
        "scene_ids": list(prefix.source_scene_ids),
    }
    changed = session.scalar(select(Scene).where(Scene.project_id == project.id, Scene.sequence == 3))
    changed.location = "changed-location"; session.flush()
    service = NarrativeStructureProjectionService()
    service.sync_after_history_change(session, project.id, 3, "REPLAY_HISTORY_CHANGED")
    assert not service.rebuild_suffix_after_history_change(session, project.id, 3)
    status = service.status(session, project.id)
    assert status["status"] == "DIRTY"
    assert status["dirty_reason"] == "HISTORY_REBUILD_TOUCHES_SEALED"
    current = session.get(Chapter, protected["chapter_id"])
    assert current.active is True
    assert current.source_scene_ids == protected["scene_ids"]
    assert current.current_writer_draft_id == protected["draft_id"]
    assert current.current_quality_assessment_id == protected["assessment_id"]
    assert session.get(ChapterWriterDraft, protected["draft_id"]).chapter_id == current.id
    assert session.get(ChapterQualityAssessment, protected["assessment_id"]).chapter_id == current.id


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
        assert changed
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


def test_open_tail_append_is_bounded_across_large_sealed_prefixes(session):
    """A normal append may read its bounded open tail, never the sealed prefix."""

    def append_counts(sealed_count):
        config_data = {
            "chapter_min_scenes": 2, "chapter_target_scenes": 200_000,
            "chapter_max_scenes": 200_000, "chapter_boundary_threshold": 999,
        }
        project = Project(name=f"D2 scale {sealed_count}", autonomy_settings={"narrative_structure": config_data})
        session.add(project); session.flush()
        revision = NarrativeStructureRevision(
            project_id=project.id, active=True, protocol_version=2,
            source_history_fingerprint="scale", source_max_sequence=sealed_count,
            config=config_data, config_fingerprint="", rebuild_from_sequence=1,
            structure_fingerprint="scale",
        )
        session.add(revision); session.flush()
        from app.narrative_structure import NarrativeStructureConfig
        from app.narrative_structure_projection import _config_fingerprint, _empty_accumulator
        revision.config_fingerprint = _config_fingerprint(NarrativeStructureConfig.resolve(project))
        projection = ProjectNarrativeStructureProjection(
            project_id=project.id, protocol_version="narrative-structure-projection-v1",
            status=NarrativeStructureProjectionStatus.READY,
            config_fingerprint=revision.config_fingerprint,
            source_feature_fingerprint="scale", feature_accumulator=_empty_accumulator(),
            structure_fingerprint="scale", active_revision_id=revision.id,
            built_through_sequence=sealed_count, sealed_through_sequence=sealed_count - 1,
            tail_start_sequence=sealed_count,
        )
        session.add(projection)
        prefix_rows = [{
            "id": str(uuid4()), "project_id": project.id, "number": number,
            "source_scene_ids": [], "word_count": 0, "quality_report": {}, "status": "DRAFT",
            "structure_revision_id": revision.id, "active": True, "structure_status": "SEALED",
            "start_sequence": number, "end_sequence": number,
            "boundary_metadata": {},
        } for number in range(1, sealed_count)]
        session.execute(Chapter.__table__.insert(), prefix_rows)
        prior = make_scene(session, project, sealed_count)
        session.add(NarrativeStructureSceneFeature(
            project_id=project.id, scene_id=prior.id, sequence=sealed_count, active=True,
            world_time=None, location_id=prior.location, participant_ids=[], thread_ids=[],
            primary_thread_id=None, proposal_type=None, state_change_count=0,
            state_change_targets=[], state_change_paths=[], thread_state_event_ids=[],
            checkpoint_fingerprint=None, source_fingerprint="scale", feature_fingerprint="scale-tail",
        ))
        open_chapter = Chapter(
            project_id=project.id, number=sealed_count, source_scene_ids=[prior.id],
            word_count=0, quality_report={}, status="DRAFT", structure_revision_id=revision.id,
            active=True, structure_status=ChapterStructureStatus.PROVISIONAL,
            start_sequence=sealed_count, end_sequence=sealed_count,
            structure_fingerprint="open", boundary_metadata={},
        )
        session.add(open_chapter); session.flush()
        first_sealed_id = session.scalar(select(Chapter.id).where(
            Chapter.project_id == project.id, Chapter.number == 1,
        ))
        new_scene = make_scene(session, project, sealed_count + 1)
        queries: list[str] = []

        def capture(_conn, _cursor, statement, _params, _context, _executemany):
            queries.append(statement)

        event.listen(session.bind, "before_cursor_execute", capture)
        try:
            changed = NarrativeStructureProjectionService().prepare_for_scene_checkpoint(
                session, project.id, new_scene, [],
            )
        finally:
            event.remove(session.bind, "before_cursor_execute", capture)
        assert changed == [open_chapter.id]
        assert session.scalar(select(Chapter.id).where(Chapter.id == first_sealed_id)) == first_sealed_id
        assert open_chapter.source_scene_ids == [prior.id, new_scene.id]
        hydrated_chapters = [
            row for row in session.identity_map.values()
            if isinstance(row, Chapter) and row.project_id == project.id
        ]
        return len(queries), len(hydrated_chapters)

    small_queries, small_hydrated = append_counts(10_000)
    large_queries, large_hydrated = append_counts(100_000)
    # The two fixtures differ only in sealed-prefix cardinality. Normal tail
    # work must therefore stay identical rather than merely below a broad cap.
    assert (small_queries, small_hydrated) == (large_queries, large_hydrated)
    assert small_queries <= 30
    assert small_hydrated <= 3
