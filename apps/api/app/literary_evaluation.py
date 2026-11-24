"""Human-centered evaluation helpers for long-form Chinese fiction.

This module intentionally stays outside the production quality gate. It
summarizes frozen experiment records and never changes a chapter, world state,
Critic decision, or adoption status.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import combinations
import json
from math import isfinite, sqrt
from pathlib import Path
import random
import re
from statistics import mean
from typing import Any, Iterable


DIMENSIONS: tuple[str, ...] = (
    "character_credibility",
    "character_growth",
    "motivation",
    "character_agency",
    "relationship_dynamics",
    "character_voice_differentiation",
    "plot_engagement",
    "causal_coherence",
    "conflict",
    "suspense",
    "plot_progression",
    "turning_point_impact",
    "foreshadowing_payoff",
    "emotion",
    "pacing",
    "chapter_structure",
    "style_naturalness",
    "description_quality",
    "dialogue_quality",
    "continuity",
    "reading_willingness",
)

DIMENSION_GROUPS: dict[str, tuple[str, ...]] = {
    "CHARACTER_CRAFT": (
        "character_credibility", "character_growth", "motivation", "character_agency",
        "relationship_dynamics", "character_voice_differentiation",
    ),
    "PLOT_AND_CAUSALITY": (
        "plot_engagement", "causal_coherence", "conflict", "suspense", "plot_progression",
        "turning_point_impact", "foreshadowing_payoff",
    ),
    "STORY_FACTS_AND_CONTINUITY": ("continuity",),
    "LITERARY_EXPRESSION": (
        "emotion", "pacing", "chapter_structure", "style_naturalness",
        "description_quality", "dialogue_quality",
    ),
    "READER_EXPERIENCE": ("reading_willingness",),
}

REQUIRED_EXPERIMENT_CONDITIONS = {"A_BASELINE", "B_MEMORY", "C_ENGINE"}
RECORDED_OUTPUT_STATUSES = {"COMPLETED", "FAILED", "BLOCKED", "INTERRUPTED", "NOT_STARTED"}
MINIMUM_RATERS_PER_TEXT = 2
MINIMUM_TASKS_FOR_EXPLORATORY_BOOTSTRAP = 4
MINIMUM_ITEMS_FOR_DESCRIPTIVE_RATER_AGREEMENT = 5


@dataclass(frozen=True)
class RatingValidationIssue:
    code: str
    field: str
    message: str


@dataclass(frozen=True)
class RatingSummary:
    condition: str
    dimension: str
    count: int
    independent_task_count: int
    mean: float | None
    standard_error: float | None
    missing_count: int


def _numeric(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5


def _nonnegative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value)) and value >= 0


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_experiment_record(record: dict[str, Any]) -> list[dict[str, str]]:
    """Check that a frozen comparison records its full design and every run outcome."""
    issues: list[dict[str, str]] = []
    if not isinstance(record.get("experiment_id"), str) or not record["experiment_id"].strip():
        issues.append({"code": "EXPERIMENT_ID_INVALID", "field": "experiment_id"})
    system_revision = record.get("system_revision")
    if not isinstance(system_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", system_revision):
        issues.append({"code": "EXPERIMENT_SYSTEM_REVISION_INVALID", "field": "system_revision"})
    if not isinstance(record.get("corpus_manifest"), str) or not record["corpus_manifest"].strip():
        issues.append({"code": "EXPERIMENT_CORPUS_MANIFEST_INVALID", "field": "corpus_manifest"})
    corpus_hash = record.get("corpus_manifest_sha256")
    if not isinstance(corpus_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", corpus_hash):
        issues.append({"code": "EXPERIMENT_CORPUS_HASH_INVALID", "field": "corpus_manifest_sha256"})
    conditions = record.get("conditions") or []
    if not isinstance(conditions, list):
        issues.append({"code": "EXPERIMENT_CONDITIONS_INVALID", "field": "conditions"})
        conditions = []
    condition_ids = [item if isinstance(item, str) else item.get("id") for item in conditions if isinstance(item, (str, dict))]
    valid_condition_ids = [item for item in condition_ids if isinstance(item, str) and item]
    if len(valid_condition_ids) != len(condition_ids):
        issues.append({"code": "EXPERIMENT_CONDITION_ID_INVALID", "field": "conditions"})
    if set(valid_condition_ids) != REQUIRED_EXPERIMENT_CONDITIONS:
        issues.append({"code": "EXPERIMENT_CONDITIONS_INCOMPLETE", "field": "conditions"})
    expected_order = ["A_BASELINE", "B_MEMORY", "C_ENGINE"]
    if valid_condition_ids != expected_order:
        issues.append({"code": "EXPERIMENT_CONDITION_ORDER_INVALID", "field": "conditions"})
    if len(valid_condition_ids) != len(set(valid_condition_ids)):
        issues.append({"code": "EXPERIMENT_CONDITIONS_DUPLICATE", "field": "conditions"})
    expected_treatments = {
        "A_BASELINE": {"memory": False, "formal_world": False, "critic": False},
        "B_MEMORY": {"memory": True, "formal_world": False, "critic": False},
        "C_ENGINE": {"memory": True, "formal_world": True, "critic": True},
    }
    for condition in conditions:
        if not isinstance(condition, dict) or not isinstance(condition.get("id"), str) or condition["id"] not in expected_treatments:
            issues.append({"code": "EXPERIMENT_CONDITION_CONTRACT_INVALID", "field": "conditions"})
            continue
        expected = expected_treatments[condition["id"]]
        if any(condition.get(key) is not value for key, value in expected.items()):
            issues.append({"code": "EXPERIMENT_CONDITION_TREATMENT_INVALID", "field": f"conditions.{condition['id']}"})
    tasks = record.get("tasks") or []
    if not isinstance(tasks, list):
        issues.append({"code": "EXPERIMENT_TASKS_INVALID", "field": "tasks"})
        tasks = []
    task_ids = [item.get("id") for item in tasks if isinstance(item, dict)]
    valid_task_ids = [item for item in task_ids if isinstance(item, str) and item]
    if not valid_task_ids or len(valid_task_ids) != len(tasks):
        issues.append({"code": "EXPERIMENT_TASKS_INVALID", "field": "tasks"})
    if len(valid_task_ids) != len(set(valid_task_ids)):
        issues.append({"code": "EXPERIMENT_TASKS_DUPLICATE", "field": "tasks"})
    analysis = record.get("analysis") or {}
    if not isinstance(analysis, dict) or not isinstance(analysis.get("minimum_raters_per_text"), int) or isinstance(analysis.get("minimum_raters_per_text"), bool) or analysis["minimum_raters_per_text"] < MINIMUM_RATERS_PER_TEXT:
        issues.append({"code": "EXPERIMENT_MINIMUM_RATERS_INVALID", "field": "analysis.minimum_raters_per_text"})

    generation = record.get("generation") or {}
    if not isinstance(generation, dict):
        issues.append({"code": "EXPERIMENT_GENERATION_INVALID", "field": "generation"})
        generation = {}
    for field in ("model_provider", "model_name", "prompt_fingerprint", "failure_and_retry_policy"):
        if not isinstance(generation.get(field), str) or not generation[field].strip():
            issues.append({"code": "EXPERIMENT_GENERATION_FIELD_MISSING", "field": f"generation.{field}"})
    sampling = generation.get("sampling")
    if not isinstance(sampling, dict) or any(key not in sampling for key in ("temperature", "top_p", "seed")):
        issues.append({"code": "EXPERIMENT_SAMPLING_INVALID", "field": "generation.sampling"})
    elif (sampling["temperature"] is not None and (not _nonnegative_number(sampling["temperature"]))) or (sampling["top_p"] is not None and (not _nonnegative_number(sampling["top_p"]) or sampling["top_p"] > 1)) or (sampling["seed"] is not None and (not isinstance(sampling["seed"], int) or isinstance(sampling["seed"], bool))):
        issues.append({"code": "EXPERIMENT_SAMPLING_INVALID", "field": "generation.sampling"})
    budget = generation.get("per_condition_token_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        issues.append({"code": "EXPERIMENT_BUDGET_INVALID", "field": "generation.per_condition_token_budget"})
    condition_prompts = generation.get("condition_prompt_fingerprints")
    if not isinstance(condition_prompts, dict) or set(condition_prompts) != set(valid_condition_ids) or any(not isinstance(value, str) or not value.strip() for value in (condition_prompts or {}).values()):
        issues.append({"code": "EXPERIMENT_CONDITION_PROMPT_FINGERPRINTS_INVALID", "field": "generation.condition_prompt_fingerprints"})
    condition_prompts = condition_prompts if isinstance(condition_prompts, dict) else {}
    condition_models = generation.get("condition_model_names")
    if not isinstance(condition_models, dict) or set(condition_models) != set(valid_condition_ids) or any(not isinstance(value, str) or not value.strip() for value in (condition_models or {}).values()):
        issues.append({"code": "EXPERIMENT_CONDITION_MODELS_INVALID", "field": "generation.condition_model_names"})
    elif any(name != generation.get("model_name") for name in condition_models.values()):
        issues.append({"code": "EXPERIMENT_CONDITION_MODELS_MUST_MATCH", "field": "generation.condition_model_names"})
    condition_models = condition_models if isinstance(condition_models, dict) else {}
    implementation_fingerprints = generation.get("condition_implementation_fingerprints")
    if not isinstance(implementation_fingerprints, dict) or set(implementation_fingerprints) != set(valid_condition_ids) or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in (implementation_fingerprints or {}).values()):
        issues.append({"code": "EXPERIMENT_CONDITION_IMPLEMENTATIONS_INVALID", "field": "generation.condition_implementation_fingerprints"})

    outputs = record.get("outputs") or []
    if not isinstance(outputs, list):
        issues.append({"code": "EXPERIMENT_OUTPUTS_INVALID", "field": "outputs"})
        outputs = []
    output_keys: set[tuple[Any, Any]] = set()
    item_by_task: dict[str, str] = {}
    task_by_item: dict[str, str] = {}
    for index, output in enumerate(outputs):
        field = f"outputs.{index}"
        if not isinstance(output, dict):
            issues.append({"code": "EXPERIMENT_OUTPUT_INVALID", "field": field})
            continue
        task_id = output.get("task_id")
        condition = output.get("condition")
        if task_id not in valid_task_ids or condition not in valid_condition_ids:
            issues.append({"code": "EXPERIMENT_OUTPUT_REFERENCE_INVALID", "field": field})
        item_id = output.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            issues.append({"code": "EXPERIMENT_ITEM_ID_INVALID", "field": f"{field}.item_id"})
        elif isinstance(task_id, str):
            previous_item = item_by_task.setdefault(task_id, item_id)
            if previous_item != item_id:
                issues.append({"code": "EXPERIMENT_PAIRED_ITEM_ID_MISMATCH", "field": f"{field}.item_id"})
            if task_by_item.setdefault(item_id, task_id) != task_id:
                issues.append({"code": "EXPERIMENT_ITEM_ID_REUSED", "field": f"{field}.item_id"})
        if isinstance(task_id, str) and isinstance(condition, str):
            key = (task_id, condition)
            if key in output_keys:
                issues.append({"code": "EXPERIMENT_OUTPUT_DUPLICATE", "field": field})
            output_keys.add(key)
        status = output.get("status")
        if not isinstance(status, str) or status not in RECORDED_OUTPUT_STATUSES:
            issues.append({"code": "EXPERIMENT_OUTPUT_STATUS_INVALID", "field": f"{field}.status"})
        if not isinstance(output.get("attempts"), int) or isinstance(output.get("attempts"), bool) or output["attempts"] < 0:
            issues.append({"code": "EXPERIMENT_ATTEMPTS_INVALID", "field": f"{field}.attempts"})
        for metric in ("confirmed_tokens", "unknown_usage_calls", "reserved_tokens", "elapsed_seconds"):
            value = output.get(metric)
            if metric == "confirmed_tokens" and value is None:
                continue
            if metric in {"confirmed_tokens", "unknown_usage_calls", "reserved_tokens"} and not _nonnegative_integer(value):
                issues.append({"code": "EXPERIMENT_METRIC_INVALID", "field": f"{field}.{metric}"})
            elif metric == "elapsed_seconds" and not _nonnegative_number(value):
                issues.append({"code": "EXPERIMENT_METRIC_INVALID", "field": f"{field}.{metric}"})
        if isinstance(condition, str) and condition in valid_condition_ids:
            if output.get("model_name") != (condition_models or {}).get(condition):
                issues.append({"code": "EXPERIMENT_OUTPUT_MODEL_MISMATCH", "field": f"{field}.model_name"})
            if output.get("prompt_fingerprint") != (condition_prompts or {}).get(condition):
                issues.append({"code": "EXPERIMENT_OUTPUT_PROMPT_MISMATCH", "field": f"{field}.prompt_fingerprint"})
        if status == "COMPLETED":
            if not isinstance(output.get("text_path"), str) or not output["text_path"].strip():
                issues.append({"code": "EXPERIMENT_COMPLETED_TEXT_MISSING", "field": f"{field}.text_path"})
            if not isinstance(output.get("text_sha256"), str) or len(output["text_sha256"]) != 64:
                issues.append({"code": "EXPERIMENT_TEXT_HASH_MISSING", "field": f"{field}.text_sha256"})
            elif not re.fullmatch(r"[0-9a-f]{64}", output["text_sha256"]):
                issues.append({"code": "EXPERIMENT_TEXT_HASH_INVALID", "field": f"{field}.text_sha256"})
            if not _nonnegative_integer(output.get("output_chars")) or output["output_chars"] == 0:
                issues.append({"code": "EXPERIMENT_OUTPUT_LENGTH_INVALID", "field": f"{field}.output_chars"})

    expected = {(task_id, condition) for task_id in valid_task_ids for condition in valid_condition_ids}
    for task_id, condition in sorted(expected - output_keys, key=lambda item: (str(item[0]), str(item[1]))):
        issues.append({"code": "EXPERIMENT_OUTPUT_OUTCOME_MISSING", "field": f"outputs[{task_id},{condition}]"})
    return issues


def experiment_fingerprint(record: dict[str, Any]) -> str:
    if not isinstance(record, dict):
        raise ValueError("EXPERIMENT_RECORD_INVALID")
    raw_outputs = record.get("outputs") or []
    outputs = raw_outputs if isinstance(raw_outputs, list) else []
    design = {
        "schema_version": record.get("schema_version"),
        "experiment_id": record.get("experiment_id"),
        "system_revision": record.get("system_revision"),
        "corpus_manifest": record.get("corpus_manifest"),
        "corpus_manifest_sha256": record.get("corpus_manifest_sha256"),
        "conditions": record.get("conditions"),
        "tasks": record.get("tasks"),
        "generation": record.get("generation"),
        "analysis": record.get("analysis"),
        "outputs": [
            {key: row.get(key) for key in (
                "item_id", "task_id", "condition", "status", "text_sha256", "attempts",
                "model_name", "prompt_fingerprint", "output_chars", "confirmed_tokens",
                "unknown_usage_calls", "reserved_tokens", "elapsed_seconds",
            )}
            if isinstance(row, dict) else {"invalid_output_type": type(row).__name__}
            for row in outputs
        ],
    }
    canonical = json.dumps(design, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_experiment_files(record: dict[str, Any], record_directory: Path) -> list[dict[str, str]]:
    """Verify the frozen corpus manifest and each completed text against recorded hashes."""
    if str(record.get("status") or "").upper() not in {"FROZEN", "COMPLETE", "COMPLETED_WITH_FAILURES"}:
        return []
    issues: list[dict[str, str]] = []
    root = record_directory.resolve()
    manifest_ref = record.get("corpus_manifest")
    manifest_hash = record.get("corpus_manifest_sha256")
    if isinstance(manifest_ref, str) and manifest_ref:
        manifest_path = (root / manifest_ref).resolve()
        if not manifest_path.is_relative_to(root):
            issues.append({"code": "CORPUS_MANIFEST_PATH_OUTSIDE_RECORD_DIRECTORY", "field": "corpus_manifest"})
        elif not manifest_path.is_file():
            issues.append({"code": "CORPUS_MANIFEST_NOT_FOUND", "field": "corpus_manifest"})
        elif hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_hash:
            issues.append({"code": "CORPUS_MANIFEST_HASH_MISMATCH", "field": "corpus_manifest_sha256"})
    outputs = record.get("outputs") if isinstance(record.get("outputs"), list) else []
    for index, row in enumerate(outputs):
        if not isinstance(row, dict) or row.get("status") != "COMPLETED":
            continue
        text_ref = row.get("text_path")
        field = f"outputs.{index}.text_path"
        if not isinstance(text_ref, str) or not text_ref:
            issues.append({"code": "COMPLETED_OUTPUT_TEXT_MISSING", "field": field})
            continue
        text_path = (root / text_ref).resolve()
        if not text_path.is_relative_to(root):
            issues.append({"code": "OUTPUT_TEXT_PATH_OUTSIDE_RECORD_DIRECTORY", "field": field})
            continue
        if not text_path.is_file():
            issues.append({"code": "COMPLETED_OUTPUT_TEXT_NOT_FOUND", "field": field})
            continue
        data = text_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row.get("text_sha256"):
            issues.append({"code": "COMPLETED_OUTPUT_HASH_MISMATCH", "field": f"outputs.{index}.text_sha256"})
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            issues.append({"code": "COMPLETED_OUTPUT_ENCODING_INVALID", "field": field})
            continue
        if not text.strip() or len(text) != row.get("output_chars"):
            issues.append({"code": "COMPLETED_OUTPUT_LENGTH_MISMATCH", "field": f"outputs.{index}.output_chars"})
    return issues


def validate_rating(rating: dict[str, Any]) -> list[RatingValidationIssue]:
    """Validate one blind-reader rating without accepting hidden model scores."""
    if not isinstance(rating, dict):
        return [RatingValidationIssue("RATING_RECORD_INVALID", "rating", "rating must be an object")]
    issues: list[RatingValidationIssue] = []
    required = {"item_id", "condition", "task_id", "rater_id", "scores", "evidence", "confidence"}
    for field in sorted(required - set(rating)):
        issues.append(RatingValidationIssue("RATING_FIELD_MISSING", field, "required rating field is missing"))
    scores = rating.get("scores")
    if not isinstance(scores, dict):
        return issues + [RatingValidationIssue("RATING_SCORES_INVALID", "scores", "scores must be an object")]
    abstentions = rating.get("score_abstentions", {})
    if not isinstance(abstentions, dict):
        issues.append(RatingValidationIssue("RATING_ABSTENTIONS_INVALID", "score_abstentions", "score abstentions must be an object"))
        abstentions = {}
    unknown = sorted(set(scores) - set(DIMENSIONS))
    issues.extend(RatingValidationIssue("RATING_DIMENSION_UNKNOWN", key, "unknown evaluation dimension") for key in unknown)
    missing = sorted(set(DIMENSIONS) - set(scores))
    issues.extend(RatingValidationIssue("RATING_DIMENSION_MISSING", key, "every dimension needs a score or explicit abstention") for key in missing)
    issues.extend(RatingValidationIssue("RATING_ABSTENTION_DIMENSION_UNKNOWN", key, "unknown evaluation dimension") for key in sorted(set(abstentions) - set(DIMENSIONS)))
    for dimension, value in scores.items():
        reason = abstentions.get(dimension)
        if dimension in DIMENSIONS and _numeric(value) and reason:
            issues.append(RatingValidationIssue("RATING_SCORE_AND_ABSTENTION_CONFLICT", dimension, "a rated dimension cannot also be abstained"))
        elif dimension in DIMENSIONS and value is None and isinstance(reason, str) and reason.strip():
            continue
        elif dimension in DIMENSIONS and not _numeric(value):
            issues.append(RatingValidationIssue("RATING_SCORE_INVALID", dimension, "score must be an integer from 1 to 5"))
    evidence = rating.get("evidence")
    if not isinstance(evidence, dict):
        issues.append(RatingValidationIssue("RATING_EVIDENCE_INVALID", "evidence", "evidence must be an object"))
    else:
        for dimension in DIMENSIONS:
            if scores.get(dimension) is None and isinstance(abstentions.get(dimension), str) and abstentions[dimension].strip():
                continue
            if not isinstance(evidence.get(dimension), str) or not evidence[dimension].strip():
                issues.append(RatingValidationIssue("RATING_EVIDENCE_MISSING", dimension, "each score needs a short text-grounded reason"))
    confidence = rating.get("confidence")
    if not _numeric(confidence):
        issues.append(RatingValidationIssue("RATING_CONFIDENCE_INVALID", "confidence", "confidence must be an integer from 1 to 5"))
    for field in (
        "model", "model_id", "model_name", "provider", "prompt", "prompt_fingerprint",
        "raw_score", "critic_score", "critic_decision", "run_id", "token_usage", "system_context",
    ):
        if field in rating:
            issues.append(RatingValidationIssue("RATING_MODEL_METADATA_FORBIDDEN", field, "blind rating cannot contain model or prompt metadata"))
    for field in ("item_id", "condition", "task_id", "rater_id"):
        if field in rating and (not isinstance(rating[field], str) or not rating[field].strip()):
            issues.append(RatingValidationIssue("RATING_IDENTIFIER_INVALID", field, "identifier must be a non-empty string"))
    revision_minutes = rating.get("revision_burden_minutes")
    if revision_minutes is not None and not _nonnegative_number(revision_minutes):
        issues.append(RatingValidationIssue("RATING_REVISION_BURDEN_INVALID", "revision_burden_minutes", "revision burden must be a non-negative finite number"))
    for field in ("next_chapter_willing", "felt_ai_like"):
        if field in rating and rating[field] is not None and not isinstance(rating[field], bool):
            issues.append(RatingValidationIssue("RATING_READER_OUTCOME_INVALID", field, "reader outcome must be boolean or null"))
    return issues


def validate_blind_rating(rating: dict[str, Any]) -> list[RatingValidationIssue]:
    """Validate a rater-facing response that contains no experiment condition."""
    if not isinstance(rating, dict):
        return [RatingValidationIssue("BLIND_RATING_RECORD_INVALID", "rating", "blind rating must be an object")]
    issues: list[RatingValidationIssue] = []
    required = {"schema_version", "anonymous_text_id", "rater_id", "scores", "evidence", "confidence"}
    for field in sorted(required - set(rating)):
        issues.append(RatingValidationIssue("BLIND_RATING_FIELD_MISSING", field, "required blind-rating field is missing"))
    forbidden = {"condition", "item_id", "task_id", "model", "prompt", "raw_score", "critic_score"}
    for field in sorted(forbidden.intersection(rating)):
        issues.append(RatingValidationIssue("BLIND_RATING_METADATA_FORBIDDEN", field, "blind packet cannot expose experiment metadata"))
    allowed = required | {
        "score_abstentions", "next_chapter_willing", "felt_ai_like",
        "revision_burden_minutes", "notes",
    }
    for field in sorted(set(rating) - allowed):
        issues.append(RatingValidationIssue("BLIND_RATING_FIELD_UNKNOWN", field, "blind packet contains an unapproved field"))
    if rating.get("schema_version") != "blind-reader-rating-v2":
        issues.append(RatingValidationIssue("BLIND_RATING_SCHEMA_UNSUPPORTED", "schema_version", "expected blind-reader-rating-v2"))
    anonymous_id = rating.get("anonymous_text_id")
    if not isinstance(anonymous_id, str) or not anonymous_id.strip():
        issues.append(RatingValidationIssue("BLIND_RATING_ID_INVALID", "anonymous_text_id", "anonymous text ID must be a non-empty string"))
    canonical = {
        "item_id": "blind-validation-item",
        "condition": "BLIND",
        "task_id": "blind-validation-task",
        "rater_id": rating.get("rater_id"),
        "scores": rating.get("scores"),
        "evidence": rating.get("evidence"),
        "score_abstentions": rating.get("score_abstentions", {}),
        "confidence": rating.get("confidence"),
        "revision_burden_minutes": rating.get("revision_burden_minutes"),
        "next_chapter_willing": rating.get("next_chapter_willing"),
        "felt_ai_like": rating.get("felt_ai_like"),
    }
    issues.extend(validate_rating(canonical))
    return issues


def prepare_blind_assignments(
    outputs: Iterable[dict[str, Any]],
    rater_ids: Iterable[str],
    *,
    seed: int,
) -> dict[str, Any]:
    """Create deterministic anonymized labels and independent rater orders."""
    rows = list(outputs)
    raters = list(rater_ids)
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("BLIND_OUTPUT_METADATA_INVALID")
        item_id, condition, task_id = row.get("item_id"), row.get("condition"), row.get("task_id")
        if not all(isinstance(value, str) and value.strip() for value in (item_id, condition, task_id)):
            raise ValueError("BLIND_OUTPUT_METADATA_INVALID")
        key = (item_id, condition)
        if key in keys:
            raise ValueError("BLIND_OUTPUT_DUPLICATE")
        keys.add(key)
    if not rows or len(raters) < MINIMUM_RATERS_PER_TEXT or any(not isinstance(rater, str) or not rater.strip() for rater in raters):
        raise ValueError("BLIND_ASSIGNMENT_INPUT_INVALID")
    if len(set(raters)) != len(raters):
        raise ValueError("BLIND_RATER_DUPLICATE")
    randomizer = random.Random(seed)
    shuffled = list(rows)
    randomizer.shuffle(shuffled)
    anonymous_ids = {id(row): f"blind-{index + 1:04d}" for index, row in enumerate(shuffled)}
    assignments = []
    for rater_index, rater in enumerate(raters):
        order = [anonymous_ids[id(row)] for row in shuffled]
        random.Random(seed + rater_index + 1).shuffle(order)
        assignments.append({"rater_id": rater, "order": order})
    return {
        "assignments": assignments,
        "unblinding_map": [
            {"anonymous_text_id": anonymous_ids[id(row)], "item_id": row["item_id"], "condition": row["condition"], "task_id": row["task_id"]}
            for row in shuffled
        ],
    }


def unblind_ratings(
    blind_ratings: Iterable[dict[str, Any]],
    unblinding_map: Iterable[dict[str, Any]],
    assignments: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    mapping_rows = list(unblinding_map)
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("anonymous_text_id"), str)
        or not row["anonymous_text_id"].strip()
        or any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("item_id", "condition", "task_id"))
        for row in mapping_rows
    ):
        raise ValueError("UNBLINDING_MAP_INVALID")
    mapping = {row.get("anonymous_text_id"): row for row in mapping_rows}
    if len(mapping) != len(mapping_rows) or None in mapping:
        raise ValueError("UNBLINDING_MAP_INVALID")
    if assignments is not None:
        if any(not isinstance(assignment, dict) or not isinstance(assignment.get("order"), list) for assignment in assignments):
            raise ValueError("BLIND_ASSIGNMENT_INVALID")
        if any(
            not isinstance(assignment.get("rater_id"), str)
            or not assignment["rater_id"].strip()
            or any(not isinstance(anonymous_id, str) or not anonymous_id.strip() for anonymous_id in assignment["order"])
            for assignment in assignments
        ):
            raise ValueError("BLIND_ASSIGNMENT_INVALID")
        expected_text_ids = {
            anonymous_id
            for assignment in assignments
            for anonymous_id in (assignment.get("order") or [])
        }
        if set(mapping) != expected_text_ids:
            raise ValueError("UNBLINDING_MAP_ASSIGNMENT_MISMATCH")
    canonical_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    expected_pairs = None
    if assignments is not None:
        expected_pairs = {
            (assignment.get("rater_id"), anonymous_id)
            for assignment in assignments
            for anonymous_id in (assignment.get("order") or [])
        }
    for rating in blind_ratings:
        issues = validate_blind_rating(rating)
        if issues:
            raise ValueError("BLIND_RATING_INVALID")
        identity = mapping.get(rating["anonymous_text_id"])
        if not identity or not all(isinstance(identity.get(key), str) and identity[key] for key in ("item_id", "condition", "task_id")):
            raise ValueError("UNBLINDING_MAP_ENTRY_MISSING")
        if expected_pairs is not None and (rating["rater_id"], rating["anonymous_text_id"]) not in expected_pairs:
            raise ValueError("BLIND_RATING_NOT_ASSIGNED")
        key = (identity["item_id"], identity["condition"], rating["rater_id"])
        if key in seen:
            raise ValueError("DUPLICATE_READER_RATING")
        seen.add(key)
        canonical_rows.append({
            **{field: rating[field] for field in ("rater_id", "scores", "evidence", "confidence")},
            "item_id": identity["item_id"], "condition": identity["condition"], "task_id": identity["task_id"],
            "score_abstentions": rating.get("score_abstentions", {}),
            **{field: rating[field] for field in ("next_chapter_willing", "felt_ai_like", "revision_burden_minutes", "notes") if field in rating},
        })
    return canonical_rows


def summarize_ratings(ratings: Iterable[dict[str, Any]]) -> list[RatingSummary]:
    rows = list(ratings)
    conditions = sorted({str(row.get("condition") or "") for row in rows})
    summaries: list[RatingSummary] = []
    for condition in conditions:
        condition_rows = [row for row in rows if str(row.get("condition") or "") == condition]
        for dimension in DIMENSIONS:
            values_by_task: dict[str, list[int]] = {}
            for row in condition_rows:
                value = row.get("scores", {}).get(dimension)
                task_id = row.get("task_id")
                if _numeric(value) and isinstance(task_id, str) and task_id:
                    values_by_task.setdefault(task_id, []).append(value)
            task_means = [mean(values) for _, values in sorted(values_by_task.items())]
            count = sum(len(values) for values in values_by_task.values())
            missing = len(condition_rows) - count
            average = mean(task_means) if task_means else None
            standard_error = None
            if len(task_means) > 1:
                variance = sum((value - average) ** 2 for value in task_means) / (len(task_means) - 1)
                standard_error = sqrt(variance / len(task_means))
            summaries.append(RatingSummary(condition, dimension, count, len(task_means), average, standard_error, missing))
    return summaries


def condition_means(ratings: Iterable[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    rows = list(ratings)
    return {
        condition: {
            summary.dimension: summary.mean
            for summary in summarize_ratings(rows)
            if summary.condition == condition
        }
        for condition in sorted({str(row.get("condition") or "") for row in rows})
    }


def paired_deltas(
    ratings: Iterable[dict[str, Any]],
    baseline_condition: str,
    comparison_condition: str,
) -> dict[str, dict[str, float | int | None]]:
    """Compare conditions only where the same item and rater rated both."""
    rows = list(ratings)
    by_key: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("item_id"), row.get("rater_id"), row.get("condition"))
        if key in by_key:
            raise ValueError("DUPLICATE_READER_RATING")
        by_key[key] = row
    output: dict[str, dict[str, float | int | None]] = {}
    for dimension in DIMENSIONS:
        task_deltas: dict[str, list[int]] = {}
        for (item_id, rater_id, condition), row in by_key.items():
            if condition != baseline_condition:
                continue
            other = by_key.get((item_id, rater_id, comparison_condition))
            task_id = row.get("task_id")
            if other and task_id != other.get("task_id"):
                raise ValueError("PAIRED_TASK_ID_MISMATCH")
            left = row.get("scores", {}).get(dimension)
            right = other.get("scores", {}).get(dimension) if other else None
            if _numeric(left) and _numeric(right) and isinstance(task_id, str) and task_id:
                task_deltas.setdefault(task_id, []).append(right - left)
        task_means = [mean(values) for _, values in sorted(task_deltas.items())]
        output[dimension] = {
            "paired_count": sum(len(values) for values in task_deltas.values()),
            "independent_task_count": len(task_means),
            "mean_delta": mean(task_means) if task_means else None,
        }
    return output


def paired_task_bootstrap(
    ratings: Iterable[dict[str, Any]],
    baseline_condition: str,
    comparison_condition: str,
    *,
    seed: int = 0,
    samples: int = 2000,
) -> dict[str, dict[str, Any]]:
    """Bootstrap paired effects by independent task, not by chapter/rater row."""
    rows = list(ratings)
    by_key: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("item_id"), row.get("rater_id"), row.get("condition"))
        if key in by_key:
            raise ValueError("DUPLICATE_READER_RATING")
        by_key[key] = row
    output: dict[str, dict[str, Any]] = {}
    for index, dimension in enumerate(DIMENSIONS):
        task_deltas: dict[str, list[float]] = {}
        for (item_id, rater_id, condition), row in by_key.items():
            if condition != baseline_condition:
                continue
            other = by_key.get((item_id, rater_id, comparison_condition))
            left = row.get("scores", {}).get(dimension)
            right = other.get("scores", {}).get(dimension) if other else None
            task_id = row.get("task_id")
            if other and task_id != other.get("task_id"):
                raise ValueError("PAIRED_TASK_ID_MISMATCH")
            if _numeric(left) and _numeric(right) and isinstance(task_id, str) and task_id:
                task_deltas.setdefault(task_id, []).append(float(right - left))
        task_means = [mean(values) for _, values in sorted(task_deltas.items())]
        interval = bootstrap_mean_ci(task_means, seed=seed + index, samples=samples) if len(task_means) >= MINIMUM_TASKS_FOR_EXPLORATORY_BOOTSTRAP else None
        output[dimension] = {
            "independent_task_count": len(task_means),
            "mean_paired_task_delta": mean(task_means) if task_means else None,
            "bootstrap_95_ci": list(interval) if interval else None,
            "status": "EXPLORATORY_ESTIMATE" if interval else "INSUFFICIENT_TASKS",
        }
    return output


def weighted_cohens_kappa(first: Iterable[int], second: Iterable[int], *, weight: str = "quadratic") -> float | None:
    left, right = list(first), list(second)
    if len(left) != len(right) or not left:
        return None
    if any(not _numeric(value) for value in [*left, *right]):
        raise ValueError("KAPPA_SCORE_INVALID")
    if weight not in {"linear", "quadratic"}:
        raise ValueError("KAPPA_WEIGHT_INVALID")
    categories = (1, 2, 3, 4, 5)
    distance = 4 if weight == "linear" else 16

    def disagreement(a: int, b: int) -> float:
        delta = abs(a - b)
        return delta / distance if weight == "linear" else delta * delta / distance

    observed = sum(disagreement(a, b) for a, b in zip(left, right)) / len(left)
    left_share = {category: left.count(category) / len(left) for category in categories}
    right_share = {category: right.count(category) / len(right) for category in categories}
    expected = sum(left_share[a] * right_share[b] * disagreement(a, b) for a in categories for b in categories)
    if expected == 0:
        return 1.0
    return round(1 - observed / expected, 6)


def _bootstrap_task_kappa_ci(
    task_items: list[tuple[str, list[tuple[int, int]]]],
    *,
    seed: int,
    samples: int = 2000,
) -> list[float] | None:
    if len(task_items) < MINIMUM_TASKS_FOR_EXPLORATORY_BOOTSTRAP or sum(len(values) for _, values in task_items) < MINIMUM_ITEMS_FOR_DESCRIPTIVE_RATER_AGREEMENT:
        return None
    rng = random.Random(seed)
    task_values = [values for _, values in task_items]
    bootstrapped: list[float] = []
    for _ in range(samples):
        selected = [rng.choice(task_values) for _ in task_values]
        pairs = [pair for task in selected for pair in task]
        score = weighted_cohens_kappa([pair[0] for pair in pairs], [pair[1] for pair in pairs])
        if score is not None:
            bootstrapped.append(score)
    if not bootstrapped:
        return None
    bootstrapped.sort()
    low = max(0, int(0.025 * len(bootstrapped)))
    high = min(len(bootstrapped) - 1, int(0.975 * len(bootstrapped)) - 1)
    return [bootstrapped[low], bootstrapped[high]]


def inter_rater_agreement(ratings: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows = list(ratings)
    output: dict[str, dict[str, Any]] = {}
    for dimension in DIMENSIONS:
        grouped: dict[tuple[Any, Any], dict[str, int]] = {}
        task_by_item: dict[tuple[Any, Any], str] = {}
        for row in rows:
            score = row.get("scores", {}).get(dimension)
            if not _numeric(score):
                continue
            key = (row.get("item_id"), row.get("condition"))
            task_id = row.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("READER_TASK_ID_REQUIRED_FOR_AGREEMENT")
            prior_task = task_by_item.setdefault(key, task_id)
            if prior_task != task_id:
                raise ValueError("READER_TASK_ID_MISMATCH")
            raters = grouped.setdefault(key, {})
            rater_id = row.get("rater_id")
            if rater_id in raters:
                raise ValueError("DUPLICATE_READER_RATING")
            raters[rater_id] = score
        pair_scores: dict[tuple[str, str], dict[str, list[tuple[int, int]]]] = {}
        for item_key, raters in grouped.items():
            for first, second in combinations(sorted(raters), 2):
                pair_by_task = pair_scores.setdefault((first, second), {})
                pair_by_task.setdefault(task_by_item[item_key], []).append((raters[first], raters[second]))
        pair_values: list[float] = []
        pair_intervals: list[list[float] | None] = []
        independent_task_counts: list[int] = []
        for pair_by_task in pair_scores.values():
            task_items = [(task_id, values) for task_id, values in sorted(pair_by_task.items()) if values]
            left = [pair[0] for _, values in task_items for pair in values]
            right = [pair[1] for _, values in task_items for pair in values]
            value = weighted_cohens_kappa(left, right) if len(left) >= 2 else None
            if value is not None:
                pair_values.append(value)
                independent_task_counts.append(len(task_items))
                pair_intervals.append(_bootstrap_task_kappa_ci(task_items, seed=len(pair_values)))
        pair_count = len(pair_values)
        usable_intervals = [interval for interval in pair_intervals if interval is not None]
        output[dimension] = {
            "rated_item_count": sum(len(raters) >= 2 for raters in grouped.values()),
            "rater_pair_count": len(pair_scores),
            "estimable_rater_pair_count": pair_count,
            "mean_pairwise_quadratic_kappa": mean(pair_values) if pair_values else None,
            "independent_task_count": min(independent_task_counts) if independent_task_counts else 0,
            "bootstrap_95_ci": [mean([interval[0] for interval in usable_intervals]), mean([interval[1] for interval in usable_intervals])] if usable_intervals else None,
            "status": "EXPLORATORY_INTERVAL" if usable_intervals else "LOW_SAMPLE_DESCRIPTIVE" if pair_values else "INSUFFICIENT_OVERLAP",
        }
    return output


def bootstrap_mean_ci(values: Iterable[float], *, seed: int = 0, samples: int = 2000, confidence: float = 0.95) -> tuple[float, float] | None:
    if samples < 100 or not 0 < confidence < 1:
        raise ValueError("BOOTSTRAP_PARAMETERS_INVALID")
    values = [float(value) for value in values]
    if any(not isfinite(value) for value in values):
        raise ValueError("BOOTSTRAP_VALUE_INVALID")
    if not values:
        return None
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    means = [mean(rng.choice(values) for _ in values) for _ in range(samples)]
    means.sort()
    lower_index = max(0, int((1 - confidence) / 2 * samples))
    upper_index = min(samples - 1, int((1 + confidence) / 2 * samples) - 1)
    return means[lower_index], means[upper_index]


def cohens_kappa(first: Iterable[int], second: Iterable[int]) -> float | None:
    """Return Cohen's kappa for two complete ordinal rating vectors."""
    left, right = list(first), list(second)
    if len(left) != len(right) or not left:
        return None
    observed = sum(a == b for a, b in zip(left, right)) / len(left)
    categories = sorted(set(left) | set(right))
    expected = sum((left.count(category) / len(left)) * (right.count(category) / len(right)) for category in categories)
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 6)


