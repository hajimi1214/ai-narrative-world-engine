"""Atomic normal-runtime Scene Commit engine for Phase 7C."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .director import DirectorContextBuilder
from .execution_trace import ExecutionTraceRecorder, stable_fingerprint
from .models import (
    CharacterDecision, CharacterDecisionStatus, CharacterKnowledge, CharacterMemory,
    ExecutionStage, ExecutionStatus, KnowledgeStatus, PerformanceStatus, Project,
    ProposalStatus, ResolutionStatus, Scene, SceneCommit, SceneCommitStatus,
    SceneExecutionBinding, ScenePerformance, ScenePerformanceTurn, SceneProposal,
    SceneStateCheckpoint, SceneStatus, SnapshotType, StateDeltaBatch,
    StateDeltaBatchStatus, StateDeltaDomain, StateDeltaItem, StateDeltaTargetType,
    StoryThread, ThreadStatus, WorldResolution,
)
from .retcon_apply import RetconPendingReplayGuard
from .revision import _pointer
from .state_delta import (
    FormalWorldStateReader, StateDeltaCandidateBuilder,
    StateDeltaInputFingerprintBuilder, WorldResolutionStateDeltaTranslator,
    compute_state_delta_after, state_delta_item_fingerprint,
)
from .state_delta_validation import StateDeltaValidationWorldView, StateDeltaValidator, normalize_world_time
from .state_effect_contract import StateEffectPayload
from .versioning import WorldSnapshotBuilder
from .historical import SceneCheckpointService, SceneCheckpointOrigin
from .snapshot_storage import ProjectWorldSnapshotHeadService, SceneCommitFormalMutationGuard
from .causal_ledger import CausalLedgerService
from .scaling import ProjectHistoryProjectionService
from .formal_state import FormalStateIdentityService
from .retrieval_index import CognitionRetrievalProjectionService


@dataclass
class SceneCommitResult:
    commit: SceneCommit
    scene: Scene
    batches: list[StateDeltaBatch]
    checkpoint: SceneStateCheckpoint
    idempotent: bool


@dataclass
class CommitPreparation:
    project: Project
    performance: ScenePerformance
    proposal: SceneProposal
    turns: list[ScenePerformanceTurn]
    decisions: list[CharacterDecision]
    resolutions: list[WorldResolution]
    batches: list[StateDeltaBatch]
    items: list[StateDeltaItem]
    pre_fingerprint: str
    source_fingerprint: str


class SceneDeltaApplyEngine:
    """Internal-only after-value applier. It has no route-level entrypoint."""

    reader = FormalWorldStateReader()
    builder = StateDeltaCandidateBuilder()

    def apply(self, db: Session, project_id: str, items: list[StateDeltaItem]) -> None:
        for item in items:
            effect = StateEffectPayload.model_validate(item.evidence["state_effect"])
            self.builder._validate_effect(effect)
            target = self.reader.target(db, project_id, item.target_type, item.target_id)
            self._assign(target, item, effect)
        guard = db.info.get("scene_commit_mutation_guard")
        if guard:
            guard.observe()
        db.flush()
        self.verify(db, project_id, items)

    def verify(self, db: Session, project_id: str, items: list[StateDeltaItem]) -> None:
        for item in items:
            effect = StateEffectPayload.model_validate(item.evidence["state_effect"])
            actual, found = self.reader.before_value(db, project_id, effect)
            if not found or not self._same_value(effect.domain, actual, item.after_value):
                raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")

    def _assign(self, target: Any, item: StateDeltaItem, effect: StateEffectPayload) -> None:
        parts = _pointer(item.path)
        if not parts:
            raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")
        root = parts[0]
        allowed = {
            StateDeltaDomain.CHARACTER_LOCATION: {"current_state"},
            StateDeltaDomain.CHARACTER_INVENTORY: {"inventory"},
            StateDeltaDomain.CHARACTER_RELATIONSHIP: {"relationships"},
            StateDeltaDomain.CHARACTER_PHYSICAL_STATE: {"physical_state"},
            StateDeltaDomain.CHARACTER_EMOTIONAL_STATE: {"emotional_state"},
            StateDeltaDomain.CHARACTER_CURRENT_STATE: {"current_state"},
            StateDeltaDomain.WORLD_ENTITY_PROFILE: {"profile"},
            StateDeltaDomain.WORLD_ENTITY_ACTIVE: {"active"},
            StateDeltaDomain.STORY_THREAD_STATE: {"state"},
            StateDeltaDomain.STORY_THREAD_STATUS: {"status"},
            StateDeltaDomain.STORY_THREAD_PROGRESS: {"progress"},
            StateDeltaDomain.WORLD_TIME: {"current_world_time"},
        }
        if root not in allowed.get(item.domain, set()):
            raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")
        value = copy.deepcopy(item.after_value)
        if item.domain == StateDeltaDomain.WORLD_TIME:
            normalized = normalize_world_time(value)
            if normalized is None:
                raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")
            setattr(target, root, normalized)
            return
        if item.domain == StateDeltaDomain.STORY_THREAD_STATUS:
            setattr(target, root, ThreadStatus(value))
            return
        if len(parts) == 1:
            setattr(target, root, value)
            return
        document = copy.deepcopy(getattr(target, root))
        current: Any = document
        for part in parts[1:-1]:
            if not isinstance(current, dict):
                raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")
            if part not in current:
                if effect.operation.name != "UPSERT":
                    raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")
                current[part] = {}
            current = current[part]
        if not isinstance(current, dict):
            raise ValueError("SCENE_COMMIT_APPLY_RESULT_MISMATCH")
        current[parts[-1]] = value
        setattr(target, root, document)

    def _same_value(self, domain: StateDeltaDomain, actual: Any, expected: Any) -> bool:
        if domain == StateDeltaDomain.WORLD_TIME:
            return normalize_world_time(actual.isoformat() if hasattr(actual, "isoformat") else actual) == normalize_world_time(expected)
        return actual == expected


class SceneCommitObservedFactMatcher:
    """Match a state effect to an exact structured, observable resolution fact."""

    @staticmethod
    def canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)

    def match(self, resolution: WorldResolution, effect: StateEffectPayload) -> dict[str, Any] | None:
        expected = self._expected_identity(effect)
        if expected is None:
            return None
        subject_types, predicate = expected
        for fact in resolution.objective_facts or []:
            if not isinstance(fact, dict):
                continue
            if (
                fact.get("subject_type") in subject_types
                and fact.get("subject_id") == effect.target_id
                and fact.get("predicate") == predicate
                and self.canonical_json(fact.get("value")) == self.canonical_json(effect.value)
            ):
                return {
                    "subject_type": fact["subject_type"],
                    "subject_id": fact["subject_id"],
                    "predicate": fact["predicate"],
                    "value": fact.get("value"),
                }
        return None

    def identity(self, fact: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(fact["subject_type"]),
            str(fact["subject_id"]),
            str(fact["predicate"]),
            self.canonical_json(fact.get("value")),
        )

    def _expected_identity(self, effect: StateEffectPayload) -> tuple[set[str], str] | None:
        parts = _pointer(effect.path)
        if effect.target_type == StateDeltaTargetType.WORLD_ENTITY:
            if effect.domain == StateDeltaDomain.WORLD_ENTITY_PROFILE and len(parts) == 2 and parts[0] == "profile":
                return {"ENTITY", "LOCATION"}, parts[1]
            if effect.domain == StateDeltaDomain.WORLD_ENTITY_ACTIVE and parts == ["active"]:
                return {"ENTITY", "LOCATION"}, "active"
            return None
        if effect.target_type != StateDeltaTargetType.CHARACTER:
            return None
        roots = {
            StateDeltaDomain.CHARACTER_LOCATION: ("current_state", "location_id"),
            StateDeltaDomain.CHARACTER_INVENTORY: ("inventory", None),
            StateDeltaDomain.CHARACTER_PHYSICAL_STATE: ("physical_state", None),
            StateDeltaDomain.CHARACTER_EMOTIONAL_STATE: ("emotional_state", None),
            StateDeltaDomain.CHARACTER_CURRENT_STATE: ("current_state", None),
        }
        rule = roots.get(effect.domain)
        if rule is None:
            return None
        root, fixed_predicate = rule
        if fixed_predicate is not None:
            predicate = fixed_predicate if parts == [root, fixed_predicate] else None
        else:
            predicate = root if parts == [root] else (parts[1] if len(parts) == 2 and parts[0] == root else None)
        return ({"CHARACTER"}, predicate) if predicate else None


class SceneCommitCognitionBuilder:
    """Deterministic observation-only cognition materialization."""

    def build(self, db: Session, scene: Scene, performance: ScenePerformance, turns: list[ScenePerformanceTurn], resolutions: dict[str, WorldResolution], items: list[StateDeltaItem]) -> tuple[list[CharacterKnowledge], list[CharacterMemory]]:
        knowledge: list[CharacterKnowledge] = []
        memories: list[CharacterMemory] = []
        seen_knowledge: set[tuple[str, str, str, str, str]] = set()
        seen_memory: set[tuple[str, str, str]] = set()
        item_by_resolution: dict[str, list[StateDeltaItem]] = {}
        for item in items:
            item_by_resolution.setdefault(item.source_resolution_id or "", []).append(item)
        matcher = SceneCommitObservedFactMatcher()
        for turn in turns:
            contents = []
            for content in (turn.observable_action, turn.spoken_content):
                if content and content not in contents:
                    contents.append(content)
            for content in contents:
                recipients = {turn.actor_character_id, *(turn.recipient_character_ids or [])}
                for character_id in sorted(recipients):
                    self._memory(memories, seen_memory, character_id, scene.id, f"TURN:{turn.id}", content)
            resolution = resolutions.get(turn.id)
            if not resolution:
                continue
            if resolution.actor_observation:
                self._memory(memories, seen_memory, turn.actor_character_id, scene.id, f"RESOLUTION:{resolution.id}", resolution.actor_observation)
            if resolution.public_observation:
                for character_id in sorted(set(resolution.recipient_character_ids or [])):
                    self._memory(memories, seen_memory, character_id, scene.id, f"RESOLUTION:{resolution.id}", resolution.public_observation)
            for item in sorted(item_by_resolution.get(resolution.id, []), key=lambda row: (row.ordinal, row.id)):
                effect = StateEffectPayload.model_validate(item.evidence["state_effect"])
                fact = matcher.match(resolution, effect)
                if fact is None:
                    continue
                recipients: set[str] = set()
                if resolution.actor_observation:
                    recipients.add(turn.actor_character_id)
                observed = effect.evidence.get("observed_by_character_ids") if isinstance(effect.evidence, dict) else None
                if isinstance(observed, list):
                    recipients.update(set(observed) & set(resolution.recipient_character_ids or []))
                subject_type, subject_id, predicate, value = matcher.identity(fact)
                proposition = f"{subject_type} {subject_id}: {predicate} = {value}"
                for character_id in sorted(recipients):
                    key = (character_id, subject_type, subject_id, predicate, value)
                    if key not in seen_knowledge:
                        seen_knowledge.add(key)
                        knowledge.append(CharacterKnowledge(character_id=character_id, proposition=proposition, status=KnowledgeStatus.KNOWN, source=scene.id, confidence=1.0))
        return knowledge, memories

    def _memory(self, memories: list[CharacterMemory], seen: set[tuple[str, str, str]], character_id: str, scene_id: str, source: str, content: str) -> None:
        key = (character_id, source, content)
        if key not in seen:
            seen.add(key)
            memories.append(CharacterMemory(character_id=character_id, content=content, source_scene=scene_id, importance=0.5, emotional_weight=0.0, confidence=1.0, distortion={}))


class SceneCommitService:
    failure_injector = None
    apply_verifier = None
    reader = FormalWorldStateReader()
    delta_builder = StateDeltaCandidateBuilder()
    delta_validator = StateDeltaValidator()
    delta_translator = WorldResolutionStateDeltaTranslator()
    apply_engine = SceneDeltaApplyEngine()
    cognition_builder = SceneCommitCognitionBuilder()

    def preflight(self, db: Session, project_id: str, performance_id: str) -> CommitPreparation | SceneCommitResult:
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise LookupError("SCENE_COMMIT_PROJECT_NOT_FOUND")
        RetconPendingReplayGuard().assert_progression_allowed(db, project_id)
        performance = db.get(ScenePerformance, performance_id)
        if not performance or performance.project_id != project_id:
            raise LookupError("SCENE_COMMIT_PERFORMANCE_NOT_FOUND")
        existing = db.scalar(select(SceneCommit).where(SceneCommit.project_id == project_id, SceneCommit.performance_id == performance_id))
        if existing and existing.status == SceneCommitStatus.COMMITTED and existing.scene_id:
            scene = db.get(Scene, existing.scene_id)
            checkpoint = db.get(SceneStateCheckpoint, existing.checkpoint_id)
            batches = db.scalars(select(StateDeltaBatch).where(StateDeltaBatch.applied_commit_id == existing.id).order_by(StateDeltaBatch.id)).all()
            if scene and checkpoint:
                return SceneCommitResult(existing, scene, batches, checkpoint, True)
        if existing:
            raise ValueError("SCENE_COMMIT_INVALID_LIFECYCLE")
        allowed_pauses = {"QUIESCENT", "TURN_LIMIT", "INSUFFICIENT_ACTIVE_PARTICIPANTS"}
        if performance.status != PerformanceStatus.RUNNING and not (performance.status == PerformanceStatus.PAUSED and performance.stop_reason in allowed_pauses):
            raise ValueError("SCENE_COMMIT_PERFORMANCE_NOT_READY")
        proposal = db.get(SceneProposal, performance.scene_proposal_id)
        if not proposal or proposal.project_id != project_id:
            raise ValueError("SCENE_COMMIT_EXECUTION_LINEAGE_INVALID")
        if proposal.status == ProposalStatus.EXECUTED:
            raise ValueError("SCENE_PROPOSAL_ALREADY_EXECUTED")
        if proposal.status != ProposalStatus.APPROVED:
            raise ValueError("SCENE_COMMIT_PROPOSAL_NOT_APPROVED")
        if proposal.context_fingerprint != performance.proposal_context_fingerprint:
            raise ValueError("SCENE_COMMIT_CONTEXT_STALE")
        if DirectorContextBuilder().build(db, project_id)["fingerprint"] != proposal.context_fingerprint:
            raise ValueError("SCENE_COMMIT_CONTEXT_STALE")
        turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence, ScenePerformanceTurn.id)).all()
        if not turns:
            raise ValueError("SCENE_COMMIT_EMPTY_PERFORMANCE")
        if [turn.sequence for turn in turns] != list(range(1, len(turns) + 1)) or any(turn.project_id != project_id for turn in turns):
            raise ValueError("SCENE_COMMIT_EXECUTION_LINEAGE_INVALID")
        decisions: list[CharacterDecision] = []
        resolutions: list[WorldResolution] = []
        for turn in turns:
            decision = db.get(CharacterDecision, turn.character_decision_id)
            if not decision or decision.project_id != project_id or decision.scene_proposal_id != proposal.id or decision.character_id != turn.actor_character_id or decision.status != CharacterDecisionStatus.VALID:
                raise ValueError("SCENE_COMMIT_DECISION_INVALID")
            decisions.append(decision)
            matching = db.scalars(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id).order_by(WorldResolution.id)).all()
            if turn.requires_world_resolution:
                if len(matching) != 1 or matching[0].project_id != project_id or matching[0].performance_id != performance.id or matching[0].status != ResolutionStatus.VALID:
                    raise ValueError("SCENE_COMMIT_RESOLUTION_INVALID")
                resolutions.append(matching[0])
            elif matching:
                raise ValueError("SCENE_COMMIT_EXECUTION_LINEAGE_INVALID")
        # A verified current-history head is the prior formal boundary.  This
        # avoids another full snapshot scan on the normal continuous path.
        current_fingerprint, _ = FormalStateIdentityService().current(db, project_id)
        batches: list[StateDeltaBatch] = []
        items: list[StateDeltaItem] = []
        for resolution in resolutions:
            expected_input = StateDeltaInputFingerprintBuilder().build(resolution, next(turn for turn in turns if turn.id == resolution.performance_turn_id), performance, current_fingerprint)
            all_batches = db.scalars(select(StateDeltaBatch).where(StateDeltaBatch.project_id == project_id, StateDeltaBatch.source_resolution_id == resolution.id).order_by(StateDeltaBatch.id)).all()
            current_batches = [batch for batch in all_batches if batch.input_fingerprint == expected_input]
            if not current_batches:
                source_unchanged = any(
                    StateDeltaInputFingerprintBuilder().build(resolution, next(turn for turn in turns if turn.id == resolution.performance_turn_id), performance, batch.base_world_fingerprint) == batch.input_fingerprint
                    for batch in all_batches
                )
                raise ValueError("SCENE_COMMIT_WORLD_STALE" if source_unchanged else "SCENE_COMMIT_SOURCE_CHANGED")
            if len(current_batches) != 1:
                raise ValueError("SCENE_COMMIT_DELTA_NOT_VALIDATED")
            if current_batches[0].status == StateDeltaBatchStatus.APPLIED:
                raise ValueError("SCENE_COMMIT_DELTA_ALREADY_APPLIED")
            if current_batches[0].status != StateDeltaBatchStatus.VALIDATED:
                raise ValueError("SCENE_COMMIT_DELTA_NOT_VALIDATED")
            batch = current_batches[0]
            if batch.base_world_fingerprint != current_fingerprint or batch.validated_world_fingerprint != current_fingerprint:
                raise ValueError("SCENE_COMMIT_WORLD_STALE")
            batches.append(batch)
            batch_items = db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id).order_by(StateDeltaItem.ordinal, StateDeltaItem.id)).all()
            self._verify_items(db, project_id, batch, resolution, batch_items)
            items.extend(batch_items)
        self._verify_combined(db, project_id, items)
        source_fingerprint = stable_fingerprint({
            "project_id": project_id, "proposal_id": proposal.id, "performance_id": performance.id,
            "decision_ids": [decision.id for decision in decisions], "turn_ids": [turn.id for turn in turns],
            "resolution_ids": [resolution.id for resolution in resolutions],
            "batch_input_fingerprints": [batch.input_fingerprint for batch in batches],
            "validation_fingerprints": [batch.validation_fingerprint for batch in batches],
            "pre_world_fingerprint": current_fingerprint,
        }, "scene-commit-source-v1")
        return CommitPreparation(project, performance, proposal, turns, decisions, resolutions, batches, items, current_fingerprint, source_fingerprint)

    def commit(self, db: Session, project_id: str, performance_id: str) -> SceneCommitResult:
        prepared = self.preflight(db, project_id, performance_id)
        if isinstance(prepared, SceneCommitResult):
            return prepared
        db.info["formal_state_sync_in_progress"] = True
        # PRE_SCENE_COMMIT remains the global legacy audit boundary.  The
        # current-history boundary is always the v3 service checkpoint.
        pre_snapshot = SceneCheckpointService().capture_formal_pre(db, project_id)
        mutation_guard = SceneCommitFormalMutationGuard(db)
        record = SceneCommit(project_id=project_id, proposal_id=prepared.proposal.id, performance_id=prepared.performance.id, status=SceneCommitStatus.PENDING, delta_batch_ids=[batch.id for batch in prepared.batches], pre_snapshot_id=pre_snapshot.id, pre_world_fingerprint=prepared.pre_fingerprint, source_fingerprint=prepared.source_fingerprint)
        db.add(record)
        db.flush()
        db.info["scene_commit_mutation_guard"] = mutation_guard
        try:
            self.apply_engine.apply(db, project_id, prepared.items)
        finally:
            db.info.pop("scene_commit_mutation_guard", None)
        if self.apply_verifier:
            self.apply_verifier(db, prepared)
            mutation_guard.observe()
            self.apply_engine.verify(db, project_id, prepared.items)
        sequence = (db.scalar(select(func.max(Scene.sequence)).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE")) or 0) + 1
        resolutions_by_turn = {resolution.performance_turn_id: resolution for resolution in prepared.resolutions}
        facts = [
            fact
            for turn in prepared.turns
            for fact in sorted(
                resolutions_by_turn.get(turn.id).objective_facts if resolutions_by_turn.get(turn.id) else [],
                key=lambda value: stable_fingerprint(value, "scene-fact-v1"),
            )
        ]
        scene = Scene(project_id=project_id, sequence=sequence, world_time=prepared.project.current_world_time, location=prepared.proposal.location_id or prepared.proposal.proposed_location, participants=list(prepared.performance.participant_order or []), intent=prepared.proposal.scene_goal, facts=facts, result={"proposal_id": prepared.proposal.id, "performance_id": prepared.performance.id, "turns": [{"decision_id": turn.character_decision_id, "turn_id": turn.id} for turn in prepared.turns], "resolutions": [{"turn_id": resolution.performance_turn_id, "resolution_id": resolution.id, "outcome": getattr(resolution.outcome, "value", resolution.outcome), "outcome_summary": resolution.outcome_summary} for resolution in prepared.resolutions], "state_delta_batch_ids": [batch.id for batch in prepared.batches], "applied_item_count": len(prepared.items)}, summary=None, story_threads=([prepared.proposal.primary_thread_id] if prepared.proposal.primary_thread_id else []), status=SceneStatus.OCCURRED, history_status="ACTIVE")
        db.add(scene); mutation_guard.observe(); db.flush()
        binding = SceneExecutionBinding(project_id=project_id, scene_id=scene.id, performance_id=prepared.performance.id, replay_session_id=None, active=True)
        db.add(binding)
        knowledge, memories = self.cognition_builder.build(db, scene, prepared.performance, prepared.turns, resolutions_by_turn, prepared.items)
        db.add_all(knowledge + memories)
        # The sibling lookup below may autoflush new cognition, so capture the
        # bounded formal manifest before any query can clear Session.new.
        mutation_guard.observe()
        for batch in prepared.batches:
            batch.status = StateDeltaBatchStatus.APPLIED
            batch.applied_scene_id = scene.id
            batch.applied_commit_id = record.id
            batch.applied_at = datetime.utcnow()
        prepared.performance.status = PerformanceStatus.COMPLETED
        prepared.performance.stop_reason = "SCENE_COMMITTED"
        prepared.proposal.status = ProposalStatus.EXECUTED
        siblings = db.scalars(select(ScenePerformance).where(ScenePerformance.scene_proposal_id == prepared.proposal.id, ScenePerformance.id != prepared.performance.id, ScenePerformance.status.in_([PerformanceStatus.READY, PerformanceStatus.RUNNING, PerformanceStatus.PAUSED, PerformanceStatus.AWAITING_WORLD]))).all()
        for sibling in siblings:
            sibling.status = PerformanceStatus.INVALIDATED
            sibling.stop_reason = "PROPOSAL_EXECUTED"
        mutation_guard.observe()
        db.flush()
        mutation_manifest = mutation_guard.assert_complete(
            project_id=project_id,
            items=prepared.items,
            scene=scene,
            knowledge=knowledge,
            memories=memories,
        )
        checkpoint, post_snapshot = SceneCheckpointService().create_normal_checkpoint(
            db, project_id, scene, pre_snapshot, items=prepared.items, knowledge=knowledge,
            memories=memories, source_scene_commit_id=record.id,
            mutation_manifest=mutation_manifest,
        )
        post_fingerprint = post_snapshot.state_fingerprint
        record.scene_id = scene.id
        record.post_snapshot_id = post_snapshot.id
        record.checkpoint_id = checkpoint.id
        record.post_world_fingerprint = post_fingerprint
        # This is an applied StateDeltaItem count; batch count is retained in delta_batch_ids.
        record.applied_delta_count = len(prepared.items)
        record.created_knowledge_count = len(knowledge)
        record.created_memory_count = len(memories)
        record.completed_at = datetime.utcnow()
        record.status = SceneCommitStatus.COMMITTED
        record.commit_fingerprint = stable_fingerprint({"source_fingerprint": record.source_fingerprint, "sequence": scene.sequence, "item_fingerprints": [item.semantic_fingerprint for item in prepared.items], "pre": record.pre_world_fingerprint, "post": post_fingerprint}, "scene-commit-v1")
        trace = ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.SCENE_COMMIT, source_type="SCENE_PERFORMANCE", source_id=prepared.performance.id, status=ExecutionStatus.SUCCEEDED, input_fingerprint=record.source_fingerprint, output_fingerprint=record.commit_fingerprint)
        db.add(trace)
        db.flush()
        # Phase 8 is a derived audit write in this same transaction.  A ledger
        # failure therefore rolls back formal scene materialization as well.
        CausalLedgerService().sync_after_scene_commit(db, record)
        # The Phase 16A projection is non-authoritative.  Its own service
        # contains a savepoint and marks failures DIRTY, leaving this frozen
        # formal commit and its Phase 8 ledger boundary intact.
        ProjectHistoryProjectionService().sync_after_scene_commit(db, project_id, scene.id)
        # Narrative structure is a rebuildable projection.  Its bounded append
        # path never changes the authority of this committed Scene.
        from .narrative_structure_projection import NarrativeStructureProjectionService
        NarrativeStructureProjectionService().sync_after_scene_commit(db, project_id, scene.id)
        # Phase 16C1 is a rebuildable accelerator. Its failure is contained in
        # a savepoint and can only make the next mind read use legacy recall.
        try:
            with db.begin_nested():
                CognitionRetrievalProjectionService().sync_after_scene_commit(db, project_id, scene.id, scene.sequence)
        except Exception:
            with db.begin_nested():
                CognitionRetrievalProjectionService().mark_dirty(db, project_id, scene.sequence)
        db.flush()
        db.info.pop("formal_state_sync_in_progress", None)
        if self.failure_injector:
            self.failure_injector("AFTER_SCENE_COMMIT_MATERIALIZATION")
        return SceneCommitResult(record, scene, prepared.batches, checkpoint, False)

    def _verify_items(self, db: Session, project_id: str, batch: StateDeltaBatch, resolution: WorldResolution, items: list[StateDeltaItem]) -> None:
        translated, translation_issues = self.delta_translator.translate(resolution)
        if translation_issues:
            raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID")
        for item in items:
            if item.project_id != project_id or item.batch_id != batch.id or item.source_resolution_id != resolution.id or item.source_turn_id != batch.source_turn_id:
                raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID")
            try:
                effect = StateEffectPayload.model_validate((item.evidence or {}).get("state_effect"))
            except Exception as exc:
                raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID") from exc
            if sum(candidate.model_dump(mode="json") == effect.model_dump(mode="json") for candidate in translated) != 1:
                raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID")
            expected_fingerprint = state_delta_item_fingerprint(project_id, resolution.id, batch.source_turn_id, effect, item.before_value, item.after_value, item.evidence)
            if expected_fingerprint != item.semantic_fingerprint:
                raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID")
            try:
                if compute_state_delta_after(item.before_value, True, effect) != item.after_value:
                    raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID")
                current, found = self.reader.before_value(db, project_id, effect)
            except ValueError as exc:
                raise ValueError("SCENE_COMMIT_ITEM_INTEGRITY_INVALID") from exc
            if (found and current != item.before_value) or (not found and effect.operation.name != "UPSERT"):
                raise ValueError("SCENE_COMMIT_BEFORE_STALE")

    def _verify_combined(self, db: Session, project_id: str, items: list[StateDeltaItem]) -> None:
        ordered = sorted(items, key=lambda item: (item.target_type.value, item.target_id, item.path, item.batch_id, item.ordinal, item.id))
        for index, item in enumerate(ordered):
            for other in ordered[index + 1:]:
                if (item.target_type, item.target_id) != (other.target_type, other.target_id):
                    break
                if item.path == other.path or item.path.startswith(other.path + "/") or other.path.startswith(item.path + "/"):
                    raise ValueError("SCENE_COMMIT_CROSS_BATCH_PATH_CONFLICT")
        view = StateDeltaValidationWorldView(db, project_id)
        for item in sorted(items, key=lambda row: (row.source_turn_id or "", row.batch_id, row.ordinal, row.id)):
            if not view.apply(item):
                raise ValueError("SCENE_COMMIT_FINAL_WORLD_INVALID")
        if self.delta_validator.validate_final_overlay(db, view, items):
            raise ValueError("SCENE_COMMIT_FINAL_WORLD_INVALID")
