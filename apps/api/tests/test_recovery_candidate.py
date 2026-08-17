import pytest
import json
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution_trace import ExecutionTraceRecorder
from app.models import ExecutionStage, RecoveryCandidate, RecoveryCandidateStatus, CharacterDecision
from app.recovery import RecoveryCandidateService
from app.recovery import RecoveryActionResolver
from app.character_mind import CharacterContextBuilder
from test_llm_character_actor import valid_payload
from test_scene_performance import approved_setup


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

@pytest.mark.parametrize("code", ["KNOWLEDGE_LEAK", "TARGET_MISMATCH", "PREMATURE_REVEAL", "INVALID_CANON_REFERENCE", "CROSS_PROJECT_REFERENCE"])
def test_constraint_errors_are_repairable(code):
    assert RecoveryActionResolver.resolve(code, None) == ["RETRY", "ABORT"]

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
