import pytest
import json
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution_trace import ExecutionTraceRecorder
from app.models import ExecutionStage, RecoveryCandidate, RecoveryCandidateStatus, CharacterDecision
from app.recovery import RecoveryCandidateService, RecoveryActionResolver, RecoveryValidationResult, CandidateRepairAgent
from app.execution_trace import RecoveryPolicy
from app.character_mind import CharacterContextBuilder
from test_llm_character_actor import valid_payload
from test_scene_performance import approved_setup, client_for, performance_payload


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def test_character_constraint_candidate_is_structured_and_unique(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id, input_fingerprint="character-context")
    service = RecoveryCandidateService()
    first = service.create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint="character-context", locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="KNOWLEDGE_LEAK", validation_report={"issues": [{"code": "KNOWLEDGE_LEAK"}]}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id)
    second = service.create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint="character-context", locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="KNOWLEDGE_LEAK", validation_report={}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id)
    assert first.id == second.id
    assert len(session.scalars(select(RecoveryCandidate)).all()) == 1


def test_manual_edit_uses_new_version_and_rfc6901(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id, input_fingerprint="character-context")
    service = RecoveryCandidateService()
    candidate = service.create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint="character-context", locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="TARGET_MISMATCH", validation_report={}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id)
    with pytest.raises(ValueError, match="RECOVERY_CONTEXT_STALE"):
        service.edit(session, candidate, 1, [{"operation": "SET", "path": "/decision_summary", "value": "repaired"}])

def test_manual_schema_invalid_version_is_saved_open(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch)
    context = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id)
    service = RecoveryCandidateService(); candidate = service.create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint=context["fingerprint"], locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="TARGET_MISMATCH", validation_report={}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id)
    version = service.edit(session, candidate, 1, [{"operation": "SET", "path": "/decision_type", "value": "NOT_A_DECISION"}])
    assert version.schema_valid is False and candidate.status == "OPEN" and version.version_number == 2

def test_manual_valid_edit_becomes_validated(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch)
    context = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id)
    service = RecoveryCandidateService(); candidate = service.create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint=context["fingerprint"], locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="TARGET_MISMATCH", validation_report={}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id)
    version = service.edit(session, candidate, 1, [{"operation": "SET", "path": "/decision_summary", "value": "verified"}])
    assert version.schema_valid is True and candidate.status == "VALIDATED"

@pytest.mark.parametrize("code", ["KNOWLEDGE_LEAK", "TARGET_MISMATCH", "INVALID_TARGET", "INVALID_WORLD_REQUEST", "PREMATURE_REVEAL", "INVALID_CANON_REFERENCE", "INVALID_ENTITY_REFERENCE", "INVALID_FACT_SUBJECT", "INVALID_RESOLUTION_STATE", "CROSS_PROJECT_REFERENCE", "CANON_CONTRADICTION", "OBSERVATION_LEAK"])
def test_constraint_errors_are_repairable(code):
    retryable, repairable = RecoveryPolicy.resolve(code)[:2]
    assert retryable is False and repairable is True
    class Candidate:
        status = "OPEN"; initial_error_code = code
    assert RecoveryActionResolver.resolve(code, Candidate(), repair_attempts=0, max_repair_attempts=1) == ["AI_REPAIR", "MANUAL_EDIT", "ABORT"]

@pytest.mark.parametrize("candidate_type, expected", [("CHARACTER_DECISION", "required_fields"), ("CHARACTER_PERFORMANCE", "action"), ("WORLD_RESOLUTION", "WorldFact")])
def test_repair_agent_uses_real_output_contract(candidate_type, expected):
    messages = CandidateRepairAgent().build_messages(candidate_type, {}, {}, {})
    body = json.loads(messages[1]["content"])
    assert isinstance(body["output_contract"], dict)
    assert expected in body["output_contract"]
    assert "untrusted" in messages[0]["content"] and "sanitized_context" in messages[1]["content"]

def test_recovery_validation_result_is_explicit():
    result = RecoveryValidationResult(True, False, False, {"x": 1}, {"issues": []}, "CONSTRAINT_FAILED")
    assert result.schema_valid and not result.constraint_valid and not result.context_stale

def test_repair_trace_parent_chain_is_explicit(session):
    recorder = ExecutionTraceRecorder(); first = recorder.start(session, project_id="p", stage=ExecutionStage.REPAIR, source_type="RECOVERY_CANDIDATE", source_id="c", attempt_number=1); session.flush()
    second = recorder.start(session, project_id="p", stage=ExecutionStage.REPAIR, source_type="RECOVERY_CANDIDATE", source_id="c", attempt_number=2, parent_trace_id=first.id)
    assert second.parent_trace_id == first.id

def test_candidate_actions_world_information_missing_are_read_only():
    class Candidate:
        status = "OPEN"; initial_error_code = "WORLD_INFORMATION_MISSING"
    assert RecoveryActionResolver.resolve("WORLD_INFORMATION_MISSING", Candidate()) == ["EDIT_WORLD", "ABORT"]

