import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api
from app.ai.errors import MODEL_TIMEOUT, ModelProviderError
from app.ai.fake import FakeModelProvider
from app.db import Base
from app.main import app
from app.models import (
    Chapter, ChapterWriterDraft, Character, CharacterDecision, NarrativeStructureRevision, Project,
    RetconApplication, RetconApplicationStatus, Scene, WritingBible,
    SceneExecutionBinding, ScenePerformance, ScenePerformanceTurn, SceneProposal,
    SceneStateCheckpoint, StateDeltaBatch, StateDeltaItem, TimelineEvent,
    WorldResolution, WriterDraftStatus, WriterPOVMode,
)
from app.narrative_structure import NarrativeStructureService
from app.writer import (
    WriterChapterSourceBuilder, WriterContextBuilder, WriterDomainError,
    WriterGroundingValidator, WriterPOVResolver, WriterProjectionAudit,
    WriterProjectionService, WriterPromptBuilder, WriterRenderConfigResolver,
    WriterVisibilityProjector, WriterWordCounter,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


@pytest.fixture()
def writer_project(session):
    project = Project(name="Writer", min_chapter_words=None, max_chapter_words=None)
    session.add(project); session.flush()
    actor = Character(project_id=project.id, name="Lin")
    session.add(actor); session.flush()
    scene = Scene(project_id=project.id, sequence=1, world_time=datetime(2026, 1, 1), location="hall", participants=[actor.id], facts=[{"subject": actor.id, "predicate": "present", "value": True}], result={"outcome": "quiet"}, summary="forbidden summary", intent="forbidden intent", story_threads=[], status="OCCURRED", history_status="ACTIVE")
    session.add(scene); session.flush()
    NarrativeStructureService().sync(session, project.id)
    session.commit()
    chapter = session.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True)))
    return project, actor, scene, chapter


def response(content="The hall remains quiet.", scene_ids=None, pov_character_id=None, **extra):
    return json.dumps({"prose": content, "chapter_title": "Quiet", "scene_coverage": scene_ids or [], "source_refs": [], "pov_character_id": pov_character_id, **extra})


def render(session, chapter, **request):
    request.setdefault("pov_mode", "OBJECTIVE")
    return WriterProjectionService().render(session, chapter.id, request, provider=FakeModelProvider(response(scene_ids=chapter.source_scene_ids)), model="fake-writer")


def grounded(context, **changes):
    value = {"prose": "prose", "chapter_title": None, "scene_coverage": [item["scene_id"] for item in context["source_manifest"]["scenes"]], "source_refs": [], "pov_character_id": context["pov_character_id"]}
    value.update(changes)
    return value


def test_word_counter_counts_cjk_characters():
    assert WriterWordCounter().count("你好世界") == 4


def test_word_counter_counts_latin_runs():
    assert WriterWordCounter().count("hello writer 2026") == 3


def test_word_counter_mixes_scripts():
    assert WriterWordCounter().count("你好 hello-world") == 4


def test_word_counter_empty_is_zero():
    assert WriterWordCounter().count(None) == 0


def test_writer_source_requires_chapter(session):
    with pytest.raises(WriterDomainError, match="CHAPTER_NOT_FOUND"):
        WriterChapterSourceBuilder().build(session, "missing")


def test_writer_source_uses_active_structure(writer_project, session):
    _, _, scene, chapter = writer_project
    source = WriterChapterSourceBuilder().build(session, chapter.id)
    assert source["source_scene_ids"] == [scene.id]


def test_writer_source_has_protocol(writer_project, session):
    source = WriterChapterSourceBuilder().build(session, writer_project[3].id)
    assert source["source_fingerprint"].startswith("writer-chapter-source-v1:")


def test_writer_source_legacy_fallback_is_structured(writer_project, session):
    source = WriterChapterSourceBuilder().build(session, writer_project[3].id)
    row = source["scenes"][0]
    assert row["legacy_source"] and row["legacy_facts"] and row["legacy_result"]


