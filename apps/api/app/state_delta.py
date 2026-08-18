"""Candidate-only formal state delta derivation for Phase 7A."""
from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .models import (
    Character, Project, ResolutionStatus, ScenePerformance, ScenePerformanceTurn,
    SceneProposal, StateDeltaBatch, StateDeltaBatchStatus, StateDeltaDomain,
    StateDeltaItem, StateDeltaOperation, StateDeltaTargetType, StoryThread,
    WorldEntity, WorldResolution,
)
from .revision import _pointer, _record
from .versioning import WorldSnapshotBuilder
from .state_effect_contract import StateEffectPayload


DERIVATION_VERSION = "state-delta-v1"
_MISSING = object()


class StateDeltaInputFingerprintBuilder:
    """Single canonical input fingerprint contract for derivation and validation."""

    def build(self, resolution: WorldResolution, turn: ScenePerformanceTurn, performance: ScenePerformance, base_world_fingerprint: str) -> str:
        source_payload = {
            "id": resolution.id, "status": _enum(resolution.status), "outcome": _enum(resolution.outcome),
            "objective_facts": resolution.objective_facts or [], "turn_id": turn.id,
            "performance_id": performance.id, "state_effects": resolution.state_effects or [],
            "world_context_fingerprint": resolution.world_context_fingerprint,
        }
        return stable_fingerprint({"source": source_payload, "base_world_fingerprint": base_world_fingerprint, "derivation_version": DERIVATION_VERSION}, "state-delta-input-v1")


def state_delta_item_fingerprint(project_id: str, resolution_id: str, turn_id: str, effect: StateEffectPayload, before: Any, after: Any, evidence: dict[str, Any]) -> str:
    return stable_fingerprint({
        "project_id": project_id, "source_resolution_id": resolution_id, "source_turn_id": turn_id,
        "target_type": effect.target_type.value, "target_id": effect.target_id,
        "domain": effect.domain.value, "operation": effect.operation.value, "path": effect.path,
        "before": before, "after": after, "evidence": evidence,
    }, "state-delta-item-v1")


class FormalWorldStateReader:
    """Reads formal state without any authority to mutate it."""

    MODELS = {
        StateDeltaTargetType.CHARACTER: Character,
        StateDeltaTargetType.WORLD_ENTITY: WorldEntity,
        StateDeltaTargetType.STORY_THREAD: StoryThread,
        StateDeltaTargetType.PROJECT: Project,
    }

    def target(self, db: Session, project_id: str, target_type: StateDeltaTargetType, target_id: str):
        row = db.get(self.MODELS[target_type], target_id)
        if not row:
            raise ValueError("STATE_DELTA_TARGET_NOT_FOUND")
        if (row.id if target_type == StateDeltaTargetType.PROJECT else row.project_id) != project_id:
            raise ValueError("STATE_DELTA_CROSS_PROJECT_REFERENCE")
        return row

    def before_value(self, db: Session, project_id: str, effect: StateEffectPayload) -> tuple[Any, bool]:
        row = self.target(db, project_id, effect.target_type, effect.target_id)
        document = _record(row)
        current: Any = document
        try:
            for part in _pointer(effect.path):
                if isinstance(current, dict) and part in current:
                    current = current[part]
                elif isinstance(current, list) and part.isdigit() and 0 <= int(part) < len(current):
                    current = current[int(part)]
                else:
                    return None, False
        except ValueError as exc:
            raise ValueError("STATE_DELTA_PATH_UNRESOLVED") from exc
        return copy.deepcopy(current), True


class WorldResolutionStateDeltaTranslator:
    """Translates only explicitly state-changing structured facts/effects."""

    def translate(self, resolution: WorldResolution) -> tuple[list[StateEffectPayload], list[dict[str, Any]]]:
        effects: list[StateEffectPayload] = []
        report: list[dict[str, Any]] = []
        for index, raw_effect in enumerate(resolution.state_effects or []):
            try:
                effect = StateEffectPayload.model_validate(raw_effect)
            except ValidationError:
                report.append({"index": index, "code": "UNSUPPORTED_STATE_EFFECT"})
                continue
            fact = next((item for item in resolution.objective_facts or [] if isinstance(item, dict) and item.get("subject_id") == effect.target_id), None)
            evidence = dict(effect.evidence)
            evidence["objective_fact"] = {"subject_type": fact.get("subject_type"), "subject_id": fact.get("subject_id"), "predicate": fact.get("predicate"), "value": fact.get("value")} if fact else None
            effects.append(effect.model_copy(update={"evidence": evidence}))
        return effects, report


