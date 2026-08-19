"""Bounded orchestration of the frozen Director -> Performance -> Commit pipeline."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .character_mind import CharacterDecisionConstraintChecker
from .director import DirectorCandidateEngine, DirectorConstraintChecker, DirectorContextBuilder, DirectorProposalFactory, StoryGravityContextBuilder, StoryGravityEngine
from .models import (AutonomousRunStatus, AutonomousStepStatus, AutonomousWorldRun, AutonomousWorldStep, CharacterDecision, CharacterDecisionStatus, DecisionType, DirectorDecisionLog, ExecutionStage, ExecutionStatus, PerformanceMode, PerformanceStatus, ProposalStatus, Project, Scene, SceneProposal, ScenePerformance, ScenePerformanceTurn, ResolverMode, StateDeltaItem, WorldResolution)
from .performance import HeuristicCharacterPerformer, PerformanceActionConstraintChecker, PerformanceCharacterContextBuilder, PerformancePostTurnStateResolver, TurnScheduler
from .performance import LLMCharacterPerformer
from .scene_commit import SceneCommitService
from .versioning import WorldSnapshotBuilder
from .retcon_apply import RetconPendingReplayGuard
from .world_resolution import HeuristicWorldResolver, LLMWorldResolver, WorldResolutionContextBuilder, WorldResolutionConstraintChecker, WorldObservationRouter, WorldResolutionPayload
from .state_delta import StateDeltaCandidateBuilder
from .state_delta_validation import StateDeltaValidator
from .execution_trace import ExecutionTraceRecorder, stable_fingerprint
from .model_router import ModelRouter
from .settings import get_settings
from .ai.factory import get_model_provider


class AutonomousWorldLoopService:
    terminal = {AutonomousRunStatus.COMPLETED, AutonomousRunStatus.FAILED, AutonomousRunStatus.CANCELLED, AutonomousRunStatus.BLOCKED}
    runnable_pause_reasons = {"QUIESCENT", "TURN_LIMIT", "INSUFFICIENT_ACTIVE_PARTICIPANTS"}
    default_stagnation_limit = 3

    def _fingerprint(self, db: Session, project_id: str) -> tuple[int, str]:
        sequence = db.scalar(select(func.max(Scene.sequence)).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE")) or 0
        payload, _ = WorldSnapshotBuilder().build(db, project_id)
        return sequence, stable_fingerprint(self._canonical(payload), "world-snapshot-v1")

    @staticmethod
    def _canonical(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): AutonomousWorldLoopService._canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, list):
            return [AutonomousWorldLoopService._canonical(item) for item in value]
        if isinstance(value, tuple):
            return [AutonomousWorldLoopService._canonical(item) for item in value]
        if isinstance(value, bool) or isinstance(value, str) or value is None:
            return value
        if isinstance(value, (int, float, Decimal)):
            normalized = Decimal(str(value)).normalize()
            return format(normalized, "f")
        return str(value)

    def _run_fingerprint(self, run: AutonomousWorldRun, steps: list[AutonomousWorldStep] | None = None) -> str:
        return stable_fingerprint({
            "project_id": run.project_id,
            "config": run.config or {},
            "scene_budget": run.scene_budget,
            "max_turns_per_scene": run.max_turns_per_scene,
            "performance_mode": getattr(run.performance_mode, "value", run.performance_mode),
            "resolver_mode": getattr(run.resolver_mode, "value", run.resolver_mode),
            "start_sequence": run.start_sequence,
            "last_committed_sequence": run.last_committed_sequence,
            "current_world_fingerprint": run.current_world_fingerprint,
            "status": getattr(run.status, "value", run.status),
            "steps": [{"ordinal": step.ordinal, "candidate_key": step.candidate_key, "output": step.step_output_fingerprint} for step in (steps or []) if getattr(step.status, "value", step.status) == "COMMITTED"],
        }, "autonomous-run-v1")

    def _refresh_run_fingerprint(self, db: Session, run: AutonomousWorldRun) -> None:
        steps = db.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal)).all()
        run.autonomous_run_fingerprint = self._run_fingerprint(run, steps)

    def create_run(self, db: Session, project_id: str, *, scene_budget: int, max_turns_per_scene: int = 6, performance_mode: str = "HEURISTIC", resolver_mode: str = "HEURISTIC", config: dict[str, Any] | None = None, client_request_id: str | None = None) -> AutonomousWorldRun:
        if scene_budget <= 0 or max_turns_per_scene <= 0:
            raise ValueError("INVALID_AUTONOMY_BUDGET")
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise LookupError("PROJECT_NOT_FOUND")
        RetconPendingReplayGuard().assert_progression_allowed(db, project_id)
        if client_request_id:
            prior = db.scalar(select(AutonomousWorldRun).where(AutonomousWorldRun.project_id == project_id, AutonomousWorldRun.client_request_id == client_request_id).order_by(AutonomousWorldRun.created_at.desc(), AutonomousWorldRun.id.desc()))
            if prior:
                if (prior.scene_budget, prior.max_turns_per_scene, getattr(prior.performance_mode, "value", prior.performance_mode), getattr(prior.resolver_mode, "value", prior.resolver_mode), prior.config or {}) != (scene_budget, max_turns_per_scene, performance_mode, resolver_mode, config or {}):
                    raise ValueError("AUTONOMY_REQUEST_MISMATCH")
                return prior
        if db.scalar(select(AutonomousWorldRun).where(AutonomousWorldRun.project_id == project_id, AutonomousWorldRun.active.is_(True))):
            raise ValueError("AUTONOMY_RUN_ACTIVE")
        sequence, fingerprint = self._fingerprint(db, project_id)
        run = AutonomousWorldRun(project_id=project_id, status=AutonomousRunStatus.CREATED, active=True, scene_budget=scene_budget, max_turns_per_scene=max_turns_per_scene, performance_mode=PerformanceMode(performance_mode), resolver_mode=ResolverMode(resolver_mode), start_sequence=sequence, start_world_fingerprint=fingerprint, current_world_fingerprint=fingerprint, autonomous_run_fingerprint="", config=config or {}, client_request_id=client_request_id)
        db.add(run); db.flush()
        self._refresh_run_fingerprint(db, run)
        return run

    def pause(self, db: Session, run_id: str, reason: str = "PAUSED") -> AutonomousWorldRun:
        run = self._run(db, run_id)
        if run.status in self.terminal: raise ValueError("AUTONOMY_RUN_NOT_PAUSABLE")
        run.status = AutonomousRunStatus.PAUSED; run.stop_reason = reason; run.active = True; self._refresh_run_fingerprint(db, run); db.flush(); return run

    def resume(self, db: Session, run_id: str) -> AutonomousWorldRun:
        run = self._run(db, run_id)
        if run.status in self.terminal: raise ValueError("AUTONOMY_RUN_NOT_RESUMABLE")
        RetconPendingReplayGuard().assert_progression_allowed(db, run.project_id)
        sequence, fingerprint = self._fingerprint(db, run.project_id)
        # Baseline is intentionally checked against formal history only.
        if fingerprint != run.current_world_fingerprint or (run.last_committed_sequence is not None and sequence != run.last_committed_sequence):
            run.status = AutonomousRunStatus.BLOCKED; run.stop_reason = "AUTONOMY_BASELINE_CHANGED"; run.active = False; self._refresh_run_fingerprint(db, run); raise ValueError("AUTONOMY_BASELINE_CHANGED")
        run.status = AutonomousRunStatus.RUNNING; run.stop_reason = None; run.last_error_code = None; run.started_at = run.started_at or datetime.utcnow(); self._refresh_run_fingerprint(db, run); db.flush(); return run

    def cancel(self, db: Session, run_id: str) -> AutonomousWorldRun:
        run = self._run(db, run_id)
        if run.status in self.terminal: return run
        run.status = AutonomousRunStatus.CANCELLED; run.active = False; run.stop_reason = "CANCELLED"; run.completed_at = datetime.utcnow(); self._refresh_run_fingerprint(db, run); db.flush(); return run

    def get_status(self, db: Session, run_id: str) -> dict[str, Any]:
        run = self._run(db, run_id)
        step = db.scalar(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run.id).order_by(AutonomousWorldStep.ordinal.desc()))
        return {"run": self.run_payload(run), "current_step": self.step_payload(step) if step else None}

    def advance(self, db: Session, run_id: str, *, max_scenes: int = 1, request_key: str = "default", request_offset: int = 0) -> dict[str, Any]:
        run = db.scalar(select(AutonomousWorldRun).where(AutonomousWorldRun.id == run_id).with_for_update())
        if not run: raise LookupError("AUTONOMY_RUN_NOT_FOUND")
        if max_scenes < 1 or max_scenes > 20: raise ValueError("INVALID_ADVANCE_LIMIT")
        RetconPendingReplayGuard().assert_progression_allowed(db, run.project_id)
        existing_steps = {
            step.request_offset: step
            for step in db.scalars(select(AutonomousWorldStep).where(
                AutonomousWorldStep.run_id == run.id,
                AutonomousWorldStep.request_key == request_key,
                AutonomousWorldStep.request_offset >= request_offset,
                AutonomousWorldStep.request_offset < request_offset + max_scenes,
            )).all()
        }
        if len(existing_steps) == max_scenes:
            return {"run": self.run_payload(run), "steps": [self.step_payload(existing_steps[offset]) for offset in sorted(existing_steps)], "existing": bool(existing_steps)}
        if run.status in self.terminal:
            raise ValueError(run.stop_reason or "AUTONOMY_RUN_NOT_RUNNING")
        if run.status == AutonomousRunStatus.CREATED:
            run.status = AutonomousRunStatus.RUNNING; run.started_at = run.started_at or datetime.utcnow(); db.flush()
        if run.status != AutonomousRunStatus.RUNNING: raise ValueError(run.stop_reason or "AUTONOMY_RUN_NOT_RUNNING")
        steps: list[AutonomousWorldStep] = []
        for offset in range(request_offset, request_offset + min(max_scenes, run.scene_budget - run.committed_scene_count)):
            step = existing_steps.get(offset)
            if step is None:
                step = self.advance_one_scene(db, run, request_key, offset)
            steps.append(step)
            if step.status != AutonomousStepStatus.COMMITTED: break
        if run.committed_scene_count >= run.scene_budget and run.status == AutonomousRunStatus.RUNNING:
            run.status = AutonomousRunStatus.COMPLETED; run.active = False; run.stop_reason = "SCENE_BUDGET_REACHED"; run.completed_at = datetime.utcnow()
        self._refresh_run_fingerprint(db, run)
        db.flush()
        return {"run": self.run_payload(run), "steps": [self.step_payload(step) for step in steps], "existing": bool(existing_steps) and not any(offset not in existing_steps for offset in range(request_offset, request_offset + len(steps)))}

    def advance_one_scene(self, db: Session, run: AutonomousWorldRun, request_key: str, request_offset: int) -> AutonomousWorldStep:
        RetconPendingReplayGuard().assert_progression_allowed(db, run.project_id)
        sequence, before = self._fingerprint(db, run.project_id)
        ordinal = (db.scalar(select(func.max(AutonomousWorldStep.ordinal)).where(AutonomousWorldStep.run_id == run.id)) or 0) + 1
        step = AutonomousWorldStep(project_id=run.project_id, run_id=run.id, ordinal=ordinal, status=AutonomousStepStatus.RUNNING, request_key=request_key, request_offset=request_offset, stage="DIRECTOR", scene_sequence_before=sequence, world_fingerprint_before=before, started_at=datetime.utcnow())
        db.add(step); db.flush()
        trace = ExecutionTraceRecorder().start(db, project_id=run.project_id, stage=ExecutionStage.AUTONOMOUS_LOOP, source_type="AUTONOMOUS_WORLD_RUN", source_id=run.id, input_fingerprint=before)
        try:
            context = DirectorContextBuilder().build(db, run.project_id)
            gravity_context = StoryGravityContextBuilder().build(db, run.project_id)
            gravity = StoryGravityEngine().build(gravity_context)
            candidates = DirectorCandidateEngine().generate(gravity_context, gravity)
            checker = DirectorConstraintChecker(); selected = None; report = None
            for candidate in candidates:
                proposal_data = DirectorProposalFactory().create(run.project_id, gravity_context, gravity, candidate)
                transient = SceneProposal(project_id=run.project_id, context_fingerprint=context["fingerprint"], **proposal_data)
                current_report = checker.validate(db, context, transient)
                if current_report.valid: selected, report = candidate, current_report; break
            if not selected:
                ExecutionTraceRecorder().block(trace, "NO_VALID_DIRECTOR_CANDIDATE")
                return self._blocked(step, run, "NO_VALID_DIRECTOR_CANDIDATE")
            if getattr(selected.proposal_type, "value", selected.proposal_type) == "NEW_THREAD":
                ExecutionTraceRecorder().block(trace, "AUTONOMY_AUTHOR_INTERVENTION_REQUIRED")
                return self._blocked(step, run, "AUTONOMY_AUTHOR_INTERVENTION_REQUIRED")
            step.step_input_fingerprint = stable_fingerprint({"run_id": run.id, "ordinal": ordinal, "world": before, "sequence": sequence, "gravity": gravity.gravity_fingerprint, "candidate": selected.candidate_key, "config": run.config or {}}, "autonomous-step-input-v1")
            proposal = SceneProposal(project_id=run.project_id, context_fingerprint=context["fingerprint"], status=ProposalStatus.APPROVED, **DirectorProposalFactory().create(run.project_id, gravity_context, gravity, selected))
            db.add(proposal); db.flush()
            db.add(DirectorDecisionLog(project_id=run.project_id, context_version=context["version"], proposal_id=proposal.id, decision_type=DecisionType.DRY_RUN, brief_reason="Autonomous Story Gravity selection.", validation_result=report.as_dict()))
            db.add(DirectorDecisionLog(project_id=run.project_id, context_version=context["version"], proposal_id=proposal.id, decision_type=DecisionType.APPROVE, brief_reason="Explicit Autonomous Run approval.", validation_result=report.as_dict()))
            performance = ScenePerformance(project_id=run.project_id, scene_proposal_id=proposal.id, take_number=1, proposal_context_fingerprint=context["fingerprint"], mode=run.performance_mode, participant_order=list(proposal.participants or []), active_participant_ids=list(proposal.participants or []), max_turns=run.max_turns_per_scene, turn_count=0)
            if not performance.active_participant_ids: return self._blocked(step, run, "NO_RUNNABLE_SCENE_PARTICIPANT")
            db.add(performance); db.flush(); step.proposal_id = proposal.id; step.performance_id = performance.id; step.director_context_fingerprint = context["fingerprint"]; step.gravity_fingerprint = gravity.gravity_fingerprint; step.candidate_key = selected.candidate_key; step.stage = "PERFORMANCE"
            self._perform(db, run, performance, proposal)
            if performance.status == PerformanceStatus.AWAITING_WORLD:
                if not self._resolve_pending(db, run, performance, proposal): return self._blocked(step, run, "WORLD_INFORMATION_MISSING", stage="WORLD_RESOLUTION")
                self._perform(db, run, performance, proposal)
            if performance.status == PerformanceStatus.AWAITING_WORLD: return self._blocked(step, run, "WORLD_INFORMATION_MISSING", stage="WORLD_RESOLUTION")
            if performance.turn_count == 0: return self._blocked(step, run, "EMPTY_PERFORMANCE", stage="PERFORMANCE")
            if performance.status == PerformanceStatus.RUNNING: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "TURN_LIMIT"
            if performance.stop_reason not in self.runnable_pause_reasons:
                return self._blocked(step, run, performance.stop_reason or "CHARACTER_DECISION_REJECTED", stage="PERFORMANCE")
            result = SceneCommitService().commit(db, run.project_id, performance.id)
            _, after = self._fingerprint(db, run.project_id)
            step.status = AutonomousStepStatus.COMMITTED; step.stage = "COMMITTED"; step.scene_commit_id = result.commit.id; step.scene_id = result.scene.id; step.checkpoint_id = result.checkpoint.id; step.scene_sequence_after = result.scene.sequence; step.world_fingerprint_after = after; step.turn_count = performance.turn_count; step.resolution_count = db.scalar(select(func.count(WorldResolution.id)).where(WorldResolution.performance_id == performance.id)) or 0; step.delta_batch_ids = [batch.id for batch in result.batches]; step.step_output_fingerprint = stable_fingerprint({"scene_id": result.scene.id, "sequence": result.scene.sequence, "commit": result.commit.commit_fingerprint, "checkpoint": result.checkpoint.checkpoint_fingerprint, "world": after}, "autonomous-step-output-v1"); step.completed_at = datetime.utcnow(); run.committed_scene_count += 1; run.last_committed_sequence = result.scene.sequence; run.current_world_fingerprint = after; run.status = AutonomousRunStatus.RUNNING; run.stop_reason = None
            self._refresh_run_fingerprint(db, run)
            ExecutionTraceRecorder().succeed(trace, output_fingerprint=step.step_output_fingerprint)
            self._apply_stagnation_guard(db, run)
            return step
        except Exception as exc:
            step.status = AutonomousStepStatus.FAILED; step.error_code = str(exc); step.error_detail = {"message": str(exc)}; step.stage = "FAILED"; run.status = AutonomousRunStatus.PAUSED; run.stop_reason = str(exc); run.last_error_code = str(exc); run.last_error_detail = {"message": str(exc)}; ExecutionTraceRecorder().fail(trace, str(exc)); raise

    def _perform(self, db, run, performance, proposal):
        turns = []
        for _ in range(performance.max_turns):
            if performance.status in {PerformanceStatus.AWAITING_WORLD, PerformanceStatus.PAUSED}: break
            actor_id = TurnScheduler().next_actor(performance, turns)
            if not actor_id: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "INSUFFICIENT_ACTIVE_PARTICIPANTS"; break
            context = PerformanceCharacterContextBuilder().build(db, run.project_id, actor_id, proposal, performance.id, turns)
            if run.performance_mode == PerformanceMode.HEURISTIC:
                raw, model_result = HeuristicCharacterPerformer().perform(context)
            else:
                settings = get_settings(); route = ModelRouter().resolve(db, run.project_id, settings, "CHARACTER")
                actor_view = __import__("app.character_mind", fromlist=["ActorPerceptionSanitizer"]).ActorPerceptionSanitizer().sanitize(context)
                raw, model_result = LLMCharacterPerformer(get_model_provider(settings, route.provider, route.base_url), route.model).perform(actor_view)
            decision = CharacterDecision(project_id=run.project_id, scene_proposal_id=proposal.id, character_id=actor_id, context_fingerprint=context["fingerprint"], **raw["decision"])
            decision_report = CharacterDecisionConstraintChecker().validate(db, context, decision)
            from .performance import PerformanceActionPayload
            action = PerformanceActionPayload.model_validate(raw["action"])
            action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, decision, action, performance.active_participant_ids)
            if not decision_report.valid or not action_report.valid:
                decision.status = CharacterDecisionStatus.REJECTED; db.add(decision); performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "CHARACTER_DECISION_REJECTED"; break
            decision.status = CharacterDecisionStatus.VALID; db.add(decision); db.flush()
            turn = ScenePerformanceTurn(project_id=run.project_id, performance_id=performance.id, sequence=len(turns) + 1, actor_character_id=actor_id, actor_context_fingerprint=context["fingerprint"], character_decision_id=decision.id, action_visibility=action.visibility, observable_action=action.observable_action, spoken_content=action.spoken_content, recipient_character_ids=[], requires_world_resolution=action.requires_world_resolution, world_resolution_request=action.world_resolution_request.model_dump(mode="json") if action.world_resolution_request else None, validation_result={"decision": decision_report.as_dict(), "action": action_report.as_dict()})
            db.add(turn); db.flush(); turns.append(turn); performance.turn_count += 1; PerformancePostTurnStateResolver().apply(performance, turns, turn, decision, action, db); db.flush()

    def _resolve_pending(self, db, run, performance, proposal) -> bool:
        turn = db.scalar(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id, ScenePerformanceTurn.requires_world_resolution.is_(True)).order_by(ScenePerformanceTurn.sequence.desc()))
        if not turn or not turn.world_resolution_request:
            return False
        request = turn.world_resolution_request
        context = WorldResolutionContextBuilder().build(db, performance, turn, proposal, request)
        if run.resolver_mode == ResolverMode.HEURISTIC:
            raw, model_result = HeuristicWorldResolver().resolve(context)
        else:
            settings = get_settings(); route = ModelRouter().resolve(db, run.project_id, settings, "WORLD")
            raw, model_result = LLMWorldResolver(get_model_provider(settings, route.provider, route.base_url), route.model).resolve(context)
        payload = WorldResolutionPayload.model_validate(raw)
        report = WorldResolutionConstraintChecker().validate(db, context, payload, run.project_id)
        if not report.get("valid") or getattr(payload.outcome, "value", payload.outcome) == "UNRESOLVED":
            return False
        resolution = WorldResolution(project_id=run.project_id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=run.resolver_mode, world_context_fingerprint=context["fingerprint"], status="VALID", **payload.model_dump(mode="json"))
        resolution.recipient_character_ids = WorldObservationRouter().recipients(performance, turn, resolution)
        db.add(resolution); db.flush()
        batch, _, _ = StateDeltaCandidateBuilder().derive(db, run.project_id, resolution.id)
        validation = StateDeltaValidator().validate(db, run.project_id, batch.id)
        if not validation.report.get("valid", not any(item.get("severity") == "BLOCKING" for item in validation.report.get("issues", []))):
            return False
        performance.status = PerformanceStatus.RUNNING; performance.stop_reason = None; db.flush()
        return True

    def _apply_stagnation_guard(self, db: Session, run: AutonomousWorldRun) -> None:
        """Operational guard only: never changes a candidate or forces a character action."""
        limit = int((run.config or {}).get("stagnation_limit", self.default_stagnation_limit))
        if limit <= 0:
            return
        recent = db.scalars(select(AutonomousWorldStep).where(
            AutonomousWorldStep.run_id == run.id,
            AutonomousWorldStep.status == AutonomousStepStatus.COMMITTED,
        ).order_by(AutonomousWorldStep.ordinal.desc()).limit(limit)).all()
        if len(recent) < limit or len({item.candidate_key for item in recent}) != 1:
            return
        for step in recent:
            if step.delta_batch_ids and db.scalar(select(func.count(StateDeltaItem.id)).where(StateDeltaItem.batch_id.in_(step.delta_batch_ids))) :
                return
        performances = [db.get(ScenePerformance, item.performance_id) for item in recent]
        if not all(performance and performance.stop_reason in {"QUIESCENT", "TURN_LIMIT"} for performance in performances):
            return
        run.status = AutonomousRunStatus.PAUSED
        run.stop_reason = "STAGNATION_GUARD"
        run.active = True
        self._refresh_run_fingerprint(db, run)

    def _blocked(self, step, run, reason, stage="BLOCKED"):
        step.status = AutonomousStepStatus.BLOCKED if reason == "STATE_DELTA_REJECTED" else AutonomousStepStatus.PAUSED; step.stage = stage; step.stop_reason = reason; step.completed_at = datetime.utcnow(); run.status = AutonomousRunStatus.BLOCKED if reason == "STATE_DELTA_REJECTED" else AutonomousRunStatus.PAUSED; run.stop_reason = reason; return step

    def _run(self, db, run_id):
        run = db.get(AutonomousWorldRun, run_id)
        if not run: raise LookupError("AUTONOMY_RUN_NOT_FOUND")
        return run

    @staticmethod
    def run_payload(run):
        return {"id": run.id, "project_id": run.project_id, "status": getattr(run.status, "value", run.status), "active": run.active, "scene_budget": run.scene_budget, "committed_scene_count": run.committed_scene_count, "max_turns_per_scene": run.max_turns_per_scene, "performance_mode": getattr(run.performance_mode, "value", run.performance_mode), "resolver_mode": getattr(run.resolver_mode, "value", run.resolver_mode), "start_sequence": run.start_sequence, "last_committed_sequence": run.last_committed_sequence, "current_world_fingerprint": run.current_world_fingerprint, "autonomous_run_fingerprint": run.autonomous_run_fingerprint, "stop_reason": run.stop_reason, "last_error_code": run.last_error_code}

    @staticmethod
    def step_payload(step):
        if not step: return None
        return {"id": step.id, "ordinal": step.ordinal, "status": getattr(step.status, "value", step.status), "stage": step.stage, "candidate_key": step.candidate_key, "proposal_id": step.proposal_id, "performance_id": step.performance_id, "scene_id": step.scene_id, "scene_commit_id": step.scene_commit_id, "checkpoint_id": step.checkpoint_id, "turn_count": step.turn_count, "resolution_count": step.resolution_count, "world_before": step.world_fingerprint_before, "world_after": step.world_fingerprint_after, "step_input_fingerprint": step.step_input_fingerprint, "step_output_fingerprint": step.step_output_fingerprint, "stop_reason": step.stop_reason, "error_code": step.error_code}
