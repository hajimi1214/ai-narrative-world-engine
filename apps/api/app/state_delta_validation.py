"""Deterministic, candidate-only State Delta validation for Phase 7B."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .models import (
    CanonFact, CanonType, Character, EntityType, Project, ResolutionStatus,
    ScenePerformance, ScenePerformanceTurn, SceneProposal, StateDeltaBatch,
    StateDeltaBatchStatus, StateDeltaDomain, StateDeltaItem, StateDeltaTargetType,
    StoryThread, ThreadStatus, WorldEntity, WorldResolution,
)
from .revision import _pointer, _record
from .state_delta import (
    DERIVATION_VERSION, FormalWorldStateReader, StateDeltaCandidateBuilder,
    StateDeltaInputFingerprintBuilder, WorldResolutionStateDeltaTranslator,
    compute_state_delta_after, state_delta_item_fingerprint,
)
from .state_effect_contract import StateEffectPayload
from .versioning import WorldSnapshotBuilder


VALIDATION_VERSION = "state-delta-validation-v1"


@dataclass
class ValidationResult:
    batch: StateDeltaBatch
    report: dict[str, Any]
    idempotent: bool


class StateDeltaValidationWorldView:
    """A deep-copied candidate overlay. It has no ORM mutation authority."""

    def __init__(self, db: Session, project_id: str):
        self.project = _record(db.get(Project, project_id))
        self.characters = {row.id: _record(row) for row in db.scalars(select(Character).where(Character.project_id == project_id)).all()}
        self.entities = {row.id: _record(row) for row in db.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id)).all()}
        self.threads = {row.id: _record(row) for row in db.scalars(select(StoryThread).where(StoryThread.project_id == project_id)).all()}

    def document(self, target_type: StateDeltaTargetType, target_id: str) -> dict[str, Any] | None:
        if target_type == StateDeltaTargetType.PROJECT:
            return self.project if self.project.get("id") == target_id else None
        mapping = {
            StateDeltaTargetType.CHARACTER: self.characters,
            StateDeltaTargetType.WORLD_ENTITY: self.entities,
            StateDeltaTargetType.STORY_THREAD: self.threads,
        }
        return mapping[target_type].get(target_id)

    def apply(self, item: StateDeltaItem) -> bool:
        document = self.document(item.target_type, item.target_id)
        if document is None:
            return False
        parts = _pointer(item.path)
        if not parts:
            return False
        current: Any = document
        for part in parts[:-1]:
            if not isinstance(current, dict):
                return False
            if part not in current:
                if item.operation.name == "UPSERT":
                    current[part] = {}
                else:
                    return False
            current = current[part]
        if not isinstance(current, dict):
            return False
        current[parts[-1]] = copy.deepcopy(item.after_value)
        return True


class StateDeltaValidator:
    reader = FormalWorldStateReader()
    candidate_builder = StateDeltaCandidateBuilder()
    translator = WorldResolutionStateDeltaTranslator()

    def validate(self, db: Session, project_id: str, batch_id: str) -> ValidationResult:
        batch = db.get(StateDeltaBatch, batch_id)
        if not batch or batch.project_id != project_id:
            raise LookupError("STATE_DELTA_BATCH_NOT_FOUND")
        if batch.status in {StateDeltaBatchStatus.VALIDATED, StateDeltaBatchStatus.REJECTED}:
            return ValidationResult(batch, batch.validation_report or {}, True)
        if batch.status == StateDeltaBatchStatus.APPLIED:
            raise ValueError("STATE_DELTA_ALREADY_APPLIED")
        if batch.status != StateDeltaBatchStatus.CANDIDATE:
            raise ValueError("STATE_DELTA_INVALID_LIFECYCLE")

        _, current_fingerprint = WorldSnapshotBuilder().build(db, project_id)
        issues: list[dict[str, Any]] = []
        item_results: list[dict[str, Any]] = []
        resolution, performance, turn, proposal = self._source(db, batch, issues)
        canonical_effects: list[StateEffectPayload] = []
        if resolution:
            canonical_effects, translation_issues = self.translator.translate(resolution)
            if translation_issues:
                for translation_issue in translation_issues:
                    self._issue(issues, translation_issue.get("code", "STATE_DELTA_ITEM_SOURCE_EFFECT_MISMATCH"))
        if resolution and performance and turn and proposal:
            expected_input = StateDeltaInputFingerprintBuilder().build(resolution, turn, performance, batch.base_world_fingerprint)
            if expected_input != batch.input_fingerprint:
                self._issue(issues, "STATE_DELTA_SOURCE_CHANGED")
        if current_fingerprint != batch.base_world_fingerprint:
            self._issue(issues, "STATE_DELTA_BASE_STATE_STALE")

        items = db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id).order_by(StateDeltaItem.ordinal, StateDeltaItem.id)).all()
        self._validate_ordinals(items, issues)
        valid_items: list[tuple[StateDeltaItem, StateEffectPayload]] = []
        for item in items:
            item_issues: list[str] = []
            effect = self._validate_item(db, batch, item, item_issues, canonical_effects)
            for code in item_issues:
                self._issue(issues, code, item)
            if effect and not item_issues:
                valid_items.append((item, effect))
            item_results.append({"item_id": item.id, "ordinal": item.ordinal, "valid": not item_issues, "issues": sorted(item_issues)})

        self._validate_path_conflicts(items, issues)
        view = StateDeltaValidationWorldView(db, project_id)
        for item, _effect in valid_items:
            if not view.apply(item):
                self._issue(issues, "STATE_DELTA_PATH_UNRESOLVED", item)
        for item, effect in valid_items:
            self._validate_domain(db, view, batch, item, effect, issues)
        self._validate_final_world(view, issues)
        self._validate_canon(db, view, valid_items, issues)

        issues = self._sorted_issues(issues)
        report = {
            "valid": not issues,
            "validation_version": VALIDATION_VERSION,
            "basis_world_fingerprint": current_fingerprint,
            "batch_input_fingerprint": batch.input_fingerprint,
            "issues": issues,
            "item_results": sorted(item_results, key=lambda item: (item["ordinal"], item["item_id"])),
            "item_count": len(items),
            "no_state_change": not items,
        }
        report["validation_fingerprint"] = stable_fingerprint({
            "validation_version": VALIDATION_VERSION, "batch_input_fingerprint": batch.input_fingerprint,
            "basis_world_fingerprint": current_fingerprint,
            "item_fingerprints": [item.semantic_fingerprint for item in items], "issues": issues,
        }, "state-delta-validation-v1")
        batch.status = StateDeltaBatchStatus.VALIDATED if report["valid"] else StateDeltaBatchStatus.REJECTED
        batch.validation_version = VALIDATION_VERSION
        batch.validation_report = report
        batch.validation_fingerprint = report["validation_fingerprint"]
        batch.validated_world_fingerprint = current_fingerprint
        batch.validation_completed_at = datetime.utcnow()
        db.add(batch)
        db.flush()
        return ValidationResult(batch, report, False)

    def _source(self, db: Session, batch: StateDeltaBatch, issues: list[dict[str, Any]]):
        if batch.source_type != "WORLD_RESOLUTION" or batch.source_id != batch.source_resolution_id:
            self._issue(issues, "STATE_DELTA_SOURCE_INVALID")
            return None, None, None, None
        resolution = db.get(WorldResolution, batch.source_resolution_id)
        if not resolution or resolution.project_id != batch.project_id or resolution.status != ResolutionStatus.VALID:
            self._issue(issues, "STATE_DELTA_SOURCE_INVALID")
            return None, None, None, None
        performance = db.get(ScenePerformance, resolution.performance_id)
        turn = db.get(ScenePerformanceTurn, resolution.performance_turn_id)
        proposal = db.get(SceneProposal, performance.scene_proposal_id) if performance else None
        if not performance or not turn or not proposal or any(row.project_id != batch.project_id for row in (performance, turn, proposal)) or resolution.performance_id != performance.id or resolution.performance_turn_id != turn.id or turn.performance_id != performance.id or performance.scene_proposal_id != proposal.id:
            self._issue(issues, "STATE_DELTA_SOURCE_LINEAGE_INVALID")
            return resolution, None, None, None
        return resolution, performance, turn, proposal

    def _validate_ordinals(self, items: list[StateDeltaItem], issues: list[dict[str, Any]]) -> None:
        if [item.ordinal for item in items] != list(range(1, len(items) + 1)):
            self._issue(issues, "STATE_DELTA_ORDINAL_INVALID")

    def _validate_item(self, db: Session, batch: StateDeltaBatch, item: StateDeltaItem, codes: list[str], canonical_effects: list[StateEffectPayload]) -> StateEffectPayload | None:
        if item.project_id != batch.project_id or item.batch_id != batch.id or item.source_turn_id != batch.source_turn_id or item.source_resolution_id != batch.source_resolution_id:
            codes.append("STATE_DELTA_ITEM_LINEAGE_INVALID")
        evidence = item.evidence if isinstance(item.evidence, dict) else {}
        required = {"source_resolution_id", "source_turn_id", "state_effect", "translator_version"}
        if not required.issubset(evidence):
            codes.append("STATE_DELTA_ITEM_EVIDENCE_INVALID")
            return None
        try:
            effect = StateEffectPayload.model_validate(evidence["state_effect"])
        except ValidationError:
            codes.append("STATE_DELTA_ITEM_EVIDENCE_INVALID")
            return None
        if evidence["source_resolution_id"] != batch.source_resolution_id or evidence["source_turn_id"] != batch.source_turn_id or evidence["translator_version"] != DERIVATION_VERSION:
            codes.append("STATE_DELTA_ITEM_EVIDENCE_INVALID")
        if (effect.target_type != item.target_type or effect.target_id != item.target_id or effect.domain != item.domain or effect.operation != item.operation or effect.path != item.path or effect.reason != item.causal_reason):
            codes.append("STATE_DELTA_ITEM_EVIDENCE_MISMATCH")
        matches = [candidate for candidate in canonical_effects if candidate.model_dump(mode="json") == effect.model_dump(mode="json")]
        if len(matches) != 1:
            codes.append("STATE_DELTA_ITEM_SOURCE_EFFECT_MISMATCH")
        expected = state_delta_item_fingerprint(batch.project_id, batch.source_resolution_id, batch.source_turn_id, effect, item.before_value, item.after_value, evidence)
        if expected != item.semantic_fingerprint:
            codes.append("STATE_DELTA_ITEM_FINGERPRINT_MISMATCH")
        try:
            expected_after = compute_state_delta_after(item.before_value, True, effect)
            if expected_after != item.after_value:
                codes.append("STATE_DELTA_ITEM_AFTER_MISMATCH")
        except ValueError:
            codes.append("STATE_DELTA_ITEM_AFTER_MISMATCH")
        try:
            self.candidate_builder._validate_effect(effect)
            before, found = self.reader.before_value(db, batch.project_id, effect)
            if (found and before != item.before_value) or (not found and effect.operation.name != "UPSERT"):
                codes.append("STATE_DELTA_BEFORE_VALUE_STALE")
        except ValueError as exc:
            codes.append(str(exc))
        return effect

    def _validate_path_conflicts(self, items: list[StateDeltaItem], issues: list[dict[str, Any]]) -> None:
        ordered = sorted(items, key=lambda item: (item.target_type.value, item.target_id, item.path, item.ordinal))
        for index, item in enumerate(ordered):
            for other in ordered[index + 1:]:
                if (item.target_type, item.target_id) != (other.target_type, other.target_id):
                    break
                if item.path == other.path:
                    self._issue(issues, "STATE_DELTA_DUPLICATE_PATH", item, [other.id])
                elif item.path.startswith(other.path + "/") or other.path.startswith(item.path + "/"):
                    self._issue(issues, "STATE_DELTA_PATH_CONFLICT", item, [other.id])

    def _validate_domain(self, db: Session, view: StateDeltaValidationWorldView, batch: StateDeltaBatch, item: StateDeltaItem, effect: StateEffectPayload, issues: list[dict[str, Any]]) -> None:
        target = view.document(item.target_type, item.target_id)
        if target is None:
            self._issue(issues, "STATE_DELTA_TARGET_NOT_FOUND", item)
            return
        if item.target_type == StateDeltaTargetType.CHARACTER and not target.get("active", False):
            self._issue(issues, "STATE_DELTA_TARGET_INACTIVE", item)
        if item.domain == StateDeltaDomain.WORLD_ENTITY_PROFILE and not target.get("active", False):
            self._issue(issues, "STATE_DELTA_TARGET_INACTIVE", item)
        if item.domain == StateDeltaDomain.CHARACTER_LOCATION:
            self._validate_location(view, item, issues)
        elif item.domain == StateDeltaDomain.CHARACTER_INVENTORY:
            self._validate_inventory(view, item, effect, issues)
        elif item.domain == StateDeltaDomain.CHARACTER_RELATIONSHIP:
            self._validate_relationship(view, item, issues)
        elif item.domain == StateDeltaDomain.CHARACTER_CURRENT_STATE and item.path.endswith("location_id"):
            self._issue(issues, "STATE_DELTA_DOMAIN_PATH_MISMATCH", item)
        elif item.domain == StateDeltaDomain.STORY_THREAD_PROGRESS:
            if isinstance(item.after_value, bool) or not isinstance(item.after_value, (int, float)) or not 0 <= item.after_value <= 1:
                self._issue(issues, "STATE_DELTA_THREAD_PROGRESS_INVALID", item)
            elif isinstance(item.before_value, (int, float)) and item.after_value < item.before_value:
                self._issue(issues, "STATE_DELTA_THREAD_PROGRESS_REGRESSION", item)
        elif item.domain == StateDeltaDomain.STORY_THREAD_STATUS:
            self._validate_thread_status(item, issues)
        elif item.domain == StateDeltaDomain.STORY_THREAD_STATE and not item.path.startswith("/state/"):
            self._issue(issues, "STATE_DELTA_DOMAIN_PATH_MISMATCH", item)
        elif item.domain == StateDeltaDomain.WORLD_TIME:
            self._validate_world_time(item, issues)

    def _validate_location(self, view: StateDeltaValidationWorldView, item: StateDeltaItem, issues: list[dict[str, Any]]) -> None:
        if item.after_value is None:
            return
        entity = view.entities.get(item.after_value)
        if not entity or entity.get("project_id") != item.project_id or not entity.get("active") or _value(entity.get("entity_type")) not in {EntityType.LOCATION.value, EntityType.CITY.value}:
            self._issue(issues, "STATE_DELTA_LOCATION_INVALID", item)

    def _validate_inventory(self, view: StateDeltaValidationWorldView, item: StateDeltaItem, effect: StateEffectPayload, issues: list[dict[str, Any]]) -> None:
        after = item.after_value
        if not isinstance(after, list):
            self._issue(issues, "STATE_DELTA_INVENTORY_ITEM_INVALID", item)
            return
        if effect.operation.name == "REMOVE" and _item_ref(effect.value) not in {_item_ref(value) for value in (item.before_value or [])}:
            self._issue(issues, "STATE_DELTA_INVENTORY_REMOVE_INVALID", item)
        before_ids = {_item_ref(value) for value in (item.before_value or [])}
        for value in after:
            reference = _item_ref(value)
            if reference is None:
                if value not in (item.before_value or []):
                    self._issue(issues, "STATE_DELTA_INVENTORY_ITEM_INVALID", item)
                continue
            entity = view.entities.get(reference)
            if entity is None and value in (item.before_value or []):
                continue
            if not entity or not entity.get("active") or _value(entity.get("entity_type")) != EntityType.ITEM.value:
                self._issue(issues, "STATE_DELTA_INVENTORY_ITEM_INVALID", item)

    def _validate_relationship(self, view: StateDeltaValidationWorldView, item: StateDeltaItem, issues: list[dict[str, Any]]) -> None:
        parts = _pointer(item.path)
        if len(parts) != 3 or parts[0] != "relationships":
            self._issue(issues, "STATE_DELTA_DOMAIN_PATH_MISMATCH", item)
            return
        other_id, field = parts[1], parts[2]
        other = view.characters.get(other_id)
        if not other:
            self._issue(issues, "STATE_DELTA_TARGET_NOT_FOUND", item)
        elif not other.get("active"):
            self._issue(issues, "STATE_DELTA_TARGET_INACTIVE", item)
        if other_id == item.target_id:
            self._issue(issues, "STATE_DELTA_RELATIONSHIP_SELF_REFERENCE", item)
        if field == "trust" and (isinstance(item.after_value, bool) or not isinstance(item.after_value, (int, float)) or not 0 <= item.after_value <= 1):
            self._issue(issues, "STATE_DELTA_RELATIONSHIP_VALUE_INVALID", item)

    def _validate_thread_status(self, item: StateDeltaItem, issues: list[dict[str, Any]]) -> None:
        value = _value(item.after_value)
        if value not in {status.value for status in ThreadStatus}:
            self._issue(issues, "STATE_DELTA_THREAD_STATUS_INVALID", item)
            return
        before = _value(item.before_value)
        if before in {ThreadStatus.RESOLVED.value, ThreadStatus.ABANDONED.value} and value != before:
            self._issue(issues, "STATE_DELTA_THREAD_TERMINAL_REOPEN", item)

    def _validate_world_time(self, item: StateDeltaItem, issues: list[dict[str, Any]]) -> None:
        after = normalize_world_time(item.after_value)
        before = normalize_world_time(item.before_value) if item.before_value else None
        if after is None:
            self._issue(issues, "STATE_DELTA_WORLD_TIME_INVALID", item)
        elif before is not None and after < before:
            self._issue(issues, "STATE_DELTA_WORLD_TIME_REGRESSION", item)

    def _validate_final_world(self, view: StateDeltaValidationWorldView, issues: list[dict[str, Any]]) -> None:
        ownership: dict[str, list[str]] = {}
        for character in view.characters.values():
            if not character.get("active"):
                continue
            location_id = (character.get("current_state") or {}).get("location_id")
            if location_id:
                entity = view.entities.get(location_id)
                if not entity or not entity.get("active"):
                    self._issue(issues, "STATE_DELTA_ENTITY_IN_USE", related_ids=[character["id"], location_id])
            for value in character.get("inventory") or []:
                reference = _item_ref(value)
                if reference and reference in view.entities and _value(view.entities[reference].get("entity_type")) == EntityType.ITEM.value:
                    ownership.setdefault(reference, []).append(character["id"])
        for item_id, owners in ownership.items():
            if len(owners) > 1:
                self._issue(issues, "STATE_DELTA_ITEM_MULTIPLE_OWNERS", related_ids=[item_id, *sorted(owners)])

    def _validate_canon(self, db: Session, view: StateDeltaValidationWorldView, items: list[tuple[StateDeltaItem, StateEffectPayload]], issues: list[dict[str, Any]]) -> None:
        canons = db.scalars(select(CanonFact).where(CanonFact.project_id == view.project["id"])).all()
        for canon in canons:
            if not canon.locked and canon.fact_type not in {CanonType.CORE_CANON, CanonType.SECRET_CANON}:
                continue
            data = canon.data or {}
            target_type = data.get("target_type")
            target_id = data.get("target_id")
            path = data.get("path")
            expected = data.get("value")
            if not (target_type and target_id and path):
                if data.get("subject_type") == "ENTITY" and data.get("subject_id") and data.get("predicate"):
                    target_type, target_id, path, expected = "WORLD_ENTITY", data["subject_id"], f"/profile/{data['predicate']}", data.get("value")
                else:
                    continue
            for item, _effect in items:
                if item.target_type.value == target_type and item.target_id == target_id and item.path == path and item.after_value != expected:
                    self._issue(issues, "STATE_DELTA_CANON_CONFLICT", item, [canon.id, target_id], "Structured Canon conflicts with the candidate world state.")

    def _issue(self, issues: list[dict[str, Any]], code: str, item: StateDeltaItem | None = None, related_ids: list[str] | None = None, message: str | None = None) -> None:
        issues.append({"code": code, "severity": "BLOCKING", "item_id": item.id if item else None, "ordinal": item.ordinal if item else None, "message": message or code.replace("_", " ").title(), "related_ids": sorted(related_ids or []), "details": {}})

    def _sorted_issues(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(issues, key=lambda item: (item["ordinal"] if item["ordinal"] is not None else -1, item["code"], item["related_ids"], item["message"]))


def _item_ref(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("entity_id"), str):
        return value["entity_id"]
    return None


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_world_time(value: Any) -> datetime | None:
    """Canonical UTC-naive world time used for all validation comparisons."""
    parsed = _time(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