class StateDeltaCandidateBuilder:
    """Creates append-only CANDIDATE rows and never applies their effects."""

    reader = FormalWorldStateReader()
    translator = WorldResolutionStateDeltaTranslator()

    def derive(self, db: Session, project_id: str, source_resolution_id: str) -> tuple[StateDeltaBatch, list[StateDeltaItem], bool]:
        resolution = db.get(WorldResolution, source_resolution_id)
        if not resolution:
            raise ValueError("STATE_DELTA_SOURCE_NOT_FOUND")
        if resolution.project_id != project_id:
            raise ValueError("STATE_DELTA_CROSS_PROJECT_REFERENCE")
        if resolution.status == ResolutionStatus.UNRESOLVED:
            raise ValueError("STATE_DELTA_SOURCE_UNRESOLVED")
        if resolution.status != ResolutionStatus.VALID:
            raise ValueError("STATE_DELTA_SOURCE_INVALID")
        performance = db.get(ScenePerformance, resolution.performance_id)
        turn = db.get(ScenePerformanceTurn, resolution.performance_turn_id)
        if not performance or not turn or performance.project_id != project_id or turn.project_id != project_id:
            raise ValueError("STATE_DELTA_SOURCE_LINEAGE_INVALID")
        if resolution.performance_id != performance.id or resolution.performance_turn_id != turn.id or turn.performance_id != performance.id:
            raise ValueError("STATE_DELTA_SOURCE_LINEAGE_INVALID")
        proposal = db.get(SceneProposal, performance.scene_proposal_id)
        if not proposal or proposal.project_id != project_id:
            raise ValueError("STATE_DELTA_SOURCE_LINEAGE_INVALID")

        _, base_fingerprint = WorldSnapshotBuilder().build(db, project_id)
        input_fingerprint = StateDeltaInputFingerprintBuilder().build(resolution, turn, performance, base_fingerprint)
        existing = db.scalar(select(StateDeltaBatch).where(StateDeltaBatch.project_id == project_id, StateDeltaBatch.input_fingerprint == input_fingerprint))
        if existing:
            return existing, self.items(db, existing.id), True

        effects, report = self.translator.translate(resolution)
        if not effects and not report:
            report.append({"code": "NO_STATE_CHANGE"})
        prepared = []
        for effect in effects:
            self._validate_effect(effect)
            before, found = self.reader.before_value(db, project_id, effect)
            self._validate_resolution_scope(resolution, turn, effect)
            if not found and effect.operation != StateDeltaOperation.UPSERT:
                raise ValueError("STATE_DELTA_PATH_UNRESOLVED")
            after = self._after_value(before, found, effect)
            if found and before == after:
                report.append({"target_id": effect.target_id, "path": effect.path, "code": "NO_STATE_CHANGE"})
                continue
            prepared.append((effect, before if found else None, after))

        prepared.sort(key=lambda item: (
            item[0].target_type.value, item[0].target_id, item[0].domain.value,
            item[0].path, item[0].operation.value,
        ))
        try:
            with db.begin_nested():
                batch = StateDeltaBatch(
                    project_id=project_id, source_type="WORLD_RESOLUTION", source_id=resolution.id,
                    source_scene_proposal_id=proposal.id, source_performance_id=performance.id,
                    source_turn_id=turn.id, source_resolution_id=resolution.id,
                    base_world_fingerprint=base_fingerprint, input_fingerprint=input_fingerprint,
                    status=StateDeltaBatchStatus.CANDIDATE, derivation_version=DERIVATION_VERSION,
                    derivation_report={"entries": report, "item_count": len(prepared)},
                )
                db.add(batch); db.flush()
                items = []
                for ordinal, (effect, before, after) in enumerate(prepared, start=1):
                    evidence = {
                        "source_resolution_id": resolution.id, "source_turn_id": turn.id,
                        "state_effect": effect.model_dump(mode="json"), "translator_version": DERIVATION_VERSION,
                        "objective_fact": effect.evidence.get("objective_fact"),
                    }
                    semantic_fingerprint = state_delta_item_fingerprint(project_id, resolution.id, turn.id, effect, before, after, evidence)
                    item = StateDeltaItem(
                        project_id=project_id, batch_id=batch.id, ordinal=ordinal, target_type=effect.target_type,
                        target_id=effect.target_id, domain=effect.domain, operation=effect.operation, path=effect.path,
                        before_value=before, after_value=after, causal_reason=effect.reason,
                        source_turn_id=turn.id, source_resolution_id=resolution.id, evidence=evidence,
                        semantic_fingerprint=semantic_fingerprint,
                    )
                    db.add(item); items.append(item)
                db.flush()
        except IntegrityError:
            existing = db.scalar(select(StateDeltaBatch).where(StateDeltaBatch.project_id == project_id, StateDeltaBatch.input_fingerprint == input_fingerprint))
            if existing:
                return existing, self.items(db, existing.id), True
            raise
        return batch, items, False

    def items(self, db: Session, batch_id: str) -> list[StateDeltaItem]:
        return db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch_id).order_by(StateDeltaItem.ordinal, StateDeltaItem.id)).all()

    def _validate_effect(self, effect: StateEffectPayload) -> None:
        path = effect.path
        rules = {
            StateDeltaDomain.CHARACTER_LOCATION: (StateDeltaTargetType.CHARACTER, "/current_state/location_id", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.CHARACTER_INVENTORY: (StateDeltaTargetType.CHARACTER, "/inventory", {StateDeltaOperation.SET, StateDeltaOperation.ADD, StateDeltaOperation.REMOVE}),
            StateDeltaDomain.CHARACTER_RELATIONSHIP: (StateDeltaTargetType.CHARACTER, "/relationships/", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.CHARACTER_PHYSICAL_STATE: (StateDeltaTargetType.CHARACTER, "/physical_state/", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.CHARACTER_EMOTIONAL_STATE: (StateDeltaTargetType.CHARACTER, "/emotional_state/", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.CHARACTER_CURRENT_STATE: (StateDeltaTargetType.CHARACTER, "/current_state/", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.WORLD_ENTITY_PROFILE: (StateDeltaTargetType.WORLD_ENTITY, "/profile/", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.WORLD_ENTITY_ACTIVE: (StateDeltaTargetType.WORLD_ENTITY, "/active", {StateDeltaOperation.SET}),
            StateDeltaDomain.STORY_THREAD_STATE: (StateDeltaTargetType.STORY_THREAD, "/state/", {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}),
            StateDeltaDomain.STORY_THREAD_STATUS: (StateDeltaTargetType.STORY_THREAD, "/status", {StateDeltaOperation.SET}),
            StateDeltaDomain.STORY_THREAD_PROGRESS: (StateDeltaTargetType.STORY_THREAD, "/progress", {StateDeltaOperation.SET}),
            StateDeltaDomain.WORLD_TIME: (StateDeltaTargetType.PROJECT, "/current_world_time", {StateDeltaOperation.SET}),
        }
        target, prefix, operations = rules[effect.domain]
        if effect.target_type != target or effect.operation not in operations or (path != prefix and not path.startswith(prefix)):
            raise ValueError("UNSUPPORTED_STATE_EFFECT")
        if effect.domain == StateDeltaDomain.CHARACTER_RELATIONSHIP and len(_pointer(path)) < 3:
            raise ValueError("UNSUPPORTED_STATE_EFFECT")

    def _validate_resolution_scope(self, resolution: WorldResolution, turn: ScenePerformanceTurn, effect: StateEffectPayload) -> None:
        facts = [item for item in resolution.objective_facts or [] if isinstance(item, dict)]
        if effect.target_type == StateDeltaTargetType.WORLD_ENTITY:
            if effect.target_id not in set(resolution.world_entity_ids_used or []) and not any(item.get("subject_id") == effect.target_id for item in facts):
                raise ValueError("UNSUPPORTED_STATE_EFFECT")
        if effect.target_type == StateDeltaTargetType.CHARACTER:
            target_id = (turn.world_resolution_request or {}).get("target_character_id")
            if effect.target_id not in {turn.actor_character_id, target_id}:
                raise ValueError("UNSUPPORTED_STATE_EFFECT")

    def _after_value(self, before: Any, found: bool, effect: StateEffectPayload) -> Any:
        if effect.operation in {StateDeltaOperation.SET, StateDeltaOperation.UPSERT}:
            return copy.deepcopy(effect.value)
        if not found or not isinstance(before, list):
            raise ValueError("STATE_DELTA_PATH_UNRESOLVED")
        after = copy.deepcopy(before)
        if effect.operation == StateDeltaOperation.ADD:
            if effect.value not in after:
                after.append(copy.deepcopy(effect.value))
            return after
        if effect.operation == StateDeltaOperation.REMOVE:
            return [item for item in after if item != effect.value]
        raise ValueError("UNSUPPORTED_STATE_EFFECT")


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _enum(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value