def compare_model_judgments(
    human_ratings: Iterable[dict[str, Any]],
    model_judgments: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compare a Judge to human scores without treating either as ground truth."""
    human_grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    judge_grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for row in human_ratings:
        human_grouped.setdefault((row.get("item_id"), row.get("condition")), []).append(row)
    for row in model_judgments:
        judge_grouped.setdefault((row.get("item_id"), row.get("condition")), []).append(row)
    output: dict[str, Any] = {"status": "NOT_COMPARABLE", "matched_count": 0, "dimensions": {}}
    for dimension in DIMENSIONS:
        errors: list[float] = []
        signed: list[float] = []
        matched_human_count = 0
        matched_judge_count = 0
        for key, human_rows in human_grouped.items():
            judge_rows = judge_grouped.get(key)
            if not judge_rows:
                continue
            human_scores = [row.get("scores", {}).get(dimension) for row in human_rows]
            judge_scores = [row.get("scores", {}).get(dimension) for row in judge_rows]
            human_scores = [score for score in human_scores if _numeric(score)]
            judge_scores = [score for score in judge_scores if _numeric(score)]
            if human_scores and judge_scores:
                human_mean = mean(human_scores)
                judge_mean = mean(judge_scores)
                errors.append(abs(judge_mean - human_mean))
                signed.append(judge_mean - human_mean)
                matched_human_count += len(human_scores)
                matched_judge_count += len(judge_scores)
        output["dimensions"][dimension] = {
            "matched_item_count": len(errors),
            "matched_human_rating_count": matched_human_count,
            "matched_judge_rating_count": matched_judge_count,
            "mean_absolute_error": mean(errors) if errors else None,
            "mean_signed_bias": mean(signed) if signed else None,
        }
    output["matched_count"] = max((item["matched_item_count"] for item in output["dimensions"].values()), default=0)
    output["status"] = "COMPARABLE" if output["matched_count"] else "NOT_COMPARABLE"
    return output


def compare_model_judgments_by_model(
    human_ratings: Iterable[dict[str, Any]],
    model_judgments: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for judgment in model_judgments:
        key = (str(judgment.get("model_name") or ""), str(judgment.get("prompt_fingerprint") or ""))
        groups.setdefault(key, []).append(judgment)
    return {
        f"{model_name}|{prompt_fingerprint}": compare_model_judgments(human_ratings, judgments)
        for (model_name, prompt_fingerprint), judgments in sorted(groups.items())
    }


def validate_model_judgment(judgment: dict[str, Any], *, text: str | None = None) -> list[dict[str, str]]:
    if not isinstance(judgment, dict):
        return [{"code": "MODEL_JUDGMENT_INVALID", "field": "model_judgment"}]
    required = {"item_id", "condition", "task_id", "model_name", "prompt_fingerprint", "scores", "evidence"}
    issues = [{"code": "MODEL_JUDGMENT_FIELD_MISSING", "field": field} for field in sorted(required - set(judgment))]
    for field in ("item_id", "condition", "task_id", "model_name", "prompt_fingerprint"):
        if field in judgment and (not isinstance(judgment[field], str) or not judgment[field].strip()):
            issues.append({"code": "MODEL_JUDGMENT_IDENTIFIER_INVALID", "field": field})
    scores = judgment.get("scores")
    evidence = judgment.get("evidence")
    if not isinstance(scores, dict):
        issues.append({"code": "MODEL_JUDGMENT_SCORES_INVALID", "field": "scores"})
        scores = {}
    if not isinstance(evidence, dict):
        issues.append({"code": "MODEL_JUDGMENT_EVIDENCE_INVALID", "field": "evidence"})
        evidence = {}
    for dimension in DIMENSIONS:
        if not _numeric(scores.get(dimension)):
            issues.append({"code": "MODEL_JUDGMENT_SCORE_INVALID", "field": f"scores.{dimension}"})
        quote = evidence.get(dimension)
        if not isinstance(quote, dict):
            issues.append({"code": "MODEL_JUDGMENT_QUOTE_INVALID", "field": f"evidence.{dimension}"})
            continue
        start, end, excerpt = quote.get("start_offset"), quote.get("end_offset"), quote.get("excerpt")
        if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool) or start < 0 or end <= start or not isinstance(excerpt, str) or not excerpt:
            issues.append({"code": "MODEL_JUDGMENT_QUOTE_INVALID", "field": f"evidence.{dimension}"})
        elif text is not None and (end > len(text) or text[start:end] != excerpt):
            issues.append({"code": "MODEL_JUDGMENT_QUOTE_MISMATCH", "field": f"evidence.{dimension}"})
    if set(scores) - set(DIMENSIONS):
        issues.append({"code": "MODEL_JUDGMENT_DIMENSION_UNKNOWN", "field": "scores"})
    if set(evidence) - set(DIMENSIONS):
        issues.append({"code": "MODEL_JUDGMENT_DIMENSION_UNKNOWN", "field": "evidence"})
    return issues


def summarize_cost_and_editing(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Summarize cost proxies while preserving unknown billing as unknown."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("condition") or ""), []).append(row)
    output: dict[str, dict[str, Any]] = {}
    for condition, items in sorted(grouped.items()):
        confirmed = [float(item["confirmed_tokens"]) for item in items if _nonnegative_integer(item.get("confirmed_tokens"))]
        token_per_char = [
            (float(item["confirmed_tokens"]) / float(item["output_chars"])) * 1000
            for item in items
            if _nonnegative_integer(item.get("confirmed_tokens"))
            and _nonnegative_number(item.get("output_chars"))
            and item["output_chars"] > 0
        ]
        editing = [float(item["editing_minutes"]) for item in items if _nonnegative_number(item.get("editing_minutes"))]
        elapsed = [float(item["elapsed_seconds"]) for item in items if _nonnegative_number(item.get("elapsed_seconds"))]
        unknown_calls = sum(item.get("unknown_usage_calls", 0) for item in items if _nonnegative_integer(item.get("unknown_usage_calls", 0)))
        reserved = sum(item.get("reserved_tokens", 0) for item in items if _nonnegative_integer(item.get("reserved_tokens", 0)))
        output[condition] = {
            "run_count": len(items),
            "completed_count": sum(item.get("status") == "COMPLETED" for item in items),
            "failure_count": sum(item.get("status") in {"FAILED", "BLOCKED"} for item in items),
            "mean_confirmed_tokens": mean(confirmed) if confirmed else None,
            "confirmed_token_missing_count": len(items) - len(confirmed),
            "mean_confirmed_tokens_per_1000_chars": mean(token_per_char) if token_per_char else None,
            "token_per_char_missing_count": len(items) - len(token_per_char),
            "mean_editing_minutes": mean(editing) if editing else None,
            "editing_time_missing_count": len(items) - len(editing),
            "mean_elapsed_seconds": mean(elapsed) if elapsed else None,
            "elapsed_time_missing_count": len(items) - len(elapsed),
            "unknown_usage_calls": unknown_calls,
            "reserved_tokens": reserved,
            "provider_cost_status": "UNKNOWN",
        }
    return output


def summarize_output_status(outputs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    allowed = {"COMPLETED", "FAILED", "BLOCKED", "INTERRUPTED", "MISSING", "NOT_STARTED"}
    for row in outputs:
        if not isinstance(row, dict):
            row = {"status": "UNKNOWN_STATUS"}
        condition = str(row.get("condition") or "")
        status = str(row.get("status") or "MISSING").upper()
        if status not in allowed:
            status = "UNKNOWN_STATUS"
        grouped.setdefault(condition, []).append({**row, "_normalized_status": status})
    return {
        condition: {
            "scheduled_count": len(rows),
            "attempted_count": sum(_nonnegative_integer(row.get("attempts")) and row["attempts"] > 0 for row in rows),
            "completed_count": sum(row["_normalized_status"] == "COMPLETED" for row in rows),
            "failed_count": sum(row["_normalized_status"] == "FAILED" for row in rows),
            "blocked_count": sum(row["_normalized_status"] == "BLOCKED" for row in rows),
            "interrupted_count": sum(row["_normalized_status"] == "INTERRUPTED" for row in rows),
            "missing_count": sum(row["_normalized_status"] in {"MISSING", "NOT_STARTED"} for row in rows),
            "unknown_status_count": sum(row["_normalized_status"] == "UNKNOWN_STATUS" for row in rows),
        }
        for condition, rows in sorted(grouped.items())
    }


def summarize_reader_outcomes(ratings: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in ratings:
        grouped.setdefault(str(row.get("condition") or ""), []).append(row)
    output: dict[str, dict[str, Any]] = {}
    for condition, rows in sorted(grouped.items()):
        next_chapter = [row["next_chapter_willing"] for row in rows if isinstance(row.get("next_chapter_willing"), bool)]
        felt_ai = [row["felt_ai_like"] for row in rows if isinstance(row.get("felt_ai_like"), bool)]
        revisions = [float(row["revision_burden_minutes"]) for row in rows if _nonnegative_number(row.get("revision_burden_minutes"))]
        output[condition] = {
            "next_chapter_willing_yes": sum(next_chapter),
            "next_chapter_willing_count": len(next_chapter),
            "next_chapter_willing_missing_count": len(rows) - len(next_chapter),
            "felt_ai_like_yes": sum(felt_ai),
            "felt_ai_like_count": len(felt_ai),
            "felt_ai_like_missing_count": len(rows) - len(felt_ai),
            "mean_revision_burden_minutes": mean(revisions) if revisions else None,
            "revision_burden_count": len(revisions),
            "revision_burden_missing_count": len(rows) - len(revisions),
        }
    return output


def summarize_blind_coverage(
    assignments: Iterable[dict[str, Any]],
    ratings: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    expected: set[tuple[str, str]] = set()
    raters: set[str] = set()
    if assignments and len(assignments) < MINIMUM_RATERS_PER_TEXT:
        raise ValueError("BLIND_READER_MINIMUM_NOT_MET")
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ValueError("BLIND_ASSIGNMENT_INVALID")
        rater_id = assignment.get("rater_id")
        order = assignment.get("order")
        if not isinstance(rater_id, str) or not rater_id or rater_id in raters or not isinstance(order, list):
            raise ValueError("BLIND_ASSIGNMENT_INVALID")
        raters.add(rater_id)
        local_ids: set[str] = set()
        for anonymous_id in order:
            if not isinstance(anonymous_id, str) or not anonymous_id or anonymous_id in local_ids:
                raise ValueError("BLIND_ASSIGNMENT_INVALID")
            local_ids.add(anonymous_id)
            expected.add((rater_id, anonymous_id))
    observed: set[tuple[str, str]] = set()
    for rating in ratings:
        if not isinstance(rating, dict):
            raise ValueError("BLIND_RATING_INVALID")
        key = (rating.get("rater_id"), rating.get("anonymous_text_id"))
        if key in observed:
            raise ValueError("DUPLICATE_BLIND_RATING")
        observed.add(key)
    if expected and not observed.issubset(expected):
        raise ValueError("BLIND_RATING_NOT_ASSIGNED")
    return {
        "expected_rating_count": len(expected),
        "received_rating_count": len(observed),
        "missing_rating_count": len(expected - observed),
        "unexpected_rating_count": len(observed - expected),
        "by_rater": {
            rater: {
                "assigned_count": sum(pair[0] == rater for pair in expected),
                "received_count": sum(pair[0] == rater for pair in observed),
                "missing_count": sum(pair[0] == rater for pair in expected - observed),
            }
            for rater in sorted(raters)
        },
    }


def evaluate_experiment(record: dict[str, Any], *, model_texts: dict[tuple[str, str], str] | None = None) -> dict[str, Any]:
    """Produce a status-aware report separating human, model, and engineering data."""
    if not isinstance(record, dict):
        return {"status": "INVALID_EXPERIMENT_RECORD", "human_reader": {"status": "NOT_ANALYZED"}, "model_judge": {"status": "NOT_ANALYZED"}}
    raw_blind = record.get("blind_ratings") or []
    raw_mapping = record.get("unblinding_map") or []
    raw_assignments = record.get("blind_assignments") or []
    raw_human = record.get("human_ratings") or []
    raw_judgments = record.get("model_judgments") or []
    if not all(isinstance(value, list) for value in (raw_blind, raw_mapping, raw_assignments, raw_human, raw_judgments)):
        return {"status": "INVALID_EXPERIMENT_RECORD", "human_reader": {"status": "INVALID"}, "model_judge": {"status": "INVALID"}}
    blind_ratings = raw_blind
    mapping = raw_mapping
    try:
        assignments = raw_assignments
        if blind_ratings and (not mapping or not assignments):
            raise ValueError("BLIND_RESEARCHER_MAP_REQUIRED")
        blind_coverage = summarize_blind_coverage(assignments, blind_ratings) if assignments else {
            "expected_rating_count": 0, "received_rating_count": len(blind_ratings),
            "missing_rating_count": 0, "unexpected_rating_count": len(blind_ratings),
        }
        unblinded = unblind_ratings(blind_ratings, mapping, assignments if assignments else None) if blind_ratings else []
    except ValueError as exc:
        return {
            "status": "INVALID_HUMAN_DATA",
            "human_reader": {"status": "INVALID", "issues": [{"code": str(exc)}]},
            "model_judge": {"status": "UNREPORTED"},
            "blind_reader_coverage": {"status": "INVALID"},
            "engineering": record.get("engineering_metrics") or {},
            "cost_and_editing": record.get("cost_and_editing") or {},
        }
    ratings = [*raw_human, *unblinded]
    validation = [issue for rating in ratings for issue in validate_rating(rating)]
    seen_rating_keys: set[tuple[Any, Any, Any]] = set()
    for rating in ratings:
        if not isinstance(rating, dict):
            continue
        key = (rating.get("item_id"), rating.get("condition"), rating.get("rater_id"))
        if not all(isinstance(value, str) for value in key):
            continue
        if key in seen_rating_keys:
            validation.append(RatingValidationIssue("DUPLICATE_READER_RATING", "human_ratings", "item, condition and rater combination must be unique"))
        seen_rating_keys.add(key)
    if validation:
        return {
            "status": "INVALID_HUMAN_DATA",
            "human_reader": {"status": "INVALID", "issues": [issue.__dict__ for issue in validation]},
            "model_judge": {"status": "UNREPORTED"},
            "engineering": record.get("engineering_metrics") or {},
            "cost_and_editing": record.get("cost_and_editing") or {},
        }
    human_status = "NOT_COLLECTED" if not ratings else "COLLECTED"
    judgments = raw_judgments
    judgment_issues: list[dict[str, str]] = []
    seen_judgments: set[tuple[Any, Any, Any, Any]] = set()
    for index, judgment in enumerate(judgments):
        if not isinstance(judgment, dict):
            judgment_issues.append({"code": "MODEL_JUDGMENT_INVALID", "field": f"model_judgments.{index}"})
            continue
        judgment_key = (judgment.get("item_id"), judgment.get("condition"), judgment.get("model_name"), judgment.get("prompt_fingerprint"))
        if all(isinstance(value, str) for value in judgment_key):
            if judgment_key in seen_judgments:
                judgment_issues.append({"code": "MODEL_JUDGMENT_DUPLICATE", "field": f"model_judgments.{index}"})
            seen_judgments.add(judgment_key)
        source_text = model_texts.get((judgment.get("item_id"), judgment.get("condition"))) if model_texts and isinstance(judgment.get("item_id"), str) and isinstance(judgment.get("condition"), str) else None
        judgment_issues.extend({"code": item["code"], "field": f"model_judgments.{index}.{item['field']}"} for item in validate_model_judgment(judgment, text=source_text))
    raw_conditions = record.get("conditions") or []
    conditions = list(raw_conditions) if isinstance(raw_conditions, list) else []
    condition_ids = {
        item if isinstance(item, str) else item.get("id")
        for item in conditions
        if isinstance(item, str) or isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    design_issues = validate_experiment_record(record)
    experiment_status = str(record.get("status") or "PLANNED").upper()
    design_validation = {
        "status": "FROZEN_VALID" if experiment_status in {"FROZEN", "COMPLETE", "COMPLETED_WITH_FAILURES"} and not design_issues else "INVALID" if experiment_status in {"FROZEN", "COMPLETE", "COMPLETED_WITH_FAILURES"} else "NOT_FROZEN",
        "experiment_status": experiment_status,
        "fingerprint": experiment_fingerprint(record),
        "issues": design_issues,
    }
    if design_validation["status"] == "INVALID":
        return {
            "status": "INVALID_EXPERIMENT_DESIGN",
            "experiment_design": design_validation,
            "human_reader": {"status": "NOT_ANALYZED"},
            "model_judge": {"status": "NOT_ANALYZED"},
            "engineering": record.get("engineering_metrics") or {},
            "cost_and_editing": summarize_cost_and_editing(record.get("cost_and_editing") or []),
        }
    frozen = experiment_status in {"FROZEN", "COMPLETE", "COMPLETED_WITH_FAILURES"}
    if not frozen and (ratings or judgments):
        return {
            "status": "UNFROZEN_EVALUATION_DATA",
            "experiment_design": design_validation,
            "human_reader": {"status": "NOT_ANALYZED", "submitted_rating_count": len(ratings)},
            "model_judge": {"status": "NOT_ANALYZED", "submitted_judgment_count": len(judgments)},
        }
    output_by_key = {
        (row.get("item_id"), row.get("condition")): row
        for row in (record.get("outputs") or [])
        if isinstance(row, dict) and isinstance(row.get("item_id"), str) and isinstance(row.get("condition"), str)
    }
    if frozen and (assignments or mapping):
        preparation = record.get("blind_preparation")
        expected_design = None
        if (
            isinstance(preparation, dict)
            and preparation.get("experiment_fingerprint") == design_validation["fingerprint"]
            and type(preparation.get("seed")) is int
            and len(assignments) >= record["analysis"]["minimum_raters_per_text"]
        ):
            completed_outputs = [row for row in record["outputs"] if row["status"] == "COMPLETED"]
            if completed_outputs:
                expected_design = prepare_blind_assignments(
                    completed_outputs,
                    [assignment["rater_id"] for assignment in assignments],
                    seed=preparation["seed"],
                )
        if expected_design != {"assignments": assignments, "unblinding_map": mapping}:
            return {
                "status": "INVALID_HUMAN_DATA",
                "experiment_design": design_validation,
                "human_reader": {"status": "INVALID", "issues": [{"code": "BLIND_PREPARATION_MISMATCH"}]},
                "model_judge": {"status": "NOT_ANALYZED"},
            }
    invalid_mapping = [
        row for row in mapping
        if not isinstance(row, dict)
        or (output := output_by_key.get((row.get("item_id"), row.get("condition")))) is None
        or output.get("status") != "COMPLETED"
        or output.get("task_id") != row.get("task_id")
    ]
    if invalid_mapping:
        return {
            "status": "INVALID_HUMAN_DATA",
            "experiment_design": design_validation,
            "human_reader": {"status": "INVALID", "issues": [{"code": "UNBLINDING_MAP_OUTPUT_REFERENCE_INVALID", "count": len(invalid_mapping)}]},
            "model_judge": {"status": "NOT_ANALYZED"},
        }
    orphaned_human = [
        row for row in ratings
        if (output := output_by_key.get((row.get("item_id"), row.get("condition")))) is None
        or output.get("status") != "COMPLETED"
        or output.get("task_id") != row.get("task_id")
    ]
    if orphaned_human:
        return {
            "status": "INVALID_HUMAN_DATA",
            "experiment_design": design_validation,
            "human_reader": {"status": "INVALID", "issues": [{"code": "RATING_OUTPUT_REFERENCE_INVALID", "count": len(orphaned_human)}]},
            "model_judge": {"status": "NOT_ANALYZED"},
        }
    for index, judgment in enumerate(judgments):
        if not isinstance(judgment, dict):
            continue
        key = (judgment.get("item_id"), judgment.get("condition"))
        output = output_by_key.get(key) if all(isinstance(value, str) for value in key) else None
        if not output or output.get("status") != "COMPLETED" or output.get("task_id") != judgment.get("task_id"):
            judgment_issues.append({"code": "MODEL_JUDGMENT_OUTPUT_REFERENCE_INVALID", "field": f"model_judgments.{index}"})
    model_status = "NOT_REPORTED" if not judgments else "INVALID" if judgment_issues else "REPORTED"
    status = "HUMAN_RATED" if ratings else "MODEL_ONLY" if judgments and not judgment_issues else "INVALID_MODEL_DATA" if judgments else "NOT_EVALUATED"
    model_comparison = compare_model_judgments_by_model(ratings, judgments) if ratings and judgments and not judgment_issues else {}
    comparison = record.get("analysis") or {}
    if not isinstance(comparison, dict):
        comparison = {}
    baseline = comparison.get("baseline_condition", "A_BASELINE")
    candidate = comparison.get("comparison_condition", "C_ENGINE")
    if condition_ids and ({baseline, candidate} - condition_ids):
        pairwise = {}
        paired_task_intervals = {}
    else:
        pairwise = paired_deltas(ratings, baseline, candidate) if ratings else {}
        paired_task_intervals = paired_task_bootstrap(ratings, baseline, candidate, seed=int(comparison.get("bootstrap_seed", 0))) if ratings else {}
    try:
        agreement = inter_rater_agreement(ratings)
    except ValueError as exc:
        agreement = {"status": "INVALID_READER_KEYS", "error": str(exc)}
    if not judgments:
        quote_verification = "NOT_AVAILABLE"
    elif judgment_issues:
        quote_verification = "INVALID"
    elif not model_texts:
        quote_verification = "NOT_VERIFIED"
    else:
        quote_verification = "VERIFIED" if all((row.get("item_id"), row.get("condition")) in model_texts for row in judgments) else "PARTIALLY_VERIFIED"
    return {
        "status": status,
        "experiment_design": design_validation,
        "human_reader": {
            "status": human_status,
            "rating_count": len(ratings),
            "blind_reader_coverage": {"status": "NOT_PREPARED" if not assignments else "REPORTED", **blind_coverage},
            "unrated_blind_item_count": max(0, len({row.get("anonymous_text_id") for row in mapping}) - len({row.get("anonymous_text_id") for row in blind_ratings})),
            "summaries": [summary.__dict__ for summary in summarize_ratings(ratings)],
            "inter_rater_agreement": agreement,
        },
        "model_judge": {"status": model_status, "judgment_count": len(judgments), "validation_issues": judgment_issues, "human_comparison_by_model": model_comparison, "quote_verification": quote_verification},
        "condition_means": condition_means(ratings),
        "paired_deltas": pairwise,
        "paired_task_bootstrap": paired_task_intervals,
        "all_condition_pairs": {
            f"{left}|{right}": {
                "paired_deltas": paired_deltas(ratings, left, right),
                "paired_task_bootstrap": paired_task_bootstrap(ratings, left, right, seed=int(comparison.get("bootstrap_seed", 0))),
            }
            for left, right in combinations(sorted(condition_ids), 2)
        } if ratings else {},
        "generation_outcomes": summarize_output_status(record.get("outputs") or []),
        "generation_cost": summarize_cost_and_editing(record.get("outputs") or []),
        "reader_outcomes": summarize_reader_outcomes(ratings),
        "condition_design": record.get("conditions") or [],
        "dimension_groups": {key: list(value) for key, value in DIMENSION_GROUPS.items()},
        "engineering": record.get("engineering_metrics") or {},
        "cost_and_editing": summarize_cost_and_editing(record.get("cost_and_editing") or []),
    }