def test_summary_does_not_change_source(writer_project, session):
    _, _, scene, chapter = writer_project
    before = WriterChapterSourceBuilder().build(session, chapter.id)["source_fingerprint"]
    scene.summary = "different prose"; scene.intent = "also different"; session.flush()
    assert WriterChapterSourceBuilder().build(session, chapter.id)["source_fingerprint"] == before


def test_structured_fact_changes_source(writer_project, session):
    _, _, scene, chapter = writer_project
    before = WriterChapterSourceBuilder().build(session, chapter.id)["source_fingerprint"]
    scene.facts = [{"subject": "new", "predicate": "present", "value": True}]; session.flush()
    assert WriterChapterSourceBuilder().build(session, chapter.id)["source_fingerprint"] != before


def test_pending_replay_blocks_source(writer_project, session):
    project, _, _, chapter = writer_project
    session.add(RetconApplication(project_id=project.id, retcon_request_id="request", retcon_plan_id="plan", source_revision_id="revision", status=RetconApplicationStatus.APPLIED_PENDING_REPLAY, plan_basis_fingerprint="plan", pre_apply_world_fingerprint="world"))
    session.flush()
    with pytest.raises(WriterDomainError, match="RETCON_REPLAY_REQUIRED"):
        WriterChapterSourceBuilder().build(session, chapter.id)


def test_inactive_chapter_blocked(writer_project, session):
    chapter = writer_project[3]; chapter.active = False; session.flush()
    with pytest.raises(WriterDomainError, match="NARRATIVE_STRUCTURE_REQUIRED"):
        WriterChapterSourceBuilder().build(session, chapter.id)


def test_legacy_structure_chapter_blocked(writer_project, session):
    chapter = writer_project[3]; chapter.structure_status = "LEGACY"; session.flush()
    with pytest.raises(WriterDomainError, match="NARRATIVE_STRUCTURE_REQUIRED"):
        WriterChapterSourceBuilder().build(session, chapter.id)


def test_objective_pov_needs_no_character(writer_project, session):
    mode, character_id = WriterPOVResolver().resolve(session, writer_project[3], {"pov_mode": "OBJECTIVE"})
    assert mode == WriterPOVMode.OBJECTIVE and character_id is None


def test_limited_pov_requires_character(writer_project, session):
    with pytest.raises(WriterDomainError, match="WRITER_POV_REQUIRED"):
        WriterPOVResolver().resolve(session, writer_project[3], {"pov_mode": "THIRD_PERSON_LIMITED"})


def test_limited_pov_accepts_participant(writer_project, session):
    mode, character_id = WriterPOVResolver().resolve(session, writer_project[3], {"pov_mode": "THIRD_PERSON_LIMITED", "pov_character_id": writer_project[1].id})
    assert mode == WriterPOVMode.THIRD_PERSON_LIMITED and character_id == writer_project[1].id


def test_foreign_pov_rejected(writer_project, session):
    foreign = Project(name="Foreign"); session.add(foreign); session.flush(); actor = Character(project_id=foreign.id, name="Other"); session.add(actor); session.flush()
    with pytest.raises(WriterDomainError, match="WRITER_POV_REQUIRED"):
        WriterPOVResolver().resolve(session, writer_project[3], {"pov_mode": "FIRST_PERSON", "pov_character_id": actor.id})


def test_default_bible_protocol(writer_project, session):
    source = WriterChapterSourceBuilder().build(session, writer_project[3].id)
    context = WriterContextBuilder().build(session, source, {"pov_mode": "OBJECTIVE"})
    assert context["fingerprints"]["writing_bible"] == "writer-default-v1"


def test_active_writing_bible_is_used(writer_project, session):
    project = writer_project[0]; session.add(WritingBible(project_id=project.id, version=1, active=True, rules={"tone": "plain"})); session.flush()
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert context["writing_rules"] == {"tone": "plain"}


def test_multiple_active_bibles_fail_closed(writer_project, session):
    project = writer_project[0]; session.add_all([WritingBible(project_id=project.id, version=1, active=True, rules={}), WritingBible(project_id=project.id, version=2, active=True, rules={})]); session.flush()
    with pytest.raises(WriterDomainError, match="WRITING_BIBLE_AMBIGUOUS"):
        WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})


