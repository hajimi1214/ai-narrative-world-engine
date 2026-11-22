"""Author archives must remain usable after every identity changes."""
import copy
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.fake import FakeModelProvider
from app.author_guided_volume import AuthorGuidedVolumeService
from app.backup import ProjectBackupService, _fingerprint
from app.db import Base
from app.formal_state import FormalStateIdentityService
from app.models import (
    AntiAIBible, AuthorGuidance, BookCompletionProposal, BookContract, CanonFact, Chapter,
    ChapterPlanningWindow, ChapterQualityAssessment, ChapterQualityFinding,
    ChapterWriterDraft, Character, ForeshadowingLedger, NarrativeStructureRevision,
    Project, ProjectProviderCredential, Scene, StoryPlan, StoryPlanChapter,
    VolumeContract, VolumeContinuitySnapshot, WorldEntity,
)
from app.narrative_structure import NarrativeStructureAudit, NarrativeStructureService
from app.quality import QualityContextBuilder, QualityGateService, QualityAssessmentAudit
from app.writer import WriterProjectionAudit, WriterProjectionService


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


def _resign(archive):
    archive.pop("fingerprint", None)
    archive["fingerprint"] = _fingerprint(archive)
    return archive


def _author_project(db, monkeypatch):
    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_chapter_evidence",
        lambda self, session, chapter: {"assessment_id": "fixture", "evidence": []},
    )
    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_chapter_scenes_published",
        staticmethod(lambda session, chapter: True),
    )
    monkeypatch.setattr(
        AuthorGuidedVolumeService,
        "_formal_world_matches_target",
        lambda self, session, volume: True,
    )
    project = Project(name="卷级恢复", story_seed="渡口的秘密")
    db.add(project); db.flush()
    location = WorldEntity(project_id=project.id, entity_type="LOCATION", name="渡口")
    actor = Character(project_id=project.id, name="林舟")
    db.add_all([location, actor]); db.flush()
    actor.current_state = {"location_id": location.id}
    fact = CanonFact(project_id=project.id, fact_type="CORE_CANON", proposition="渡口在午夜关闭。")
    db.add(fact); db.flush()
    service = AuthorGuidedVolumeService()
    contract = service.ensure_contract(db, project, {"title": "渡口", "premise": "找回信件", "author_locked_constraints": ["秘密不得在第二卷前揭晓"]})
    volume = service.ensure_volume(db, project, contract, {"volume_goal": "找回信件", "target_closing_state": {"letter": "found"}})
    volume.actual_chapter_start = 1; volume.actual_chapter_end = 1
    chapter = Chapter(project_id=project.id, number=1, active=True, status="ADOPTED", content="林舟找回信件。")
    db.add(chapter); db.flush()
    window = service.ensure_window(db, project, volume, size=1)
    service.ensure_window_tasks(db, project, volume, window, contract)
    task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == project.id))
    task.number = 1; task.end_state = {"letter": "found"}
    window.start_chapter_number = 1; window.end_chapter_number = 1; window.status = "COMPLETED"
    guidance = AuthorGuidance(project_id=project.id, volume_id=volume.id, window_id=window.id, author_note="保留人物的错误认知", analysis={"character_id": actor.id})
    foreshadow = ForeshadowingLedger(project_id=project.id, source_volume_id=volume.id, source_chapter_id=chapter.id, foreshadow_ref="letter-mark", title="信上的缺口", related_character_ids=[actor.id], related_fact_ids=[fact.id], earliest_payoff_volume=2)
    db.add_all([guidance, foreshadow]); db.flush()
    service.seal(db, volume, author_confirmed=True)
    snapshot = db.get(VolumeContinuitySnapshot, volume.sealed_snapshot_id)
    service.completion_proposal(db, contract, volume)
    second_volume = service.ensure_volume(db, project, contract, {"opening_state": {"source_volume_snapshot_id": snapshot.id}, "target_closing_state": {"letter": "decoded"}}, number=2)
    second_window = service.ensure_window(db, project, second_volume, size=2)
    service.ensure_window_tasks(db, project, second_volume, second_window, contract)
    db.add(ProjectProviderCredential(project_id=project.id, purpose="GENERATION", secret_ciphertext="synthetic-not-a-key", secret_fingerprint="synthetic", secret_hint="test"))
    db.commit()
    return project, actor, volume, snapshot, second_volume