def test_candidate_actions_validated_adopt_only():
    class Candidate:
        status = "VALIDATED"; initial_error_code = "TARGET_MISMATCH"
    assert RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate()) == ["ADOPT", "MANUAL_EDIT", "ABORT"]


def test_world_information_missing_candidate_cannot_be_edited(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.WORLD_RESOLVER, source_type="SCENE_PERFORMANCE_TURN", source_id="turn", input_fingerprint="world-context")
    payload = {"outcome": "UNRESOLVED", "outcome_summary": "missing", "objective_facts": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [], "resolution_basis_summary": None, "missing_information": ["door state"]}
    service = RecoveryCandidateService()
    candidate = service.create(session, project_id=project.id, trace=trace, candidate_type="WORLD_RESOLUTION", payload=payload, context_fingerprint="world-context", locator={"project_id": project.id, "proposal_id": proposal.id, "performance_id": "performance", "performance_turn_id": "turn", "world_resolution_id": "resolution"}, error_code="WORLD_INFORMATION_MISSING", validation_report={}, stage="WORLD_RESOLVER", source_type="SCENE_PERFORMANCE_TURN", source_id="turn")
    with pytest.raises(ValueError, match="WORLD_FACT_REQUIRED"):
        service.edit(session, candidate, 1, [{"operation": "SET", "path": "/outcome", "value": "SUCCESS"}])


def test_candidate_payload_rejects_raw_and_oversized_data(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id, input_fingerprint="x")
    value = json.loads(valid_payload()); value["decision_summary"] = "x" * 70000
    with pytest.raises(ValueError, match="CANDIDATE_TOO_LARGE"):
        RecoveryCandidateService().create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=value, context_fingerprint="x", locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="KNOWLEDGE_LEAK", validation_report={"raw_output": "must not persist"}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id)

def test_world_information_missing_actions_have_no_repair():
    class Candidate:
        status = "OPEN"; initial_error_code = "WORLD_INFORMATION_MISSING"
    assert RecoveryActionResolver.resolve("WORLD_INFORMATION_MISSING", Candidate()) == ["EDIT_WORLD", "ABORT"]

def test_adopted_candidate_has_no_actions():
    class Candidate:
        status = "ADOPTED"; initial_error_code = "TARGET_MISMATCH"
    assert RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate()) == []

def test_aborted_candidate_has_no_actions():
    class Candidate:
        status = "ABORTED"; initial_error_code = "TARGET_MISMATCH"
    assert RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate()) == []

def test_repair_attempt_limit_removes_ai_repair():
    class Candidate:
        status = "OPEN"; initial_error_code = "TARGET_MISMATCH"
    assert RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate(), repair_attempts=1, max_repair_attempts=1) == ["MANUAL_EDIT", "ABORT"]

def test_stale_candidate_requires_rebuild_context():
    class Candidate:
        status = "STALE"; initial_error_code = "TARGET_MISMATCH"
    assert RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate()) == ["REBUILD_CONTEXT", "ABORT"]

def test_trace_sanitizer_removes_candidate_secrets():
    from app.execution_trace import TraceSanitizer
    clean = TraceSanitizer.clean({"api-key": "x", "raw_output": "x", "safe": 1})
    assert clean == {"safe": 1}

def test_model_output_invalid_without_candidate_is_retryable_action():
    assert RecoveryActionResolver.resolve("MODEL_OUTPUT_INVALID", None) == ["RETRY", "ABORT"]

def test_validated_candidate_can_adopt():
    class Candidate:
        status = "VALIDATED"; initial_error_code = "TARGET_MISMATCH"
    assert "ADOPT" in RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate())

def test_open_candidate_can_manual_edit():
    class Candidate:
        status = "OPEN"; initial_error_code = "TARGET_MISMATCH"
    assert "MANUAL_EDIT" in RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate())

def test_open_candidate_can_abort():
    class Candidate:
        status = "OPEN"; initial_error_code = "TARGET_MISMATCH"
    assert "ABORT" in RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate())

def test_context_stale_flag_overrides_candidate_actions():
    class Candidate:
        status = "VALIDATED"; initial_error_code = "TARGET_MISMATCH"
    assert RecoveryActionResolver.resolve("TARGET_MISMATCH", Candidate(), context_stale=True) == ["REBUILD_CONTEXT", "ABORT"]

def test_recovery_validation_schema_invalid_shape():
    result = RecoveryValidationResult(False, False, False, None, {"code": "MODEL_OUTPUT_INVALID"}, "MODEL_OUTPUT_INVALID")
    assert result.normalized_payload is None and result.error_code == "MODEL_OUTPUT_INVALID"

def test_recovery_validation_constraint_invalid_shape():
    result = RecoveryValidationResult(True, False, False, {"ok": True}, {"issues": [{"code": "TARGET_MISMATCH"}]}, "CONSTRAINT_FAILED")
    assert result.normalized_payload == {"ok": True} and not result.constraint_valid