def test_writer_context_has_required_partitions(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert {"writing_rules", "chapter", "formal_history", "pov_subjective_context", "entity_labels", "rendering_contract", "source_manifest", "fingerprints"} <= set(context)


def test_context_excludes_scene_summary(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert "forbidden summary" not in json.dumps(context, default=str)


def test_context_excludes_scene_intent(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert "forbidden intent" not in json.dumps(context, default=str)


def test_grounding_accepts_empty_structured_claims(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context), context)["valid"]


def test_grounding_rejects_unknown_event(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    report = WriterGroundingValidator().validate(grounded(context, events=[{"event_id": "unknown"}]), context)
    assert report["issues"][0]["code"] == "WRITER_UNGROUNDED_EVENT"


def test_grounding_rejects_unknown_location(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context, locations=["elsewhere"]), context)["issues"][0]["code"] == "WRITER_UNGROUNDED_LOCATION"


def test_grounding_rejects_unknown_entity(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context, entities=["stranger"]), context)["issues"][0]["code"] == "WRITER_UNGROUNDED_ENTITY"


def test_grounding_rejects_unbound_action(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context, events=[{"action": "jumps"}]), context)["issues"][0]["code"] == "WRITER_UNGROUNDED_ACTION"


def test_grounding_rejects_unbound_knowledge(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context, knowledge=[{}]), context)["issues"][0]["code"] == "WRITER_UNGROUNDED_KNOWLEDGE"


def test_grounding_rejects_unbound_memory(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context, memories=[{}]), context)["issues"][0]["code"] == "WRITER_UNGROUNDED_MEMORY"


