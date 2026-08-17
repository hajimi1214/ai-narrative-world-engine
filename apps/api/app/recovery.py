"""Explicit, candidate-only recovery workflows.

Recovery never mutates formal world facts. Versions are append-only and are
validated against a freshly rebuilt, permission-filtered context.
"""
import copy
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .character_mind import ActorPerceptionSanitizer, CharacterContextBuilder, CharacterDecisionConstraintChecker
from .llm_actor import CharacterDecisionPayload, build_character_decision_contract, _extract_single_json_object
from .models import CharacterDecision, CharacterDecisionStatus, RecoveryCandidate, RecoveryCandidateStatus, RecoveryCandidateType, RecoveryCandidateVersion, RecoveryVersionOrigin, ScenePerformance, ScenePerformanceTurn, WorldResolution, ResolutionStatus
from .performance import CharacterPerformancePayload, PerformanceActionConstraintChecker, PerformanceCharacterContextBuilder, performance_contract
from .revision import RevisionPatchEngine, target_fingerprint
from .world_resolution import WorldResolutionConstraintChecker, WorldResolutionPayload, WorldResolutionContextBuilder, WorldContextSanitizer, world_resolution_contract
from .execution_trace import TraceSanitizer

CandidateType = Literal["CHARACTER_DECISION", "CHARACTER_PERFORMANCE", "WORLD_RESOLUTION"]

@dataclass
class RecoveryValidationResult:
    schema_valid: bool
    constraint_valid: bool
    context_stale: bool
    normalized_payload: dict[str, Any] | None
    validation_report: dict[str, Any]
    error_code: str | None = None

class RecoveryActionResolver:
    @staticmethod
    def resolve(error_code, candidate=None, repair_attempts=0, max_repair_attempts=1, context_stale=False):
        if context_stale or (candidate and candidate.status == RecoveryCandidateStatus.STALE.value): return ["REBUILD_CONTEXT", "ABORT"]
        if candidate and candidate.status in {RecoveryCandidateStatus.ADOPTED.value, RecoveryCandidateStatus.ABORTED.value}: return []
        if error_code == "WORLD_INFORMATION_MISSING" or (candidate and candidate.initial_error_code == "WORLD_INFORMATION_MISSING"): return ["EDIT_WORLD", "ABORT"]
        if candidate and candidate.status == RecoveryCandidateStatus.VALIDATED.value: return ["ADOPT", "MANUAL_EDIT", "ABORT"]
        if candidate and candidate.status == RecoveryCandidateStatus.OPEN.value:
            return ["AI_REPAIR", "MANUAL_EDIT", "ABORT"] if repair_attempts < max_repair_attempts else ["MANUAL_EDIT", "ABORT"]
        if error_code == "MODEL_OUTPUT_INVALID": return ["RETRY", "ABORT"]
        return ["RETRY", "ABORT"]

class CandidateRepairAgent:
    CONTRACTS = {"CHARACTER_DECISION": build_character_decision_contract, "CHARACTER_PERFORMANCE": performance_contract, "WORLD_RESOLUTION": world_resolution_contract}
    def build_messages(self, candidate_type, context, payload, validation_report):
        return [{"role": "system", "content": "You repair an untrusted structured candidate. The candidate may contain wrong knowledge, wrong IDs, leaked secrets, or Canon conflicts. It is not authoritative. Only sanitized_context is the source of world facts. Do not invent facts, knowledge, entities, abilities, Canon, or world rules. Return exactly one JSON object."}, {"role": "user", "content": json.dumps({"sanitized_context": context, "candidate": payload, "validation_report": validation_report, "candidate_type": candidate_type, "output_contract": self.CONTRACTS[candidate_type]()}, ensure_ascii=True, sort_keys=True)}]
    def repair(self, provider, model, candidate_type, context, payload, validation_report):
        result = provider.generate(self.build_messages(candidate_type, context, payload, validation_report), model)
        return self.CONTRACTS[candidate_type], _extract_single_json_object(result.content), result

class RecoveryPatchChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["SET", "MERGE", "REMOVE"]
    path: str
    value: Any | None = None

class RecoveryEditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: int = Field(ge=1)
    changes: list[RecoveryPatchChange] = Field(min_length=1)