def test_author_state_roundtrip_preserves_seals_and_continues_planning(db, monkeypatch):
    source, actor, first, original_snapshot, second = _author_project(db, monkeypatch)
    service = ProjectBackupService()
    archive = service.export(db, source.id)
    author_tables = {"book_contracts", "volume_contracts", "chapter_planning_windows", "volume_continuity_snapshots", "foreshadowing_ledger", "author_guidance", "book_completion_proposals"}
    assert author_tables.issubset(archive["tables"])
    assert archive["schema_version"] == 2
    assert "synthetic-not-a-key" not in json.dumps(archive)
    restored = service.restore(db, archive)
    db.commit()

    for model in (BookContract, VolumeContract, ChapterPlanningWindow, VolumeContinuitySnapshot, ForeshadowingLedger, AuthorGuidance, BookCompletionProposal):
        assert db.scalar(select(func.count()).select_from(model).where(model.project_id == restored.id)) == db.scalar(select(func.count()).select_from(model).where(model.project_id == source.id))
    new_actor = db.scalar(select(Character).where(Character.project_id == restored.id))
    volumes = db.scalars(select(VolumeContract).where(VolumeContract.project_id == restored.id).order_by(VolumeContract.volume_number)).all()
    sealed, current = volumes
    frozen = db.get(VolumeContinuitySnapshot, sealed.sealed_snapshot_id)
    assert sealed.status.value == "SEALED" and sealed.sealed_at == first.sealed_at
    assert frozen.id != original_snapshot.id
    assert set(frozen.character_states) == {new_actor.id}
    assert frozen.character_states[new_actor.id]["character_id"] == new_actor.id
    assert actor.id not in json.dumps(frozen.character_states)
    assert frozen.source_fingerprint != original_snapshot.source_fingerprint
    assert sealed.fingerprint != first.fingerprint
    assert current.opening_state["source_volume_snapshot_id"] == frozen.id
    new_window = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == current.id))
    assert new_window.source_volume_snapshot_id == frozen.id
    plan = db.scalar(select(StoryPlan).where(StoryPlan.project_id == restored.id, StoryPlan.generation_report["author_window_id"].as_string() == new_window.id))
    assert new_window.plan_fingerprint == plan.source_fingerprint
    proposal = db.scalar(select(BookCompletionProposal).where(BookCompletionProposal.project_id == restored.id))
    assert proposal.evidence_chapter_ids == [db.scalar(select(Chapter.id).where(Chapter.project_id == restored.id))]
    assert service.export(db, restored.id)["tables"]  # Restored state itself is exportable.

    author = AuthorGuidedVolumeService()
    before = copy.deepcopy(frozen.character_states)
    assert author.seal(db, sealed, author_confirmed=True).sealed_snapshot_id == frozen.id
    context = author.continuity_context(db, current)
    assert context["source_snapshot_id"] == frozen.id and new_actor.id in context["character_states"]
    task_ids = author.ensure_window_tasks(db, restored, current, new_window, db.scalar(select(BookContract).where(BookContract.project_id == restored.id)))
    assert len(task_ids) == 2
    assert all(db.get(StoryPlanChapter, task_id).project_id == restored.id for task_id in task_ids)
    followup = author.ensure_followup_window(db, restored, current, new_window, size=2)
    assert followup.source_volume_snapshot_id == frozen.id
    assert frozen.character_states == before
    FormalStateIdentityService().audit(db, restored.id)
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def _written_project(db):
    project = Project(name="草稿历史", autonomy_settings={"quality_gate": {"require_critic": False}})
    db.add(project); db.flush()
    db.add(AntiAIBible(project_id=project.id, version=1, active=True, warning_expressions=["林舟"]))
    actor = Character(project_id=project.id, name="林舟")
    db.add(actor); db.flush()
    scene = Scene(project_id=project.id, sequence=1, participants=[actor.id], location="渡口", facts=[], result={}, status="OCCURRED", history_status="ACTIVE", world_time=datetime(2026, 1, 1))
    db.add(scene); db.flush()
    NarrativeStructureService().sync(db, project.id)
    chapter = db.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True)))
    writer = WriterProjectionService()
    prose = "林舟推开门，把信放在桌上。"
    payload = {"chapter_title": "来信", "prose": prose, "scene_coverage": chapter.source_scene_ids, "source_refs": [], "pov_character_id": None}
    draft = writer.render(db, chapter.id, {"pov_mode": "OBJECTIVE"}, provider=FakeModelProvider(json.dumps(payload)), model="fake-writer")
    writer.adopt(db, draft.id)
    assessment = QualityGateService().assess(db, chapter.id)
    QualityGateService().approve(db, assessment.id)
    # Add another historical version, with a repair-assessment cycle in nullable
    # FKs and a deliberately different unused model output.
    historical = copy.deepcopy({column.key: getattr(draft, column.key) for column in ChapterWriterDraft.__table__.columns})
    historical.update(id=None, version=2, status="STALE", client_request_id="history", parent_draft_id=draft.id, source_quality_assessment_id=assessment.id)
    old = ChapterWriterDraft(**historical)
    db.add(old); db.flush()
    draft.supersedes_draft_id = old.id
    db.commit()
    return project, chapter, draft, assessment