def test_grounding_rejects_unbound_outcome(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    assert WriterGroundingValidator().validate(grounded(context, outcomes=[{}]), context)["issues"][0]["code"] == "WRITER_UNGROUNDED_OUTCOME"


def test_render_creates_validated_draft(writer_project, session):
    draft = render(session, writer_project[3])
    assert draft.status == WriterDraftStatus.VALIDATED and draft.content == "The hall remains quiet."


def test_render_does_not_mutate_chapter(writer_project, session):
    chapter = writer_project[3]; render(session, chapter)
    assert chapter.content is None and chapter.current_writer_draft_id is None


def test_render_records_fake_provider_metadata(writer_project, session):
    draft = render(session, writer_project[3])
    assert draft.provider == "fake" and draft.model == "fake-writer" and draft.model_request_id == "fake-request"


def test_render_client_request_is_idempotent(writer_project, session):
    chapter = writer_project[3]
    first = render(session, chapter, client_request_id="same")
    second = render(session, chapter, client_request_id="same")
    assert first.id == second.id


def test_render_request_mismatch_is_blocked(writer_project, session):
    chapter = writer_project[3]; render(session, chapter, client_request_id="same")
    with pytest.raises(WriterDomainError, match="WRITER_REQUEST_MISMATCH"):
        render(session, chapter, client_request_id="same", config={"temperature": 1})


def test_render_without_key_allocates_versions(writer_project, session):
    chapter = writer_project[3]
    assert (render(session, chapter).version, render(session, chapter).version) == (1, 2)


def test_render_malformed_output_marks_failed(writer_project, session):
    draft = WriterProjectionService().render(session, writer_project[3].id, {"pov_mode": "OBJECTIVE"}, provider=FakeModelProvider("not-json"), model="fake")
    assert draft.status == WriterDraftStatus.FAILED and draft.validation_report["issues"][0]["code"] == "MODEL_OUTPUT_INVALID"


def test_render_provider_failure_marks_failed(writer_project, session):
    provider = FakeModelProvider(error=ModelProviderError(MODEL_TIMEOUT))
    draft = WriterProjectionService().render(session, writer_project[3].id, {"pov_mode": "OBJECTIVE"}, provider=provider, model="fake")
    assert draft.status == WriterDraftStatus.FAILED and draft.validation_report["issues"][0]["code"] == MODEL_TIMEOUT


def test_render_rejects_grounding_issue(writer_project, session):
    output = response(scene_ids=writer_project[3].source_scene_ids, source_refs=[{"source_type": "SCENE", "source_id": "fabricated"}])
    draft = WriterProjectionService().render(session, writer_project[3].id, {"pov_mode": "OBJECTIVE"}, provider=FakeModelProvider(output), model="fake")
    assert draft.status == WriterDraftStatus.REJECTED


def test_min_word_count_rejects_short_draft(writer_project, session):
    project, _, _, chapter = writer_project; project.min_chapter_words = 10; session.flush()
    assert render(session, chapter).status == WriterDraftStatus.REJECTED


def test_max_word_count_rejects_long_draft(writer_project, session):
    project, _, _, chapter = writer_project; project.max_chapter_words = 1; session.flush()
    assert render(session, chapter).status == WriterDraftStatus.REJECTED


def test_adopt_updates_chapter_atomically(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter)
    WriterProjectionService().adopt(session, draft.id)
    assert chapter.current_writer_draft_id == draft.id and chapter.content == draft.content and chapter.word_count == draft.word_count


def test_adopt_keeps_chapter_draft_status(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter); WriterProjectionService().adopt(session, draft.id)
    assert chapter.status == "DRAFT"


def test_adopt_keeps_quality_report(writer_project, session):
    chapter = writer_project[3]; chapter.quality_report = {"legacy": True}; draft = render(session, chapter); WriterProjectionService().adopt(session, draft.id)
    assert chapter.quality_report == {"legacy": True}


def test_adopt_rejects_failed_draft(writer_project, session):
    draft = WriterProjectionService().render(session, writer_project[3].id, {"pov_mode": "OBJECTIVE"}, provider=FakeModelProvider("bad"), model="fake")
    with pytest.raises(WriterDomainError, match="WRITER_DRAFT_NOT_VALIDATED"):
        WriterProjectionService().adopt(session, draft.id)


def test_untracked_chapter_content_requires_force(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter); chapter.content = "old untracked"; session.flush()
    with pytest.raises(WriterDomainError, match="CHAPTER_CONTENT_UNTRACKED"):
        WriterProjectionService().adopt(session, draft.id)


def test_force_replaces_untracked_content(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter); chapter.content = "old untracked"; session.flush()
    WriterProjectionService().adopt(session, draft.id, force_replace_untracked=True)
    assert chapter.content == draft.content


def test_second_adoption_supersedes_prior(writer_project, session):
    chapter = writer_project[3]; first = render(session, chapter); WriterProjectionService().adopt(session, first.id)
    second = render(session, chapter); WriterProjectionService().adopt(session, second.id)
    assert first.status == WriterDraftStatus.SUPERSEDED and second.status == WriterDraftStatus.ADOPTED


def test_projection_audit_accepts_tracked_chapter(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter); WriterProjectionService().adopt(session, draft.id)
    assert WriterProjectionAudit().audit(session, chapter.id)["valid"]


def test_projection_audit_accepts_untracked_empty_chapter(writer_project, session):
    assert WriterProjectionAudit().audit(session, writer_project[3].id) == {"valid": True, "tracked": False}


def test_projection_audit_detects_content_tamper(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter); WriterProjectionService().adopt(session, draft.id); chapter.content = "tampered"; session.flush()
    with pytest.raises(WriterDomainError, match="WRITER_PROJECTION_INVALID"):
        WriterProjectionAudit().audit(session, chapter.id)


def test_writer_draft_version_unique(writer_project, session):
    first = render(session, writer_project[3])
    duplicate = ChapterWriterDraft(**{column.name: getattr(first, column.name) for column in first.__table__.columns if column.name not in {"id", "created_at"}})
    duplicate.client_request_id = None; session.add(duplicate)
    with pytest.raises(IntegrityError): session.flush()


def test_writer_preview_is_read_only(writer_project, session):
    before = session.scalar(select(func.count(ChapterWriterDraft.id)))
    WriterProjectionService().preview(session, writer_project[3].id, {"pov_mode": "OBJECTIVE"})
    assert session.scalar(select(func.count(ChapterWriterDraft.id))) == before


def test_api_lists_and_reads_drafts(writer_project, session, monkeypatch):
    project, _, _, chapter = writer_project; draft = render(session, chapter); session.commit()
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    client = TestClient(app)
    assert client.get(f"/projects/{project.id}/chapters/{chapter.id}/writer/drafts").status_code == 200
    assert client.get(f"/projects/{project.id}/writer-drafts/{draft.id}").json()["content"] == draft.content


def test_api_cross_project_draft_is_404(writer_project, session, monkeypatch):
    project, _, _, chapter = writer_project; draft = render(session, chapter); other = Project(name="Other"); session.add(other); session.commit()
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    assert TestClient(app).get(f"/projects/{other.id}/writer-drafts/{draft.id}").status_code == 404


def test_grounding_requires_exact_scene_coverage(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    report = WriterGroundingValidator().validate(grounded(context, scene_coverage=[]), context)
    assert report["issues"][0]["code"] == "WRITER_SCENE_COVERAGE_INVALID"


def test_grounding_accepts_manifest_scene_ref(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    output = grounded(context, source_refs=[{"source_type": "SCENE", "source_id": writer_project[2].id}])
    assert WriterGroundingValidator().validate(output, context)["valid"]


def test_grounding_rejects_foreign_source_ref(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    output = grounded(context, source_refs=[{"source_type": "SCENE", "source_id": "foreign"}])
    assert WriterGroundingValidator().validate(output, context)["issues"][0]["code"] == "WRITER_SOURCE_REF_INVALID"


def test_grounding_rejects_pov_mismatch(writer_project, session):
    actor = writer_project[1]
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "THIRD_PERSON_LIMITED", "pov_character_id": actor.id})
    assert WriterGroundingValidator().validate(grounded(context, pov_character_id=None), context)["issues"][0]["code"] == "WRITER_POV_MISMATCH"


def test_parser_rejects_markdown_wrapper():
    with pytest.raises(ValueError, match="MODEL_OUTPUT_INVALID"):
        WriterProjectionService._parse("```json\n{}\n```")


def test_parser_rejects_extra_server_fields():
    raw = response(scene_ids=["scene"])
    value = json.loads(raw); value["status"] = "VALIDATED"
    with pytest.raises(ValueError, match="MODEL_OUTPUT_INVALID"):
        WriterProjectionService._parse(json.dumps(value))


def test_parser_rejects_empty_prose():
    with pytest.raises(ValueError, match="WRITER_OUTPUT_INVALID"):
        WriterProjectionService._parse(response(content="", scene_ids=["scene"]))


def test_prompt_labels_fact_and_subjective_authority(writer_project, session):
    context = WriterContextBuilder().build(session, WriterChapterSourceBuilder().build(session, writer_project[3].id), {"pov_mode": "OBJECTIVE"})
    system = WriterPromptBuilder().build(context)[0]["content"]
    assert "FORMAL_HISTORY" in system and "SUBJECTIVE_POV" in system and "prose renderer" in system


def test_adopt_retry_is_idempotent(writer_project, session):
    chapter = writer_project[3]; draft = render(session, chapter)
    first = WriterProjectionService().adopt(session, draft.id)
    second = WriterProjectionService().adopt(session, draft.id)
    assert first.id == second.id and second.current_writer_draft_id == draft.id


def test_writing_bible_change_marks_unadopted_draft_stale(writer_project, session):
    project, _, _, chapter = writer_project
    first = WritingBible(project_id=project.id, version=1, active=True, rules={"tone": "plain"}); session.add(first); session.flush()
    draft = render(session, chapter)
    first.active = False; session.add(WritingBible(project_id=project.id, version=2, active=True, rules={"tone": "lyrical"})); session.flush()
    with pytest.raises(WriterDomainError, match="WRITER_STYLE_SOURCE_CHANGED"):
        WriterProjectionService().adopt(session, draft.id)
    assert draft.status == WriterDraftStatus.STALE


def test_preview_contains_no_full_context_or_prose(writer_project, session):
    preview = WriterProjectionService().preview(session, writer_project[3].id, {"pov_mode": "OBJECTIVE"})
    encoded = json.dumps(preview)
    assert "formal_history" not in preview and "forbidden summary" not in encoded and "forbidden intent" not in encoded


def test_idempotency_key_alias_returns_same_draft(writer_project, session):
    chapter = writer_project[3]
    first = render(session, chapter, idempotency_key="alias")
    second = render(session, chapter, idempotency_key="alias")
    assert first.id == second.id and first.client_request_id == "alias"


def test_render_api_uses_existing_routed_provider_and_does_not_adopt(writer_project, session, monkeypatch):
    project, _, _, chapter = writer_project
    provider = FakeModelProvider(response(scene_ids=chapter.source_scene_ids))
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    monkeypatch.setattr(api, "get_model_provider", lambda *args, **kwargs: provider)
    result = TestClient(app).post(f"/projects/{project.id}/chapters/{chapter.id}/writer/render", json={"pov_mode": "OBJECTIVE", "idempotency_key": "api-render"})
    assert result.status_code == 200 and result.json()["prose"] == "The hall remains quiet."
    with sessionmaker(bind=session.bind, expire_on_commit=False)() as fresh:
        current = fresh.get(Chapter, chapter.id)
        assert current.content is None and current.current_writer_draft_id is None


def test_nested_adopt_api_is_explicit(writer_project, session, monkeypatch):
    project, _, _, chapter = writer_project; draft = render(session, chapter); session.commit()
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    result = TestClient(app).post(f"/projects/{project.id}/chapters/{chapter.id}/writer/drafts/{draft.id}/adopt", json={})
    assert result.status_code == 200 and result.json()["current_writer_draft_id"] == draft.id


def visibility_scene():
    turns = []
    resolutions = []
    for index, (visibility, actor, recipients) in enumerate((
        ("PUBLIC", "A", []), ("TARGETED", "A", ["B"]),
        ("PRIVATE", "A", []), ("COVERT", "A", []),
    ), 1):
        turn_id = f"turn-{index}"
        turns.append({"id": turn_id, "visibility": visibility, "actor_character_id": actor, "recipient_character_ids": recipients, "observable_action": visibility, "spoken_content": None, "decision": None})
        resolutions.append({"id": f"resolution-{index}", "turn_id": turn_id, "actor_character_id": actor, "recipient_character_ids": recipients, "actor_observation": f"actor-{visibility}", "public_observation": f"public-{visibility}", "objective_facts": [{"secret": visibility}]})
    return [{"scene_id": "scene", "participants": ["A", "B", "C"], "turns": turns, "resolutions": resolutions, "state_delta_items": [{"id": "delta", "target_type": "CHARACTER", "target_id": "C", "after_value": "terrified"}], "state_changes": [{"id": "event", "target_type": "WORLD_ENTITY", "target_id": "vault", "after_value": "Secret Vault"}]}]


@pytest.mark.parametrize(("mode", "pov", "expected"), [
    (WriterPOVMode.OBJECTIVE, None, ["turn-1"]),
    (WriterPOVMode.THIRD_PERSON_LIMITED, "B", ["turn-1", "turn-2"]),
    (WriterPOVMode.THIRD_PERSON_LIMITED, "C", ["turn-1"]),
    (WriterPOVMode.FIRST_PERSON, "A", ["turn-1", "turn-2", "turn-3", "turn-4"]),
])
def test_visibility_projector_observation_matrix(mode, pov, expected):
    rows = WriterVisibilityProjector().project(visibility_scene(), mode, pov)
    assert [item["id"] for item in rows[0]["turns"]] == expected


def test_public_resolution_does_not_require_recipient():
    rows = WriterVisibilityProjector().project(visibility_scene(), WriterPOVMode.THIRD_PERSON_LIMITED, "C")
    assert rows[0]["resolutions"][0]["public_observation"] == "public-PUBLIC"
    assert rows[0]["resolutions"][0]["actor_observation"] is None


def test_limited_projection_excludes_raw_world_truth_and_refs(writer_project, session):
    source = WriterChapterSourceBuilder().build(session, writer_project[3].id)
    source["scenes"][0]["state_delta_items"] = [{"id": "delta", "target_type": "CHARACTER", "target_id": "hidden", "after_value": "terrified"}]
    source["scenes"][0]["state_changes"] = [{"id": "event", "target_type": "WORLD_ENTITY", "target_id": "vault", "after_value": "Secret Vault"}]
    context = WriterContextBuilder().build(session, source, {"pov_mode": "THIRD_PERSON_LIMITED", "pov_character_id": writer_project[1].id})
    encoded = json.dumps(context, default=str)
    assert "terrified" not in encoded and "Secret Vault" not in encoded
    assert not {"delta", "event"} & {item["source_id"] for item in context["renderable_source_refs"]}


def test_writer_config_validation(writer_project):
    project = writer_project[0]
    with pytest.raises(WriterDomainError, match="INVALID_WRITER_CONFIG"):
        WriterRenderConfigResolver().resolve(project, {"min_words": 10, "max_words": 5})
    with pytest.raises(WriterDomainError, match="INVALID_WRITER_CONFIG"):
        WriterRenderConfigResolver().resolve(project, {"target_words": 0})


def test_same_rules_new_bible_version_stales_draft(writer_project, session):
    project, _, _, chapter = writer_project
    first = WritingBible(project_id=project.id, version=1, active=True, rules={"tone": "plain"}); session.add(first); session.flush()
    draft = render(session, chapter)
    first.active = False; session.add(WritingBible(project_id=project.id, version=2, active=True, rules={"tone": "plain"})); session.flush()
    with pytest.raises(WriterDomainError, match="WRITER_STYLE_SOURCE_CHANGED"):
        WriterProjectionService().adopt(session, draft.id)
    assert draft.status == WriterDraftStatus.STALE


def test_default_config_change_stales_but_explicit_override_does_not(writer_project, session):
    project, _, _, chapter = writer_project
    draft = render(session, chapter, target_words=2000)
    project.target_chapter_words = 4000; session.flush()
    WriterProjectionService().adopt(session, draft.id)
    assert draft.status == WriterDraftStatus.ADOPTED


def test_preview_and_render_prompt_fingerprint_match(writer_project, session):
    chapter = writer_project[3]
    preview = WriterProjectionService().preview(session, chapter.id, {"pov_mode": "OBJECTIVE"})
    draft = render(session, chapter)
    assert preview["prompt_fingerprint"] == draft.prompt_fingerprint


def test_adopt_title_policy(writer_project, session):
    chapter = writer_project[3]; chapter.title = "Existing"
    first = render(session, chapter); WriterProjectionService().adopt(session, first.id)
    assert chapter.title == "Existing"
    second = render(session, chapter); WriterProjectionService().adopt(session, second.id, replace_title=True)
    assert chapter.title == "Quiet"


def test_adopt_uses_candidate_title_when_missing(writer_project, session):
    chapter = writer_project[3]; chapter.title = None
    draft = render(session, chapter); WriterProjectionService().adopt(session, draft.id)
    assert chapter.title == "Quiet"


def test_adopt_failure_rolls_back_in_fresh_session(writer_project, session):
    _, _, _, chapter = writer_project
    draft = render(session, chapter); session.commit()
    service = WriterProjectionService(failure_injector=lambda stage: (_ for _ in ()).throw(RuntimeError(stage)))
    with pytest.raises(RuntimeError, match="AFTER_CHAPTER_CONTENT_BEFORE_DRAFT_FINALIZATION"):
        service.adopt(session, draft.id)
    session.rollback()
    with sessionmaker(bind=session.bind, expire_on_commit=False)() as fresh:
        saved_chapter = fresh.get(Chapter, chapter.id)
        saved_draft = fresh.get(ChapterWriterDraft, draft.id)
        assert saved_chapter.content is None and saved_chapter.current_writer_draft_id is None
        assert saved_draft.status == WriterDraftStatus.VALIDATED


def test_formal_execution_lineage_is_writer_source(writer_project, session):
    project, actor, scene, chapter = writer_project
    proposal = SceneProposal(project_id=project.id, context_fingerprint="formal", proposal_type="CONTINUE_THREAD", participants=[actor.id], scene_goal="formal", character_motivations={}, entry_state={}, expected_progress={}, allowed_reveals=[], forbidden_reveals=[], required_canon=[], possible_outcomes=[], new_entity_requests=[], risk_flags=[], director_reasoning_summary="formal", status="EXECUTED")
    session.add(proposal); session.flush()
    decision = CharacterDecision(project_id=project.id, scene_proposal_id=proposal.id, character_id=actor.id, context_fingerprint="formal", decision_type="OBSERVE", intent="observe", chosen_action="observe", motivation="observe", goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[], relationship_factors={}, uncertainties=[], refused_options=[], decision_summary="observe", status="VALID")
    session.add(decision); session.flush()
    performance = ScenePerformance(project_id=project.id, scene_proposal_id=proposal.id, take_number=1, proposal_context_fingerprint="formal", mode="HEURISTIC", status="COMPLETED", participant_order=[actor.id], active_participant_ids=[actor.id], max_turns=1, turn_count=1)
    session.add(performance); session.flush()
    turn = ScenePerformanceTurn(project_id=project.id, performance_id=performance.id, sequence=1, actor_character_id=actor.id, actor_context_fingerprint="formal", character_decision_id=decision.id, action_visibility="PUBLIC", observable_action="observe", spoken_content="I look around.", recipient_character_ids=[], requires_world_resolution=False, world_resolution_request=None, validation_result={})
    session.add(turn); session.flush()
    resolution = WorldResolution(project_id=project.id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode="HEURISTIC", world_context_fingerprint="formal", status="VALID", outcome="SUCCESS", outcome_summary="door opened", objective_facts=[], state_effects=[], actor_observation="I see the door open.", public_observation="The door opens.", recipient_character_ids=[], canon_fact_ids_used=[], world_entity_ids_used=[], resolution_basis_summary="formal", missing_information=[])
    session.add(resolution); session.flush()
    batch = StateDeltaBatch(project_id=project.id, source_type="WORLD_RESOLUTION", source_id=resolution.id, source_performance_id=performance.id, source_turn_id=turn.id, source_resolution_id=resolution.id, base_world_fingerprint="before", input_fingerprint="formal-input", status="APPLIED", derivation_version="formal", derivation_report={}, applied_scene_id=scene.id)
    session.add(batch); session.flush()
    session.add(StateDeltaItem(project_id=project.id, batch_id=batch.id, ordinal=1, target_type="WORLD_ENTITY", target_id="door", domain="WORLD_ENTITY_PROFILE", operation="SET", path="/profile/opened", before_value=False, after_value=True, causal_reason="formal", source_turn_id=turn.id, source_resolution_id=resolution.id, evidence={}, semantic_fingerprint="formal-delta-fp"))
    session.add(SceneExecutionBinding(project_id=project.id, scene_id=scene.id, performance_id=performance.id, active=True))
    session.add(SceneStateCheckpoint(project_id=project.id, scene_id=scene.id, sequence=scene.sequence, pre_snapshot_id="formal-pre", post_snapshot_id="formal-post", current_scene_id=scene.id, capture_protocol_version=3, version=1, active=True, checkpoint_fingerprint="formal-checkpoint"))
    session.add(TimelineEvent(project_id=project.id, event_type="STATE_CHANGE", source_type="STATE_DELTA_ITEM", source_id="formal-delta", source_key="formal-event", scene_id=scene.id, sequence=scene.sequence, ordinal=1, origin="NORMAL_COMMIT", active=True, target_type="WORLD_ENTITY", target_id="door", path="/profile/opened", before_value=False, after_value=True, structured_payload={}, event_fingerprint="formal-event-fp"))
    session.flush()
    source = WriterChapterSourceBuilder().build(session, chapter.id, run_audit=False)
    row = source["scenes"][0]
    assert row["legacy_source"] is False and row["binding_id"] and row["performance_id"]
    assert row["turns"][0]["spoken_content"] == "I look around."
    assert row["checkpoint_id"] and row["state_changes"][0]["path"] == "/profile/opened"
