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
    AntiAIBible, Chapter, ChapterQualityAssessment, ChapterQualityFinding,
    ChapterWriterDraft, Character, ExecutionTrace, Project, Scene,
    WriterDraftOrigin, WriterDraftStatus,
)
from app.narrative_structure import NarrativeStructureService
from app.quality import (
    AntiAIBibleResolver, AntiAIStyleRuleEngine, CriticOutputValidator,
    NarrativeRepetitionDetector, QualityAssessmentAudit, QualityContextBuilder,
    QualityDomainError, QualityGateConfigResolver, QualityGateService,
    QualityRepairService, assessment_payload, finding_fingerprint,
)
from app.writer import WriterProjectionService


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def writer_response(chapter, prose="The door opens.", **changes):
    value = {"chapter_title": "Door", "prose": prose, "scene_coverage": chapter.source_scene_ids, "source_refs": [], "pov_character_id": None}
    value.update(changes)
    return json.dumps(value)


def critic_response(decision="PASS", overall=95, findings=None, **score_changes):
    scores = {"factual_grounding": 95, "pov_compliance": 95, "reveal_safety": 95, "style_naturalness": 95, "repetition": 95, "pacing": 95, "voice_consistency": 95, "overall": overall}
    scores.update(score_changes)
    return json.dumps({"decision": decision, "scores": scores, "findings": findings or []})


@pytest.fixture()
def quality_project(session):
    project = Project(name="Quality", min_chapter_words=None, max_chapter_words=None, autonomy_settings={"quality_gate": {"require_critic": True}})
    session.add(project); session.flush()
    actor = Character(project_id=project.id, name="Lin")
    session.add(actor); session.flush()
    scene = Scene(project_id=project.id, sequence=1, world_time=datetime(2026, 1, 1), location="hall", participants=[actor.id], facts=[], result={}, summary="summary", intent="intent", story_threads=[], status="OCCURRED", history_status="ACTIVE")
    session.add(scene); session.flush(); NarrativeStructureService().sync(session, project.id); session.commit()
    chapter = session.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True)))
    draft = WriterProjectionService().render(session, chapter.id, {"pov_mode": "OBJECTIVE"}, provider=FakeModelProvider(writer_response(chapter)), model="writer-test")
    WriterProjectionService().adopt(session, draft.id); session.commit()
    return project, actor, scene, chapter, draft


def assess_pass(session, chapter, key=None):
    request = {"client_request_id": key} if key else {}
    return QualityGateService().assess(session, chapter.id, request, provider=FakeModelProvider(critic_response()), model="critic-test")


def test_default_anti_ai_bible_is_read_only(quality_project, session):
    before = session.scalar(select(func.count(AntiAIBible.id)))
    value = AntiAIBibleResolver().resolve(session, quality_project[0].id)
    assert value["id"] is None and value["rules"]["disabled_expressions"] == []
    assert session.scalar(select(func.count(AntiAIBible.id))) == before


def test_anti_ai_bible_fingerprint_includes_version(quality_project, session):
    project = quality_project[0]
    first = AntiAIBible(project_id=project.id, version=1, active=True)
    session.add(first); session.flush(); fp1 = AntiAIBibleResolver().resolve(session, project.id)["fingerprint"]
    first.active = False; second = AntiAIBible(project_id=project.id, version=2, active=True)
    session.add(second); session.flush(); fp2 = AntiAIBibleResolver().resolve(session, project.id)["fingerprint"]
    assert fp1 != fp2


def test_multiple_active_anti_ai_bibles_fail_closed(quality_project, session):
    project = quality_project[0]
    session.add_all([AntiAIBible(project_id=project.id, version=1, active=True), AntiAIBible(project_id=project.id, version=2, active=True)]); session.flush()
    with pytest.raises(QualityDomainError, match="ANTI_AI_BIBLE_AMBIGUOUS"):
        AntiAIBibleResolver().resolve(session, project.id)