def test_writer_quality_cycles_restore_history_and_allow_fresh_review(db):
    project, chapter, draft, assessment = _written_project(db)
    restored = ProjectBackupService().restore(db, ProjectBackupService().export(db, project.id))
    db.commit()
    new_chapter = db.scalar(select(Chapter).where(Chapter.project_id == restored.id, Chapter.active.is_(True)))
    new_draft = db.get(ChapterWriterDraft, new_chapter.current_writer_draft_id)
    new_assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.project_id == restored.id))
    finding = db.scalar(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == new_assessment.id))
    assert finding.finding_metadata == {"expression": "林舟"}
    assert new_draft.id != draft.id and new_draft.content == draft.content
    assert db.scalar(select(func.count(ChapterWriterDraft.id)).where(ChapterWriterDraft.project_id == restored.id)) == 2
    historical = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.project_id == restored.id, ChapterWriterDraft.version == 2))
    assert historical.parent_draft_id == new_draft.id and new_draft.supersedes_draft_id == historical.id
    assert historical.source_quality_assessment_id == new_assessment.id
    assert new_chapter.current_quality_assessment_id is None and new_chapter.status == "ADOPTED"
    assert new_assessment.active is False and new_assessment.stale_at is not None
    assert new_assessment.status.value == "PASS"  # History retains the original decision.
    QualityAssessmentAudit().audit(db, new_assessment.id)
    assert new_draft.validation_report["backup_restore"]["original_fingerprints"]["writer_context_fingerprint"] == draft.writer_context_fingerprint
    assert WriterProjectionAudit().audit(db, new_chapter.id)["valid"]
    NarrativeStructureAudit().audit(db, restored.id)
    context = QualityContextBuilder().build(db, new_chapter.id)
    assert context["writer_draft"].id == new_draft.id
    fresh = QualityGateService().assess(db, new_chapter.id, {"idempotency_key": "after-restore"})
    assert fresh.status.value == "PASS" and fresh.id != new_assessment.id
    QualityGateService().approve(db, fresh.id)
    QualityAssessmentAudit().audit(db, fresh.id)
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def test_legacy_v1_archive_with_missing_draft_history_still_restores(db):
    project, chapter, draft, assessment = _written_project(db)
    archive = ProjectBackupService().export(db, project.id)
    archive["schema_version"] = 1
    legacy_tables = {"projects", "characters", "scenes", "chapters"}
    archive["tables"] = {key: rows for key, rows in archive["tables"].items() if key in legacy_tables}
    restored = ProjectBackupService().restore(db, _resign(archive))
    db.commit()
    copy_chapter = db.scalar(select(Chapter).where(Chapter.project_id == restored.id))
    assert copy_chapter.content == chapter.content and copy_chapter.status == "ADOPTED"
    assert copy_chapter.current_writer_draft_id is None and copy_chapter.current_quality_assessment_id is None
    assert copy_chapter.structure_revision_id is None
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []


@pytest.mark.parametrize("tamper", ["owner", "fk", "json_reference", "json_key", "duplicate", "missing_sealed_snapshot"])
def test_restore_rejects_foreign_or_dangling_author_references_before_insertion(db, tamper, monkeypatch):
    source, actor, volume, snapshot, second = _author_project(db, monkeypatch)
    outsider = Project(name="外来项目")
    db.add(outsider); db.flush()
    outsider_actor = Character(project_id=outsider.id, name="外来角色")
    db.add(outsider_actor); db.commit()
    archive = ProjectBackupService().export(db, source.id)
    if tamper == "owner":
        archive["tables"]["volume_contracts"][0]["project_id"] = outsider.id
    elif tamper == "fk":
        archive["tables"]["author_guidance"][0]["volume_id"] = outsider.id
    elif tamper == "json_reference":
        archive["tables"]["foreshadowing_ledger"][0]["related_character_ids"] = [outsider_actor.id]
    elif tamper == "json_key":
        archive["tables"]["volume_continuity_snapshots"][0]["character_states"] = {outsider_actor.id: {"name": "外来角色"}}
    elif tamper == "duplicate":
        archive["tables"]["characters"].append(copy.deepcopy(archive["tables"]["characters"][0]))
    else:
        archive["tables"].pop("volume_continuity_snapshots")
    before = db.scalar(select(func.count(Project.id)))
    with pytest.raises(ValueError, match="BACKUP_(PROJECT_REFERENCE_INVALID|REFERENCE_INVALID|DUPLICATE_ID|SEALED_SNAPSHOT_MISSING)"):
        ProjectBackupService().restore(db, _resign(archive))
    assert db.scalar(select(func.count(Project.id))) == before


