"""Shared persisted Performance and WorldResolution runtime operations.

The HTTP API and the autonomous orchestrator intentionally use these operations
instead of maintaining independent turn or resolver semantics.  They do not
commit: their caller owns the transaction boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai.errors import MODEL_OUTPUT_INVALID, ModelProviderError
from .ai.factory import get_model_provider
from .character_mind import ActorPerceptionSanitizer, CharacterDecisionConstraintChecker
from .director import DirectorContextBuilder
from .execution_trace import ExecutionTraceRecorder, stable_fingerprint
from .model_router import ModelRouter
from .models import (
    Character, CharacterDecision, CharacterDecisionStatus, ExecutionStage,
    PerformanceMode, PerformanceStatus, ResolverMode, ResolutionOutcome,
    ResolutionStatus, ScenePerformance, ScenePerformanceTurn, SceneProposal,
    WorldEntity, WorldResolution,
)
from .performance import (
    HeuristicCharacterPerformer, LLMCharacterPerformer, PerformanceActionConstraintChecker,
    PerformanceActionPayload, PerformanceCharacterContextBuilder,
    PerformanceObservationRouter, PerformancePostTurnStateResolver, TurnScheduler,
)
from .recovery import RecoveryCandidateService
from .settings import get_settings
from .world_resolution import (
    HeuristicWorldResolver, LLMWorldResolver, WorldObservationRouter,
    WorldResolutionConstraintChecker, WorldResolutionContextBuilder,
    WorldResolutionPayload,
)


@dataclass
class RuntimeFailure(Exception):
    code: str
    detail: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.code


def _primary_issue(report: dict[str, Any]) -> str:
    codes = sorted(str(item.get("code")) for item in report.get("issues", []) if item.get("code"))
    return codes[0] if codes else "VALIDATION_BLOCKED"


def persisted_turns(db: Session, performance_id: str) -> list[ScenePerformanceTurn]:
    return db.scalars(
        select(ScenePerformanceTurn)
        .where(ScenePerformanceTurn.performance_id == performance_id)
        .order_by(ScenePerformanceTurn.sequence, ScenePerformanceTurn.id)
    ).all()


class PerformanceRuntimeService:
    """One persisted turn, with the same scheduler, context, routing and checks."""

    def step(self, db: Session, project_id: str, performance: ScenePerformance, proposal: SceneProposal, *, heuristic_performer=None, model_provider_factory=None) -> dict[str, Any]:
        initial_status, initial_stop_reason = performance.status, performance.stop_reason
        if getattr(performance.status, "value", performance.status) == "READY":
            performance.status = PerformanceStatus.RUNNING
        if performance.status != PerformanceStatus.RUNNING:
            raise RuntimeFailure("PERFORMANCE_NOT_RUNNABLE", {"status": getattr(performance.status, "value", performance.status), "stop_reason": performance.stop_reason})
        current = DirectorContextBuilder().build(db, project_id)
        if proposal.context_fingerprint != current["fingerprint"] or performance.proposal_context_fingerprint != current["fingerprint"]:
            performance.status = PerformanceStatus.INVALIDATED
            performance.stop_reason = "STALE_PERFORMANCE"
            raise RuntimeFailure("STALE_PERFORMANCE")
        turns = persisted_turns(db, performance.id)
        sequences = [turn.sequence for turn in turns]
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeFailure("PERFORMANCE_TURN_SEQUENCE_INVALID")
        if performance.turn_count != len(turns):
            # The persisted ledger is authoritative; repair only the derived counter.
            performance.turn_count = len(turns)
        if performance.turn_count >= performance.max_turns:
            performance.status = PerformanceStatus.PAUSED
            performance.stop_reason = "TURN_LIMIT"
            return {"performance": performance, "turn": None, "decision": None, "paused": True}
        previous = db.get(CharacterDecision, turns[-1].character_decision_id) if turns else None
        resume_actor = None
        if turns and turns[-1].requires_world_resolution:
            resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turns[-1].id, WorldResolution.status == ResolutionStatus.VALID))
            if resolution:
                resume_actor = turns[-1].actor_character_id
        actor_id = resume_actor or TurnScheduler().next_actor(performance, turns, previous.target_character_id if previous else None)
        if not actor_id:
            performance.status = PerformanceStatus.PAUSED
            performance.stop_reason = "INSUFFICIENT_ACTIVE_PARTICIPANTS"
            return {"performance": performance, "turn": None, "decision": None, "paused": True}
        context = PerformanceCharacterContextBuilder().build(db, project_id, actor_id, proposal, performance.id, turns)
        trace = ExecutionTraceRecorder().start(
            db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR,
            source_type="SCENE_PERFORMANCE", source_id=performance.id,
            provider="HEURISTIC" if performance.mode == PerformanceMode.HEURISTIC else None,
            input_fingerprint=context["fingerprint"],
        )
        try:
            if performance.mode == PerformanceMode.HEURISTIC:
                raw, model_result = (heuristic_performer or HeuristicCharacterPerformer)().perform(context)
            else:
                settings = get_settings()
                route = ModelRouter().resolve(db, project_id, settings, "CHARACTER")
                trace.provider, trace.model = route.provider, route.model
                raw, model_result = LLMCharacterPerformer((model_provider_factory or get_model_provider)(settings, route.provider, route.base_url), route.model).perform(ActorPerceptionSanitizer().sanitize(context))
        except ModelProviderError as exc:
            ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status)
            performance.status, performance.stop_reason = initial_status, initial_stop_reason
            raise RuntimeFailure(exc.code, {"upstream_status": exc.upstream_status}) from exc
        try:
            action = PerformanceActionPayload.model_validate(raw["action"])
        except Exception as exc:
            ExecutionTraceRecorder().block(trace, MODEL_OUTPUT_INVALID)
            raise RuntimeFailure(MODEL_OUTPUT_INVALID) from exc
        decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=actor_id, context_fingerprint=context["fingerprint"], **raw["decision"])
        decision_report = CharacterDecisionConstraintChecker().validate(db, context, decision)
        action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, decision, action, performance.active_participant_ids)
        valid = decision_report.valid and action_report.valid
        report = {"decision": decision_report.as_dict(), "action": action_report.as_dict()}
        decision.status = CharacterDecisionStatus.VALID if valid else CharacterDecisionStatus.REJECTED
        db.add(decision)
        db.flush()
        recipients = PerformanceObservationRouter().recipients(
            action.visibility,
            [item for item in performance.participant_order if item in performance.active_participant_ids],
            actor_id,
            action.target_character_id,
        )
        turn = ScenePerformanceTurn(
            project_id=project_id, performance_id=performance.id, sequence=len(turns) + 1,
            actor_character_id=actor_id, actor_context_fingerprint=context["fingerprint"],
            character_decision_id=decision.id, action_visibility=action.visibility,
            observable_action=action.observable_action if valid else None,
            spoken_content=action.spoken_content if valid else None,
            recipient_character_ids=recipients if valid else [],
            requires_world_resolution=action.requires_world_resolution if valid else False,
            world_resolution_request=action.world_resolution_request.model_dump(mode="json") if valid and action.world_resolution_request else None,
            validation_result=report,
        )
        db.add(turn)
        db.flush()
        performance.turn_count = len(turns) + 1
        if not valid:
            code = _primary_issue({"issues": report["decision"]["issues"] + report["action"]["issues"]})
            ExecutionTraceRecorder().block(trace, code, validation_report=report)
            candidate = RecoveryCandidateService().create(
                db, project_id=project_id, trace=trace, candidate_type="CHARACTER_PERFORMANCE", payload=raw,
                context_fingerprint=context["fingerprint"],
                locator={"project_id": project_id, "proposal_id": proposal.id, "performance_id": performance.id, "actor_character_id": actor_id, "source_turn_id": turn.id, "source_decision_id": decision.id},
                error_code=code, validation_report=report, stage=ExecutionStage.CHARACTER_ACTOR.value,
                source_type="SCENE_PERFORMANCE", source_id=performance.id,
            )
            performance.status, performance.stop_reason = PerformanceStatus.PAUSED, "CHARACTER_DECISION_REJECTED"
            return {"performance": performance, "turn": turn, "decision": decision, "validation_report": report, "recovery_candidate_id": candidate.id}
        ExecutionTraceRecorder().succeed(trace, latency_ms=getattr(model_result, "latency_ms", None), request_id=getattr(model_result, "request_id", None), output_fingerprint=stable_fingerprint(raw))
        PerformancePostTurnStateResolver().apply(performance, turns + [turn], turn, decision, action, db)
        return {"performance": performance, "turn": turn, "decision": decision, "validation_report": report}


class WorldResolutionRuntimeService:
    """One pending world request. Non-valid outcomes are durable audit rows."""

    def resolve(self, db: Session, project_id: str, performance: ScenePerformance, proposal: SceneProposal, mode: ResolverMode | str | None = None, *, heuristic_resolver=None, model_provider_factory=None) -> dict[str, Any]:
        if performance.status != PerformanceStatus.AWAITING_WORLD:
            raise RuntimeFailure("PERFORMANCE_NOT_AWAITING_WORLD")
        turns = list(reversed(persisted_turns(db, performance.id)))
        turn = next((item for item in turns if item.requires_world_resolution and (not (row := db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == item.id))) or row.status in {ResolutionStatus.UNRESOLVED, ResolutionStatus.REJECTED})), None)
        if not turn or not turn.world_resolution_request:
            raise RuntimeFailure("NO_PENDING_WORLD_REQUEST")
        request = turn.world_resolution_request
        if request.get("target_character_id"):
            target = db.get(Character, request["target_character_id"])
            if not target or target.project_id != project_id:
                raise RuntimeFailure("CROSS_PROJECT_REFERENCE")
            if target.id not in (performance.active_participant_ids or []):
                raise RuntimeFailure("INVALID_TARGET")
        if request.get("target_entity_id"):
            target = db.get(WorldEntity, request["target_entity_id"])
            if not target or target.project_id != project_id:
                raise RuntimeFailure("CROSS_PROJECT_REFERENCE" if target else "INVALID_ENTITY_REFERENCE")
        context = WorldResolutionContextBuilder().build(db, performance, turn, proposal, request)
        context_fingerprint = context["fingerprint"]
        try:
            mode = ResolverMode(mode or performance.mode.value)
        except ValueError as exc:
            raise RuntimeFailure("INVALID_RESOLVER_MODE") from exc
        trace = ExecutionTraceRecorder().start(
            db, project_id=project_id, stage=ExecutionStage.WORLD_RESOLVER,
            source_type="SCENE_PERFORMANCE_TURN", source_id=turn.id,
            provider="HEURISTIC" if mode == ResolverMode.HEURISTIC else None,
            input_fingerprint=context_fingerprint,
        )
        try:
            if mode == ResolverMode.HEURISTIC:
                raw, model_result = (heuristic_resolver or HeuristicWorldResolver)().resolve(context)
            else:
                settings = get_settings()
                route = ModelRouter().resolve(db, project_id, settings, "WORLD")
                trace.provider, trace.model = route.provider, route.model
                raw, model_result = LLMWorldResolver((model_provider_factory or get_model_provider)(settings, route.provider, route.base_url), route.model).resolve(context)
            payload = WorldResolutionPayload.model_validate(raw)
        except ModelProviderError as exc:
            ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status)
            raise RuntimeFailure(exc.code, {"upstream_status": exc.upstream_status}) from exc
        except Exception as exc:
            ExecutionTraceRecorder().block(trace, MODEL_OUTPUT_INVALID)
            raise RuntimeFailure(MODEL_OUTPUT_INVALID) from exc
        fresh = WorldResolutionContextBuilder().build(db, performance, turn, proposal, request)
        if fresh["fingerprint"] != context_fingerprint:
            ExecutionTraceRecorder().block(trace, "WORLD_CONTEXT_STALE")
            raise RuntimeFailure("WORLD_CONTEXT_STALE")
        report = WorldResolutionConstraintChecker().validate(db, fresh, payload, project_id)
        status = ResolutionStatus.VALID if report["valid"] and payload.outcome != ResolutionOutcome.UNRESOLVED else (ResolutionStatus.UNRESOLVED if report["valid"] else ResolutionStatus.REJECTED)
        resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id))
        if resolution and resolution.status == ResolutionStatus.VALID:
            raise RuntimeFailure("WORLD_ALREADY_RESOLVED")
        values = dict(project_id=project_id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=mode, world_context_fingerprint=context_fingerprint, status=status, **payload.model_dump(mode="json"))
        if resolution:
            for key, value in values.items():
                if key not in {"project_id", "performance_id", "performance_turn_id"}:
                    setattr(resolution, key, value)
        else:
            resolution = WorldResolution(**values)
        resolution.recipient_character_ids = WorldObservationRouter().recipients(performance, turn, resolution)
        db.add(resolution)
        db.flush()
        if status == ResolutionStatus.VALID:
            ExecutionTraceRecorder().succeed(trace, latency_ms=getattr(model_result, "latency_ms", None), request_id=getattr(model_result, "request_id", None), output_fingerprint=stable_fingerprint(payload.model_dump(mode="json")))
            performance.status, performance.stop_reason = PerformanceStatus.RUNNING, None
            candidate = None
        else:
            code = "WORLD_INFORMATION_MISSING" if status == ResolutionStatus.UNRESOLVED else _primary_issue(report)
            ExecutionTraceRecorder().block(trace, code, validation_report=report)
            candidate = RecoveryCandidateService().create(
                db, project_id=project_id, trace=trace, candidate_type="WORLD_RESOLUTION", payload=payload.model_dump(mode="json"),
                context_fingerprint=context_fingerprint,
                locator={"project_id": project_id, "proposal_id": proposal.id, "performance_id": performance.id, "performance_turn_id": turn.id, "world_resolution_id": resolution.id},
                error_code=code, validation_report=report, stage=ExecutionStage.WORLD_RESOLVER.value,
                source_type="SCENE_PERFORMANCE_TURN", source_id=turn.id,
            )
            performance.status = PerformanceStatus.AWAITING_WORLD
            performance.stop_reason = "WORLD_INFORMATION_MISSING" if status == ResolutionStatus.UNRESOLVED else "WORLD_RESOLUTION_REJECTED"
        return {"performance": performance, "resolution": resolution, "validation_report": report, "recovery_candidate_id": candidate.id if candidate else None}