@pytest.mark.parametrize("limits", [{"unknown": 1}, {"expressions": []}, {"punctuation": {"!": -1}}, {"repeated_exact_sentence": 0}])
def test_invalid_frequency_schema_is_rejected(limits):
    rules = {**AntiAIBibleResolver.DEFAULT, "frequency_limits": limits}
    with pytest.raises(QualityDomainError, match="ANTI_AI_BIBLE_INVALID"):
        AntiAIBibleResolver.validate(rules)


def test_disabled_expression_is_blocking():
    rules = {**AntiAIBibleResolver.DEFAULT, "disabled_expressions": ["不由得"]}
    report = AntiAIStyleRuleEngine().evaluate("他不由得皱眉。", rules)
    finding = report["findings"][0]
    assert finding["severity"] == "BLOCKING" and finding["rule_code"] == "ANTI_AI_DISABLED_EXPRESSION"
    assert "他不由得皱眉。"[finding["start_offset"]:finding["end_offset"]] == finding["excerpt"]


def test_warning_expression_is_nonblocking():
    rules = {**AntiAIBibleResolver.DEFAULT, "warning_expressions": ["与此同时"]}
    finding = AntiAIStyleRuleEngine().evaluate("与此同时，门开了。", rules)["findings"][0]
    assert finding["severity"] == "MINOR"


def test_expression_frequency_limit_is_deterministic():
    rules = {**AntiAIBibleResolver.DEFAULT, "frequency_limits": {"expressions": {"于是": 1}}}
    report = AntiAIStyleRuleEngine().evaluate("于是他走。于是她停。", rules)
    assert [item["rule_code"] for item in report["findings"]] == ["ANTI_AI_EXPRESSION_FREQUENCY"]


def test_punctuation_frequency_limit_is_deterministic():
    rules = {**AntiAIBibleResolver.DEFAULT, "frequency_limits": {"punctuation": {"!": 1}}}
    report = AntiAIStyleRuleEngine().evaluate("Stop!!", rules)
    assert report["findings"][0]["rule_code"] == "ANTI_AI_PUNCTUATION_FREQUENCY"


def test_unicode_normalization_detects_fullwidth_expression():
    rules = {**AntiAIBibleResolver.DEFAULT, "disabled_expressions": ["ABC"]}
    report = AntiAIStyleRuleEngine().evaluate("ＡＢＣ", rules)
    assert report["findings"][0]["excerpt"] == "ＡＢＣ"


def test_whitespace_normalization_is_stable():
    rules = {**AntiAIBibleResolver.DEFAULT, "disabled_expressions": ["he said"]}
    assert AntiAIStyleRuleEngine().evaluate("he   said", rules)["metrics"]["disabled_hit_count"] == 1


def test_repeated_exact_sentence_is_detected():
    findings, metrics = NarrativeRepetitionDetector().detect("He waits. He waits. He waits.", {"repeated_exact_sentence": 3})
    assert metrics["repeated_exact_sentences"] == 1 and any(item["rule_code"] == "REPEATED_EXACT_SENTENCE" for item in findings)


def test_repeated_paragraph_opening_is_detected():
    prose = "他看着远处。\n他看着门口。\n他看着窗外。"
    findings, _ = NarrativeRepetitionDetector().detect(prose, {"repeated_paragraph_opening": 3})
    assert any(item["rule_code"] == "REPEATED_PARAGRAPH_OPENING" for item in findings)


def test_normal_name_repetition_does_not_block():
    report = AntiAIStyleRuleEngine().evaluate("Lin came. Lin spoke. Lin left.", AntiAIBibleResolver.DEFAULT)
    assert not any(item["severity"] == "BLOCKING" for item in report["findings"])


def test_prose_statistics_are_reported():
    metrics = AntiAIStyleRuleEngine().evaluate("“Hi.”\nNext.", AntiAIBibleResolver.DEFAULT)["metrics"]
    assert metrics["char_count"] > 0 and metrics["word_count"] == 2 and metrics["paragraph_count"] == 2 and metrics["sentence_count"] == 2