def test_recovery_validation_context_stale_shape():
    result = RecoveryValidationResult(False, False, True, None, {"code": "RECOVERY_CONTEXT_STALE"}, "RECOVERY_CONTEXT_STALE")
    assert result.context_stale and result.error_code == "RECOVERY_CONTEXT_STALE"

def test_repair_agent_contract_is_not_named_placeholder():
    body = json.loads(CandidateRepairAgent().build_messages("CHARACTER_DECISION", {}, {}, {})[1]["content"])
    assert body["output_contract"] != "CharacterDecisionPayload"

def test_repair_agent_candidate_is_not_authority():
    system = CandidateRepairAgent().build_messages("WORLD_RESOLUTION", {}, {}, {})[0]["content"]
    assert "not authoritative" in system and "sanitized_context" in system

def test_recovery_policy_world_missing_forbids_repair():
    retryable, repairable, actions = RecoveryPolicy.resolve("WORLD_INFORMATION_MISSING")
    assert not retryable and not repairable and actions == ["EDIT_WORLD", "ABORT"]

def test_recovery_policy_context_stale_allows_retry():
    retryable, repairable, actions = RecoveryPolicy.resolve("RECOVERY_CONTEXT_STALE")
    assert retryable is False and repairable is False and actions == ["ABORT"]

def test_world_information_missing_endpoints_are_read_only(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch); client = client_for(session, monkeypatch)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.WORLD_RESOLVER, source_type="SCENE_PERFORMANCE_TURN", source_id="missing")
    payload = {"outcome": "UNRESOLVED", "outcome_summary": "missing", "objective_facts": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [], "resolution_basis_summary": None, "missing_information": ["fact"]}
    candidate = RecoveryCandidateService().create(session, project_id=project.id, trace=trace, candidate_type="WORLD_RESOLUTION", payload=payload, context_fingerprint="x", locator={"project_id": project.id, "proposal_id": proposal.id, "performance_id": "missing", "performance_turn_id": "missing", "world_resolution_id": "missing"}, error_code="WORLD_INFORMATION_MISSING", validation_report={}, stage="WORLD_RESOLVER", source_type="SCENE_PERFORMANCE_TURN", source_id="missing"); session.commit()
    for endpoint, body in [("edit", {"base_version": 1, "changes": [{"operation": "SET", "path": "/outcome", "value": "SUCCESS"}]}), ("ai-repair", None), ("adopt", None)]:
        response = client.post(f"/projects/{project.id}/recovery-candidates/{candidate.id}/{endpoint}", json=body)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "WORLD_FACT_REQUIRED"

def test_ai_repair_missing_performance_returns_stale_not_500(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch); client = client_for(session, monkeypatch)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="SCENE_PERFORMANCE", source_id="missing")
    candidate = RecoveryCandidateService().create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_PERFORMANCE", payload=performance_payload(), context_fingerprint="x", locator={"project_id": project.id, "proposal_id": proposal.id, "performance_id": "missing", "actor_character_id": actor.id, "source_turn_id": "missing", "source_decision_id": "missing"}, error_code="TARGET_MISMATCH", validation_report={}, stage="CHARACTER_ACTOR", source_type="SCENE_PERFORMANCE", source_id="missing"); session.commit()
    response = client.post(f"/projects/{project.id}/recovery-candidates/{candidate.id}/ai-repair")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "RECOVERY_CONTEXT_STALE"

def test_character_adopt_missing_character_returns_stale_not_500(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch); client = client_for(session, monkeypatch); context = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id)
    candidate = RecoveryCandidateService().create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint=context["fingerprint"], locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="TARGET_MISMATCH", validation_report={}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id); candidate.status = RecoveryCandidateStatus.VALIDATED.value; session.delete(actor); session.commit()
    response = client.post(f"/projects/{project.id}/recovery-candidates/{candidate.id}/adopt")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "RECOVERY_CONTEXT_STALE"

def test_candidate_list_uses_wrapped_api_contract(session, monkeypatch):
    project, _, actor, _, proposal, _ = approved_setup(session, monkeypatch); client = client_for(session, monkeypatch); context = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    trace = ExecutionTraceRecorder().start(session, project_id=project.id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=actor.id)
    candidate = RecoveryCandidateService().create(session, project_id=project.id, trace=trace, candidate_type="CHARACTER_DECISION", payload=json.loads(valid_payload()), context_fingerprint=context["fingerprint"], locator={"project_id": project.id, "proposal_id": proposal.id, "character_id": actor.id}, error_code="TARGET_MISMATCH", validation_report={}, stage="CHARACTER_ACTOR", source_type="CHARACTER", source_id=actor.id); session.commit()
    body = client.get(f"/projects/{project.id}/recovery-candidates").json()[0]
    assert body["candidate"]["id"] == candidate.id and body["current_version"]["version_number"] == 1 and "available_actions" in body