def test_restore_database_failure_rolls_back_only_the_new_copy(db):
    project = Project(name="原作")
    db.add(project); db.commit()
    archive = ProjectBackupService().export(db, project.id)
    archive["tables"]["characters"] = [{"id": "invalid-row", "project_id": project.id, "name": None}]
    before = db.scalar(select(func.count(Project.id)))
    with pytest.raises(ValueError, match="BACKUP_RESTORE_INVALID"):
        ProjectBackupService().restore(db, _resign(archive))
    assert db.scalar(select(func.count(Project.id))) == before
    assert db.get(Project, project.id).name == "原作"


@pytest.mark.parametrize("requires_resolution", [False, True])
def test_committed_engine_scene_restores_evidence_and_continues_writer(db, monkeypatch, requires_resolution):
    # This fixture runs the production delta derivation/validation path. Commit
    # then creates its real compact checkpoint, causal ledger and D2 structure.
    from test_scene_commit import prepared_commit
    from app.causal_ledger import CurrentCausalLedgerAudit
    from app.historical import SceneCheckpointIntegrityValidator
    from app.models import AutonomousWorldRun, SceneStateCheckpoint
    from app.scene_commit import SceneCommitService
    from app.writer import WriterChapterSourceBuilder

    project, location, actor, other, proposal, performance, turn, resolution, batch, _ = prepared_commit(db, monkeypatch, requires_resolution=requires_resolution)
    SceneCommitService().commit(db, project.id, performance.id)
    db.commit()
    archive = ProjectBackupService().export(db, project.id)
    assert {"scene_proposals", "scene_performances", "scene_state_checkpoints", "world_snapshots", "scene_commits"}.issubset(archive["tables"])
    restored = ProjectBackupService().restore(db, archive)
    db.commit()
    NarrativeStructureAudit().audit(db, restored.id)
    CurrentCausalLedgerAudit().audit(db, restored.id)
    checkpoint = db.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == restored.id))
    SceneCheckpointIntegrityValidator().validate_integrity(db, checkpoint)
    chapter = db.scalar(select(Chapter).where(Chapter.project_id == restored.id, Chapter.active.is_(True)))
    source = WriterChapterSourceBuilder().build(db, chapter)
    assert source["scenes"][0]["turns"] and source["scenes"][0]["legacy_source"] is False
    if requires_resolution:
        assert source["scenes"][0]["state_delta_items"]
    payload = {"chapter_title": "恢复后续写", "prose": "门开了，林舟走进房间。", "scene_coverage": chapter.source_scene_ids, "source_refs": [], "pov_character_id": None}
    draft = WriterProjectionService().render(db, chapter.id, {"pov_mode": "OBJECTIVE"}, provider=FakeModelProvider(json.dumps(payload)), model="fake-writer")
    WriterProjectionService().adopt(db, draft.id)
    assert WriterProjectionAudit().audit(db, chapter.id)["valid"]
    assert db.scalar(select(func.count(AutonomousWorldRun.id)).where(AutonomousWorldRun.project_id == restored.id)) == 0
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def test_full_auto_fake_engine_backup_restores_written_chapters(monkeypatch):
    # Reuse the existing full-chain scenario as a source factory: it performs
    # real direction/plan/agent/Writer/quality/adoption services with fake LLMs.
    from test_auto_director import test_full_auto_pauses_at_operational_budget_without_completing_book as run_fake_book
    import app.api as api
    from app.causal_ledger import CurrentCausalLedgerAudit
    from app.models import AutoDirectorRun, AutonomousWorldRun

    run_fake_book(monkeypatch)
    with api.SessionLocal() as db:
        source = db.scalar(select(Project))
        service = ProjectBackupService()
        archive = service.export(db, source.id)
        source_tables = copy.deepcopy(archive["tables"])
        restored = service.restore(db, archive)
        db.commit()
        NarrativeStructureAudit().audit(db, restored.id)
        CurrentCausalLedgerAudit().audit(db, restored.id)
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == restored.id, Chapter.active.is_(True), Chapter.content.is_not(None)).order_by(Chapter.number)).all()
        assert [chapter.number for chapter in chapters] == [1, 2]
        for chapter in chapters:
            assert WriterProjectionService().preview(db, chapter.id, {"pov_mode": "OBJECTIVE"})["source_counts"]["scenes"]
            assert WriterProjectionAudit().audit(db, chapter.id)["valid"]
        chapter = chapters[0]
        prior = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
        payload = {"chapter_title": chapter.title, "prose": chapter.content, "scene_coverage": chapter.source_scene_ids, "source_refs": [], "pov_character_id": prior.pov_character_id, "task_coverage": prior.validation_report.get("task_coverage", []), "task_forbidden_hits": []}
        rewritten = WriterProjectionService().render(db, chapter.id, {"pov_mode": prior.pov_mode.value, "pov_character_id": prior.pov_character_id}, provider=FakeModelProvider(json.dumps(payload)), model="fake-writer-after-import")
        WriterProjectionService().adopt(db, rewritten.id)
        scores = {key: 95 for key in ("factual_grounding", "pov_compliance", "reveal_safety", "style_naturalness", "repetition", "pacing", "voice_consistency", "overall")}
        review = QualityGateService().assess(db, chapter.id, provider=FakeModelProvider(json.dumps({"decision": "PASS", "scores": scores, "findings": []})), model="fake-critic-after-import")
        QualityGateService().approve(db, review.id)
        QualityAssessmentAudit().audit(db, review.id)
        assert service.export(db, source.id)["tables"] == source_tables
        assert db.scalar(select(func.count(AutoDirectorRun.id)).where(AutoDirectorRun.project_id == restored.id)) == 0
        assert db.scalar(select(func.count(AutonomousWorldRun.id)).where(AutonomousWorldRun.project_id == restored.id)) == 0