def test_finding_fingerprint_ignores_random_identity():
    base = {"source": "DETERMINISTIC", "category": "FORMAT", "severity": "MINOR", "rule_code": "X", "message": "m", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": [], "metadata": {}}
    assert finding_fingerprint(base | {"id": "one"}) == finding_fingerprint(base | {"id": "two"})


def test_quality_config_uses_project_defaults(quality_project):
    config = QualityGateConfigResolver().resolve(quality_project[0], {})
    assert config.require_critic is True and config.min_overall_score == 70


def test_quality_config_request_overrides_project(quality_project):
    config = QualityGateConfigResolver().resolve(quality_project[0], {"config": {"require_critic": False, "min_overall_score": 90}})
    assert config.require_critic is False and config.min_overall_score == 90


@pytest.mark.parametrize("config", [{"min_overall_score": 101}, {"max_repair_attempts": 4}, {"unknown": True}])
def test_invalid_quality_config_is_rejected(quality_project, config):
    with pytest.raises(QualityDomainError, match="INVALID_QUALITY_GATE_CONFIG"):
        QualityGateConfigResolver().resolve(quality_project[0], {"config": config})


def test_critic_parser_accepts_strict_payload():
    value = CriticOutputValidator().parse(critic_response(), "prose", [])
    assert value["decision"] == "PASS" and value["scores"]["overall"] == 95


@pytest.mark.parametrize("content", ["not json", "```json\n{}\n```", json.dumps({"decision": "PASS"}), critic_response(overall=101)])
def test_malformed_critic_is_rejected(content):
    with pytest.raises(QualityDomainError, match="MODEL_OUTPUT_INVALID"):
        CriticOutputValidator().parse(content, "prose", [])


def test_critic_unknown_category_is_rejected():
    finding = {"category": "AI_PROBABILITY", "severity": "MINOR", "message": "bad", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": []}
    with pytest.raises(QualityDomainError, match="CRITIC_FINDING_CATEGORY_INVALID"):
        CriticOutputValidator().parse(critic_response(findings=[finding]), "prose", [])


def test_critic_invalid_span_is_rejected():
    finding = {"category": "CLARITY", "severity": "MINOR", "message": "bad", "start_offset": 0, "end_offset": 4, "excerpt": "wrong", "source_refs": []}
    with pytest.raises(QualityDomainError, match="CRITIC_FINDING_SPAN_INVALID"):
        CriticOutputValidator().parse(critic_response(findings=[finding]), "text", [])


def test_critic_hidden_ref_is_rejected():
    finding = {"category": "POV_COMPLIANCE", "severity": "BLOCKING", "message": "leak", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": [{"source_type": "TURN", "source_id": "hidden"}]}
    with pytest.raises(QualityDomainError, match="CRITIC_SOURCE_REF_INVALID"):
        CriticOutputValidator().parse(critic_response(findings=[finding]), "text", [])


def test_critic_visible_ref_is_accepted():
    finding = {"category": "CONTINUITY", "severity": "INFO", "message": "note", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": [{"source_type": "SCENE", "source_id": "s"}]}
    value = CriticOutputValidator().parse(critic_response(findings=[finding]), "text", [{"source_type": "SCENE", "source_id": "s"}])
    assert value["findings"][0]["source_refs"][0]["source_id"] == "s"


def test_quality_context_uses_current_adopted_draft(quality_project, session):
    context = QualityContextBuilder().build(session, quality_project[3].id, {}, critic_provider="fake", critic_model="critic")
    assert context["writer_draft"].id == quality_project[4].id and context["prose"] == quality_project[3].content


def test_quality_context_contains_safe_writer_context_only(quality_project, session):
    context = QualityContextBuilder().build(session, quality_project[3].id, {}, critic_provider="fake", critic_model="critic")
    encoded = json.dumps(context["writer_safe_context"], default=str)
    assert "before_value" not in encoded and "after_value" not in encoded and "raw prompt" not in encoded


def test_quality_preview_is_read_only(quality_project, session):
    counts = tuple(session.scalar(select(func.count(model.id))) for model in (ChapterQualityAssessment, ChapterQualityFinding, ChapterWriterDraft))
    result = QualityGateService().preview(session, quality_project[3].id)
    assert result["deterministic_report"]["metrics"]["char_count"] > 0
    assert counts == tuple(session.scalar(select(func.count(model.id))) for model in (ChapterQualityAssessment, ChapterQualityFinding, ChapterWriterDraft))


def test_critic_pass_creates_pass_assessment(quality_project, session):
    assessment = assess_pass(session, quality_project[3])
    assert assessment.status.value == "PASS" and assessment.active and assessment.overall_score == 95


def test_assessment_does_not_auto_approve(quality_project, session):
    assessment = assess_pass(session, quality_project[3])
    assert assessment.status.value == "PASS" and quality_project[3].status == "DRAFT" and quality_project[3].current_quality_assessment_id is None


def test_assessment_idempotency_skips_second_critic(quality_project, session):
    provider = FakeModelProvider(critic_response())
    first = QualityGateService().assess(session, quality_project[3].id, {"client_request_id": "same"}, provider=provider, model="critic")
    second = QualityGateService().assess(session, quality_project[3].id, {"client_request_id": "same"}, provider=provider, model="critic")
    assert first.id == second.id and provider.calls == 1


def test_assessment_request_mismatch_is_rejected(quality_project, session):
    provider = FakeModelProvider(critic_response())
    QualityGateService().assess(session, quality_project[3].id, {"client_request_id": "same"}, provider=provider, model="critic")
    with pytest.raises(QualityDomainError, match="QUALITY_REQUEST_MISMATCH"):
        QualityGateService().assess(session, quality_project[3].id, {"client_request_id": "same", "config": {"min_overall_score": 99}}, provider=provider, model="critic")


def test_current_context_fast_path_reuses_assessment(quality_project, session):
    first = assess_pass(session, quality_project[3])
    provider = FakeModelProvider(critic_response())
    second = QualityGateService().assess(session, quality_project[3].id, {}, provider=provider, model="critic-test")
    assert first.id == second.id and provider.calls == 0


def test_deterministic_block_dominates_critic_pass(quality_project, session):
    project, _, _, chapter, draft = quality_project
    bible = AntiAIBible(project_id=project.id, version=1, active=True, disabled_expressions=["door"])
    session.add(bible); session.flush()
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response()), model="critic")
    assert assessment.status.value == "REPAIR_REQUIRED" and "BLOCKING_FINDINGS" in assessment.decision_reason_codes


def test_low_critic_score_requires_repair(quality_project, session):
    assessment = QualityGateService().assess(session, quality_project[3].id, {}, provider=FakeModelProvider(critic_response(overall=20)), model="critic")
    assert assessment.status.value == "REPAIR_REQUIRED" and "OVERALL_SCORE_BELOW_MINIMUM" in assessment.decision_reason_codes


@pytest.mark.parametrize("category", ["FACTUAL_GROUNDING", "POV_COMPLIANCE", "REVEAL_SAFETY"])
def test_blocking_grounding_findings_require_repair(quality_project, session, category):
    finding = {"category": category, "severity": "BLOCKING", "message": "unsupported prose", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": []}
    assessment = QualityGateService().assess(session, quality_project[3].id, {}, provider=FakeModelProvider(critic_response(decision="REPAIR_REQUIRED", findings=[finding])), model="critic")
    assert assessment.status.value == "REPAIR_REQUIRED"


def test_provider_failure_persists_failed_assessment(quality_project, session):
    provider = FakeModelProvider(error=ModelProviderError(MODEL_TIMEOUT))
    assessment = QualityGateService().assess(session, quality_project[3].id, {}, provider=provider, model="critic")
    assert assessment.status.value == "FAILED" and assessment.active is False and assessment.decision_reason_codes == [MODEL_TIMEOUT]
    assert quality_project[3].status == "DRAFT"


def test_trace_does_not_store_raw_prose(quality_project, session):
    assess_pass(session, quality_project[3])
    traces = session.scalars(select(ExecutionTrace).where(ExecutionTrace.project_id == quality_project[0].id, ExecutionTrace.stage == "CRITIC")).all()
    assert traces and quality_project[3].content not in json.dumps([item.validation_report for item in traces])


def test_findings_have_contiguous_ordinals(quality_project, session):
    finding = {"category": "CLARITY", "severity": "MINOR", "message": "note", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": []}
    assessment = QualityGateService().assess(session, quality_project[3].id, {}, provider=FakeModelProvider(critic_response(findings=[finding])), model="critic")
    rows = session.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id).order_by(ChapterQualityFinding.ordinal)).all()
    assert [item.ordinal for item in rows] == list(range(1, len(rows) + 1))


def test_quality_assessment_audit_passes(quality_project, session):
    assessment = assess_pass(session, quality_project[3])
    assert QualityAssessmentAudit().audit(session, assessment.id)["valid"]


def test_quality_finding_tamper_fails_audit(quality_project, session):
    finding = {"category": "CLARITY", "severity": "MINOR", "message": "note", "start_offset": None, "end_offset": None, "excerpt": None, "source_refs": []}
    assessment = QualityGateService().assess(session, quality_project[3].id, {}, provider=FakeModelProvider(critic_response(findings=[finding])), model="critic")
    row = session.scalar(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id)); row.message = "tampered"
    with pytest.raises(QualityDomainError, match="QUALITY_FINDING_INTEGRITY_INVALID"):
        QualityAssessmentAudit().audit(session, assessment.id)


def test_explicit_approval_sets_quality_approved(quality_project, session):
    assessment = assess_pass(session, quality_project[3])
    chapter = QualityGateService().approve(session, assessment.id)
    assert chapter.status == "QUALITY_APPROVED" and chapter.current_quality_assessment_id == assessment.id and chapter.quality_status == "PASS"


def test_nonpass_assessment_cannot_be_approved(quality_project, session):
    assessment = QualityGateService().assess(session, quality_project[3].id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    with pytest.raises(QualityDomainError, match="QUALITY_ASSESSMENT_NOT_PASS"):
        QualityGateService().approve(session, assessment.id)


def test_content_tamper_blocks_approval(quality_project, session):
    assessment = assess_pass(session, quality_project[3]); quality_project[3].content = "tampered"
    with pytest.raises(QualityDomainError):
        QualityGateService().approve(session, assessment.id)


def test_stale_approval_marks_assessment_stale(quality_project, session):
    assessment = assess_pass(session, quality_project[3]); quality_project[3].content = "tampered"
    with pytest.raises(QualityDomainError, match="QUALITY_SOURCE_CHANGED"):
        QualityGateService().approve(session, assessment.id)
    assert assessment.status.value == "STALE" and not assessment.active and assessment.stale_at is not None


def test_new_writer_adopt_invalidates_quality(quality_project, session):
    project, _, _, chapter, _ = quality_project
    assessment = assess_pass(session, chapter); QualityGateService().approve(session, assessment.id)
    draft = WriterProjectionService().render(session, chapter.id, {"pov_mode": "OBJECTIVE", "client_request_id": "new"}, provider=FakeModelProvider(writer_response(chapter, "The door closes.")), model="writer")
    WriterProjectionService().adopt(session, draft.id)
    assert chapter.status == "DRAFT" and chapter.quality_status == "STALE" and chapter.current_quality_assessment_id is None
    assert assessment.status.value == "STALE" and not assessment.active


def test_repair_is_candidate_only(quality_project, session):
    project, _, _, chapter, original = quality_project
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    repair = FakeModelProvider(writer_response(chapter, "The door opens quietly."))
    draft, child = QualityRepairService().repair(session, assessment.id, {}, repair_provider=repair, repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    assert draft.origin.value == "QUALITY_REPAIR" and draft.status.value == "VALIDATED" and child.status.value == "PASS"
    assert chapter.current_writer_draft_id == original.id and chapter.content == original.content


def test_repair_provenance_is_auditable(quality_project, session):
    chapter = quality_project[3]
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    draft, _ = QualityRepairService().repair(session, assessment.id, {}, repair_provider=FakeModelProvider(writer_response(chapter, "Fixed.")), repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    assert draft.source_quality_assessment_id == assessment.id and draft.parent_draft_id == quality_project[4].id


def test_repair_foreign_source_ref_is_rejected(quality_project, session):
    chapter = quality_project[3]
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    output = writer_response(chapter, "Fixed.", source_refs=[{"source_type": "TURN", "source_id": "foreign"}])
    draft, child = QualityRepairService().repair(session, assessment.id, {}, repair_provider=FakeModelProvider(output), repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    assert draft.status.value == "REJECTED" and child is None


def test_repair_limit_is_enforced(quality_project, session):
    chapter = quality_project[3]
    assessment = QualityGateService().assess(session, chapter.id, {"config": {"max_repair_attempts": 1}}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    QualityRepairService().repair(session, assessment.id, {}, repair_provider=FakeModelProvider(writer_response(chapter, "Fixed.")), repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    with pytest.raises(QualityDomainError, match="QUALITY_REPAIR_LIMIT_REACHED"):
        QualityRepairService().repair(session, assessment.id, {}, repair_provider=FakeModelProvider(writer_response(chapter, "Fixed twice.")), repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")


def test_repair_request_is_idempotent(quality_project, session):
    chapter = quality_project[3]
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    repair_provider = FakeModelProvider(writer_response(chapter, "Fixed once."))
    first, first_child = QualityRepairService().repair(session, assessment.id, {"client_request_id": "repair"}, repair_provider=repair_provider, repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    second, second_child = QualityRepairService().repair(session, assessment.id, {"client_request_id": "repair"}, repair_provider=repair_provider, repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    assert first.id == second.id and first_child.id == second_child.id and repair_provider.calls == 1


def test_repair_adopt_requires_pass_assessment(quality_project, session):
    chapter = quality_project[3]
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    draft, _ = QualityRepairService().repair(session, assessment.id, {}, repair_provider=FakeModelProvider(writer_response(chapter, "Still weak.")), repair_model="repair", critic_provider=FakeModelProvider(critic_response(overall=1)), critic_model="critic")
    with pytest.raises(QualityDomainError, match="QUALITY_REPAIR_ASSESSMENT_NOT_PASS"):
        QualityRepairService().adopt(session, draft.id)


def test_repair_adopt_is_explicit_and_atomic(quality_project, session):
    chapter, original = quality_project[3], quality_project[4]
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    draft, child = QualityRepairService().repair(session, assessment.id, {}, repair_provider=FakeModelProvider(writer_response(chapter, "Fixed.")), repair_model="repair", critic_provider=FakeModelProvider(critic_response()), critic_model="critic")
    result = QualityRepairService().adopt(session, draft.id)
    assert result.status == "QUALITY_APPROVED" and result.current_writer_draft_id == draft.id and result.current_quality_assessment_id == child.id
    assert draft.status.value == "ADOPTED" and original.status.value == "SUPERSEDED"


def test_quality_api_preview_is_safe_and_read_only(quality_project, session, monkeypatch):
    project, _, _, chapter, _ = quality_project
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    result = TestClient(app).post(f"/projects/{project.id}/chapters/{chapter.id}/quality/preview", json={})
    assert result.status_code == 200 and "writer_safe_context" not in result.json()


def test_quality_api_assess_and_detail(quality_project, session, monkeypatch):
    project, _, _, chapter, _ = quality_project
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    monkeypatch.setattr(api, "_quality_provider", lambda *args: (FakeModelProvider(critic_response()), "critic", None))
    client = TestClient(app)
    assessed = client.post(f"/projects/{project.id}/chapters/{chapter.id}/quality/assess", json={})
    assert assessed.status_code == 200
    detail = client.get(f"/projects/{project.id}/chapters/{chapter.id}/quality/assessments/{assessed.json()['id']}")
    assert detail.status_code == 200 and "writer_safe_context" not in detail.text
    assert "raw prompt" not in detail.text and "messages" not in detail.text and chapter.content not in detail.text


def test_quality_api_cross_project_is_404(quality_project, session, monkeypatch):
    other = Project(name="Other"); session.add(other); session.commit()
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    result = TestClient(app).get(f"/projects/{other.id}/chapters/{quality_project[3].id}/quality")
    assert result.status_code == 404


def test_assessment_unique_active_constraint_sqlite(quality_project, session):
    chapter, draft = quality_project[3], quality_project[4]
    base = dict(project_id=chapter.project_id, chapter_id=chapter.id, writer_draft_id=draft.id, status="RUNNING", active=True, request_fingerprint="r", content_fingerprint=draft.content_fingerprint, writer_context_fingerprint=draft.writer_context_fingerprint, chapter_source_fingerprint=draft.chapter_source_fingerprint, anti_ai_bible_fingerprint="a", writing_bible_fingerprint=draft.writing_bible_fingerprint, quality_config={}, quality_config_fingerprint="c", quality_context_fingerprint="q", deterministic_report={}, critic_report={}, decision_reason_codes=[])
    session.add(ChapterQualityAssessment(version=1, **base)); session.flush(); session.add(ChapterQualityAssessment(version=2, **base))
    with pytest.raises(IntegrityError):
        session.flush()


def test_writer_draft_origin_defaults_to_writer(quality_project):
    assert quality_project[4].origin.value == WriterDraftOrigin.WRITER.value and quality_project[4].source_quality_assessment_id is None


def test_no_ai_probability_field_exists_in_report():
    encoded = json.dumps(AntiAIStyleRuleEngine().evaluate("plain prose", AntiAIBibleResolver.DEFAULT))
    assert "probability" not in encoded.lower()


def test_warning_finding_does_not_prevent_pass(quality_project, session):
    project, _, _, chapter, _ = quality_project
    session.add(AntiAIBible(project_id=project.id, version=1, active=True, warning_expressions=["door"])); session.flush()
    assessment = QualityGateService().assess(session, chapter.id, {}, provider=FakeModelProvider(critic_response()), model="critic")
    assert assessment.status.value == "PASS"
    assert session.scalar(select(func.count(ChapterQualityFinding.id)).where(ChapterQualityFinding.assessment_id == assessment.id)) == 1


def test_auto_repair_config_never_auto_creates_draft(quality_project, session):
    chapter = quality_project[3]
    before = session.scalar(select(func.count(ChapterWriterDraft.id)))
    assessment = QualityGateService().assess(session, chapter.id, {"config": {"auto_repair_enabled": True}}, provider=FakeModelProvider(critic_response(overall=1)), model="critic")
    assert assessment.status.value == "REPAIR_REQUIRED"
    assert session.scalar(select(func.count(ChapterWriterDraft.id))) == before


def test_quality_context_fingerprint_is_timestamp_independent(quality_project, session):
    first = QualityContextBuilder().build(session, quality_project[3].id, {}, critic_provider="fake", critic_model="critic")
    second = QualityContextBuilder().build(session, quality_project[3].id, {}, critic_provider="fake", critic_model="critic")
    assert first["quality_context_fingerprint"] == second["quality_context_fingerprint"]


def test_assessment_payload_never_contains_prose_or_safe_context(quality_project, session):
    assessment = assess_pass(session, quality_project[3])
    encoded = json.dumps(assessment_payload(session, assessment, include_findings=True))
    assert quality_project[3].content not in encoded and "writer_safe_context" not in encoded and "messages" not in encoded
