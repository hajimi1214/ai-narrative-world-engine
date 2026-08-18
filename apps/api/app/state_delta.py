"""Candidate-only formal state delta derivation for Phase 7A."""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
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


DERIVATION_VERSION = "state-delta-v1"
_MISSING = object()


class StateEffectPayload(BaseModel):
    """Explicit structured proof that a runtime consequence changes state."""

    model_config = ConfigDict(extra="forbid")
    effect_kind: Literal["STATE_CHANGE"] = "STATE_CHANGE"
    target_type: StateDeltaTargetType
    target_id: str
    domain: StateDeltaDomain
    operation: StateDeltaOperation
    path: str
    value: Any
    reason: str = Field(min_length=1, max_length=1000)
    evidence: dict[str, Any] = Field(default_factory=dict)


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
        for index, fact in enumerate(resolution.objective_facts or []):
            if not isinstance(fact, dict):
                report.append({"index": index, "code": "UNSUPPORTED_STATE_EFFECT"})
                continue
            raw_effect = fact.get("state_effect")
            explicitly_state_changing = fact.get("effect_kind") == "STATE_CHANGE"
            if raw_effect is None and explicitly_state_changing:
                raw_effect = self._effect_from_state_change_fact(fact)
            if raw_effect is None:
                report.append({"index": index, "code": "UNSUPPORTED_STATE_EFFECT" if explicitly_state_changing else "NO_STATE_CHANGE"})
                continue
            try:
                effect = StateEffectPayload.model_validate(raw_effect)
            except Exception:
                report.append({"index": index, "code": "UNSUPPORTED_STATE_EFFECT"})
                continue
            evidence = dict(effect.evidence)
            evidence["objective_fact"] = {
                "subject_type": fact.get("subject_type"), "subject_id": fact.get("subject_id"),
                "predicate": fact.get("predicate"), "value": fact.get("value"),
            }
            effects.append(effect.model_copy(update={"evidence": evidence}))
        return effects, report

    def _effect_from_state_change_fact(self, fact: dict[str, Any]) -> dict[str, Any] | None:
        subject_type, subject_id, predicate = fact.get("subject_type"), fact.get("subject_id"), fact.get("predicate")
        value = fact.get("value")
        if subject_type in {"ENTITY", "LOCATION"} and isinstance(subject_id, str) and isinstance(predicate, str) and predicate:
            return {"target_type": "WORLD_ENTITY", "target_id": subject_id, "domain": "WORLD_ENTITY_PROFILE", "operation": "SET", "path": f"/profile/{_escape(predicate)}", "value": value, "reason": "validated structured world-resolution state change", "evidence": {"effect_kind": "STATE_CHANGE"}}
        if subject_type == "CHARACTER" and isinstance(subject_id, str) and isinstance(predicate, str):
            for prefix, domain, root in (
                ("current_state.", "CHARACTER_CURRENT_STATE", "/current_state/"),
                ("physical_state.", "CHARACTER_PHYSICAL_STATE", "/physical_state/"),
                ("emotional_state.", "CHARACTER_EMOTIONAL_STATE", "/emotional_state/"),
            ):
                if predicate.startswith(prefix) and len(predicate) > len(prefix):
                    return {"target_type": "CHARACTER", "target_id": subject_id, "domain": domain, "operation": "SET", "path": root + _escape(predicate[len(prefix):]), "value": value, "reason": "validated structured character state change", "evidence": {"effect_kind": "STATE_CHANGE"}}
        return None


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
            raise ValueError("STATE_DELTA_CROSS_PROJECT_REFERENCE")
        proposal = db.get(SceneProposal, performance.scene_proposal_id)
        if proposal and proposal.project_id != project_id:
            raise ValueError("STATE_DELTA_CROSS_PROJECT_REFERENCE")

        _, base_fingerprint = WorldSnapshotBuilder().build(db, project_id)
        source_payload = {
            "id": resolution.id, "status": _enum(resolution.status), "outcome": _enum(resolution.outcome),
            "objective_facts": resolution.objective_facts or [], "turn_id": turn.id,
            "performance_id": performance.id,
        }
        input_fingerprint = stable_fingerprint({"source": source_payload, "base_world_fingerprint": base_fingerprint, "derivation_version": DERIVATION_VERSION}, "state-delta-input-v1")
        existing = db.scalar(select(StateDeltaBatch).where(StateDeltaBatch.project_id == project_id, StateDeltaBatch.input_fingerprint == input_fingerprint, StateDeltaBatch.status == StateDeltaBatchStatus.CANDIDATE))
        if existing:
            return existing, self.items(db, existing.id), True

        effects, report = self.translator.translate(resolution)
        prepared = []
        for effect in effects:
            self._validate_effect(effect)
            before, found = self.reader.before_value(db, project_id, effect)
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
        batch = StateDeltaBatch(
            project_id=project_id, source_type="WORLD_RESOLUTION", source_id=resolution.id,
            source_scene_proposal_id=proposal.id if proposal else None, source_performance_id=performance.id,
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
            semantic_fingerprint = stable_fingerprint({
                "project_id": project_id, "source_resolution_id": resolution.id, "source_turn_id": turn.id,
                "target_type": effect.target_type.value, "target_id": effect.target_id,
                "domain": effect.domain.value, "operation": effect.operation.value, "path": effect.path,
                "before": before, "after": after, "evidence": evidence,
            }, "state-delta-item-v1")
            item = StateDeltaItem(
                project_id=project_id, batch_id=batch.id, ordinal=ordinal, target_type=effect.target_type,
                target_id=effect.target_id, domain=effect.domain, operation=effect.operation, path=effect.path,
                before_value=before, after_value=after, causal_reason=effect.reason,
                source_turn_id=turn.id, source_resolution_id=resolution.id, evidence=evidence,
                semantic_fingerprint=semantic_fingerprint,
            )
            db.add(item); items.append(item)
        db.flush()
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