def test_historical_snapshot_rows_get_new_ids_without_resurrecting_deleted_state(db):
    from app.models import WorldSnapshot
    from app.snapshot_storage import SnapshotPayloadResolver
    from app.versioning import WorldSnapshotBuilder

    source = Project(name="历史快照")
    db.add(source); db.flush()
    former_fact = CanonFact(project_id=source.id, fact_type="WORLD_FACT", proposition="旧规则")
    db.add(former_fact); db.flush()
    original_snapshot = WorldSnapshotBuilder().create(db, source.id, "BASELINE")
    old_fact_id = former_fact.id
    db.delete(former_fact); db.commit()
    restored = ProjectBackupService().restore(db, ProjectBackupService().export(db, source.id))
    db.commit()
    snapshots = db.scalars(select(WorldSnapshot).where(WorldSnapshot.project_id == restored.id)).all()
    historical = next(snapshot for snapshot in snapshots if snapshot.storage_mode.value == "LEGACY_FULL")
    payload = SnapshotPayloadResolver().materialize(db, historical)
    assert payload["canon_facts"][0]["id"] != old_fact_id
    assert payload["canon_facts"][0]["project_id"] == restored.id
    assert db.scalar(select(func.count(CanonFact.id)).where(CanonFact.project_id == restored.id)) == 0
    assert old_fact_id == original_snapshot.payload["canon_facts"][0]["id"]


def test_unsupported_replay_history_is_rejected_before_export_or_restore(db, monkeypatch):
    from test_scene_commit import prepared_commit
    from app.scene_commit import SceneCommitService

    project, _, _, _, _, performance, *_ = prepared_commit(db, monkeypatch, requires_resolution=False)
    result = SceneCommitService().commit(db, project.id, performance.id)
    db.commit()
    archive = ProjectBackupService().export(db, project.id)
    archive["tables"]["scene_state_checkpoints"][0]["origin"] = "REPLAY_COMMIT"
    with pytest.raises(ValueError, match="BACKUP_REPLAY_HISTORY_UNSUPPORTED"):
        ProjectBackupService().restore(db, _resign(archive))
    result.checkpoint.origin = "REPLAY_COMMIT"
    db.flush()
    with pytest.raises(ValueError, match="BACKUP_REPLAY_HISTORY_UNSUPPORTED"):
        ProjectBackupService().export(db, project.id)