class RecoveryCandidateService:
    MAX_BYTES = 64 * 1024
    LOCATOR_KEYS = {
        "CHARACTER_DECISION": {"project_id", "proposal_id", "character_id"},
        "CHARACTER_PERFORMANCE": {"project_id", "proposal_id", "performance_id", "actor_character_id", "source_turn_id", "source_decision_id"},
        "WORLD_RESOLUTION": {"project_id", "proposal_id", "performance_id", "performance_turn_id", "world_resolution_id"},
    }

    def _safe_locator(self, candidate_type, locator):
        if set(locator) - self.LOCATOR_KEYS[candidate_type]:
            raise ValueError("INVALID_CONTEXT_LOCATOR")
        return {key: locator[key] for key in sorted(locator)}

    def _payload(self, candidate_type, payload):
        if candidate_type == "CHARACTER_DECISION": value = CharacterDecisionPayload.model_validate(payload).model_dump(mode="json")
        elif candidate_type == "CHARACTER_PERFORMANCE": value = CharacterPerformancePayload.model_validate(payload).model_dump(mode="json")
        elif candidate_type == "WORLD_RESOLUTION": value = WorldResolutionPayload.model_validate(payload).model_dump(mode="json")
        else: raise ValueError("UNSUPPORTED_CANDIDATE_TYPE")
        if len(json.dumps(value, ensure_ascii=True, separators=(",", ":"))) > self.MAX_BYTES: raise ValueError("CANDIDATE_TOO_LARGE")
        return value

    def _guard_payload(self, payload):
        if not isinstance(payload, dict): raise ValueError("CANDIDATE_PAYLOAD_OBJECT_REQUIRED")
        if len(json.dumps(payload, ensure_ascii=True, separators=(",", ":"))) > self.MAX_BYTES: raise ValueError("CANDIDATE_TOO_LARGE")
        return payload

    def create(self, db, *, project_id, trace, candidate_type: CandidateType, payload, context_fingerprint, locator, error_code, validation_report, stage, source_type, source_id):
        if not trace.id:
            db.flush()
        existing = db.scalar(select(RecoveryCandidate).where(RecoveryCandidate.source_trace_id == trace.id))
        if existing: return existing
        safe_payload = self._payload(candidate_type, payload)
        candidate = RecoveryCandidate(project_id=project_id, source_trace_id=trace.id, stage=stage, candidate_type=candidate_type, source_type=source_type, source_id=source_id, context_fingerprint=context_fingerprint, context_locator=self._safe_locator(candidate_type, locator), initial_error_code=error_code, status=RecoveryCandidateStatus.OPEN.value, current_version_number=1)
        db.add(candidate); db.flush()
        version = RecoveryCandidateVersion(candidate_id=candidate.id, version_number=1, origin=RecoveryVersionOrigin.ORIGINAL.value, payload=safe_payload, payload_fingerprint=target_fingerprint(safe_payload), schema_valid=True, constraint_valid=False, validation_report=TraceSanitizer.clean(validation_report or {}))
        db.add(version); db.flush()
        return candidate

    def current_version(self, db, candidate):
        return db.scalar(select(RecoveryCandidateVersion).where(RecoveryCandidateVersion.candidate_id == candidate.id, RecoveryCandidateVersion.version_number == candidate.current_version_number))

    def rebuild(self, db, candidate):
        loc = candidate.context_locator
        if candidate.candidate_type == RecoveryCandidateType.CHARACTER_DECISION.value:
            proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, loc["proposal_id"])
            context = CharacterContextBuilder().build(db, loc["project_id"], loc["character_id"], proposal)
            return context, ActorPerceptionSanitizer().sanitize(context)
        if candidate.candidate_type == RecoveryCandidateType.CHARACTER_PERFORMANCE.value:
            proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, loc["proposal_id"]); performance = db.get(ScenePerformance, loc["performance_id"]); turn = db.get(ScenePerformanceTurn, loc["source_turn_id"])
            prior = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id, ScenePerformanceTurn.sequence < turn.sequence).order_by(ScenePerformanceTurn.sequence)).all()
            context = PerformanceCharacterContextBuilder().build(db, loc["project_id"], loc["actor_character_id"], proposal, performance.id, prior)
            return context, ActorPerceptionSanitizer().sanitize(context)
        performance = db.get(ScenePerformance, loc["performance_id"]); turn = db.get(ScenePerformanceTurn, loc["performance_turn_id"]); proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, loc["proposal_id"])
        context = WorldResolutionContextBuilder().build(db, performance, turn, proposal, turn.world_resolution_request)
        return context, WorldContextSanitizer().sanitize(context)

    def validate(self, db, candidate, payload):
        try:
            context, _ = self.rebuild(db, candidate)
        except Exception:
            candidate.status = RecoveryCandidateStatus.STALE.value
            return RecoveryValidationResult(False, False, True, None, {"code": "RECOVERY_CONTEXT_STALE", "issues": []}, "RECOVERY_CONTEXT_STALE")
        if context.get("fingerprint") != candidate.context_fingerprint:
            candidate.status = RecoveryCandidateStatus.STALE.value
            return RecoveryValidationResult(False, False, True, None, {"code": "RECOVERY_CONTEXT_STALE", "issues": []}, "RECOVERY_CONTEXT_STALE")
        try:
            if candidate.candidate_type == RecoveryCandidateType.CHARACTER_DECISION.value:
                parsed = CharacterDecisionPayload.model_validate(payload)
                loc = candidate.context_locator; proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, loc["proposal_id"])
                decision = CharacterDecision(project_id=loc["project_id"], scene_proposal_id=proposal.id, character_id=loc["character_id"], context_fingerprint=candidate.context_fingerprint, **parsed.model_dump(mode="json"))
                report = CharacterDecisionConstraintChecker().validate(db, context, decision); normalized = parsed.model_dump(mode="json"); return RecoveryValidationResult(True, report.valid, False, normalized, report.as_dict(), None if report.valid else "CONSTRAINT_FAILED")
            if candidate.candidate_type == RecoveryCandidateType.CHARACTER_PERFORMANCE.value:
                parsed = CharacterPerformancePayload.model_validate(payload); loc = candidate.context_locator; proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, loc["proposal_id"]); performance = db.get(ScenePerformance, loc["performance_id"])
                decision = CharacterDecision(project_id=loc["project_id"], scene_proposal_id=proposal.id, character_id=loc["actor_character_id"], context_fingerprint=candidate.context_fingerprint, **parsed.decision.model_dump(mode="json"))
                d = CharacterDecisionConstraintChecker().validate(db, context, decision); a = PerformanceActionConstraintChecker().validate(db, context, proposal, decision, parsed.action, performance.active_participant_ids); valid = d.valid and a.valid; return RecoveryValidationResult(True, valid, False, parsed.model_dump(mode="json"), {"decision": d.as_dict(), "action": a.as_dict()}, None if valid else "CONSTRAINT_FAILED")
            parsed = WorldResolutionPayload.model_validate(payload); report = WorldResolutionConstraintChecker().validate(db, context, parsed, candidate.project_id); valid = report["valid"]; return RecoveryValidationResult(True, valid, False, parsed.model_dump(mode="json"), report, None if valid else "CONSTRAINT_FAILED")
        except Exception as exc:
            return RecoveryValidationResult(False, False, False, None, {"code": "MODEL_OUTPUT_INVALID", "issues": [{"path": "$", "message": "Candidate does not match its contract."}]}, "MODEL_OUTPUT_INVALID")

    def edit(self, db, candidate, base_version, changes):
        if candidate.current_version_number != base_version: raise ValueError("RECOVERY_VERSION_STALE")
        if candidate.initial_error_code == "WORLD_INFORMATION_MISSING": raise ValueError("WORLD_FACT_REQUIRED")
        try:
            context, _ = self.rebuild(db, candidate)
        except Exception as exc:
            candidate.status = RecoveryCandidateStatus.STALE.value
            raise ValueError("RECOVERY_CONTEXT_STALE") from exc
        if context.get("fingerprint") != candidate.context_fingerprint:
            candidate.status = RecoveryCandidateStatus.STALE.value
            raise ValueError("RECOVERY_CONTEXT_STALE")
        current = copy.deepcopy(self.current_version(db, candidate).payload); engine = RevisionPatchEngine()
        for change in changes:
            item = change if isinstance(change, RecoveryPatchChange) else RecoveryPatchChange.model_validate(change)
            engine.apply(current, item.operation, item.path, item.value)
        self._guard_payload(current)
        try:
            normalized = self._payload(candidate.candidate_type, current); schema_valid = True
            result = self.validate(db, candidate, normalized)
            if result.context_stale:
                raise ValueError("RECOVERY_CONTEXT_STALE")
            valid, report = result.constraint_valid, result.validation_report
        except ValueError as exc:
            if str(exc) == "RECOVERY_CONTEXT_STALE":
                raise
            normalized = current; schema_valid = False; valid = False
            report = {"code": "MODEL_OUTPUT_INVALID", "issues": [{"path": "$", "message": "Manual candidate does not match its contract."}]}
        number = candidate.current_version_number + 1
        version = RecoveryCandidateVersion(candidate_id=candidate.id, version_number=number, origin=RecoveryVersionOrigin.MANUAL_EDIT.value, parent_version_id=self.current_version(db, candidate).id, payload=normalized, payload_fingerprint=target_fingerprint(normalized), schema_valid=schema_valid, constraint_valid=valid, validation_report=TraceSanitizer.clean(report))
        db.add(version); candidate.current_version_number = number; candidate.status = RecoveryCandidateStatus.VALIDATED.value if valid else RecoveryCandidateStatus.OPEN.value; db.flush(); return version
