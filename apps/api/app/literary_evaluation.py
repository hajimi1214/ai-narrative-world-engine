"""Human-centered evaluation helpers for long-form Chinese fiction.

This module intentionally stays outside the production quality gate. It
summarizes frozen experiment records and never changes a chapter, world state,
Critic decision, or adoption status.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
import random
from statistics import mean
from typing import Any, Iterable


DIMENSIONS: tuple[str, ...] = (
    "character_credibility",
    "character_growth",
    "motivation",
    "plot_engagement",
    "conflict",
    "emotion",
    "pacing",
    "chapter_structure",
    "style_naturalness",
    "continuity",
    "reading_willingness",
)

DIMENSION_GROUPS: dict[str, tuple[str, ...]] = {
    "STORY_FACTS_AND_CONTINUITY": ("continuity",),
    "LITERARY_EXPRESSION": (
        "character_credibility", "character_growth", "motivation", "plot_engagement",
        "conflict", "emotion", "pacing", "chapter_structure", "style_naturalness",
    ),
    "READER_EXPERIENCE": ("reading_willingness",),
}


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
    mean: float | None
    standard_error: float | None
    missing_count: int


def _numeric(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5


def validate_rating(rating: dict[str, Any]) -> list[RatingValidationIssue]:
    """Validate one blind-reader rating without accepting hidden model scores."""
    issues: list[RatingValidationIssue] = []
    required = {"item_id", "condition", "rater_id", "scores", "evidence", "confidence"}
    for field in sorted(required - set(rating)):
        issues.append(RatingValidationIssue("RATING_FIELD_MISSING", field, "required rating field is missing"))
    scores = rating.get("scores")
    if not isinstance(scores, dict):
        return issues + [RatingValidationIssue("RATING_SCORES_INVALID", "scores", "scores must be an object")]
    unknown = sorted(set(scores) - set(DIMENSIONS))
    issues.extend(RatingValidationIssue("RATING_DIMENSION_UNKNOWN", key, "unknown evaluation dimension") for key in unknown)
    missing = sorted(set(DIMENSIONS) - set(scores))
    issues.extend(RatingValidationIssue("RATING_DIMENSION_MISSING", key, "every dimension needs a score") for key in missing)
    for dimension, value in scores.items():
        if dimension in DIMENSIONS and not _numeric(value):
            issues.append(RatingValidationIssue("RATING_SCORE_INVALID", dimension, "score must be an integer from 1 to 5"))
    evidence = rating.get("evidence")
    if not isinstance(evidence, dict):
        issues.append(RatingValidationIssue("RATING_EVIDENCE_INVALID", "evidence", "evidence must be an object"))
    else:
        for dimension in DIMENSIONS:
            if not str(evidence.get(dimension) or "").strip():
                issues.append(RatingValidationIssue("RATING_EVIDENCE_MISSING", dimension, "each score needs a short text-grounded reason"))
    confidence = rating.get("confidence")
    if not _numeric(confidence):
        issues.append(RatingValidationIssue("RATING_CONFIDENCE_INVALID", "confidence", "confidence must be an integer from 1 to 5"))
    for field in ("model", "prompt", "raw_score", "critic_score"):
        if field in rating:
            issues.append(RatingValidationIssue("RATING_MODEL_METADATA_FORBIDDEN", field, "blind rating cannot contain model or prompt metadata"))
    return issues


def summarize_ratings(ratings: Iterable[dict[str, Any]]) -> list[RatingSummary]:
    rows = list(ratings)
    conditions = sorted({str(row.get("condition") or "") for row in rows})
    summaries: list[RatingSummary] = []
    for condition in conditions:
        condition_rows = [row for row in rows if str(row.get("condition") or "") == condition]
        for dimension in DIMENSIONS:
            values = [row.get("scores", {}).get(dimension) for row in condition_rows if _numeric(row.get("scores", {}).get(dimension))]
            missing = len(condition_rows) - len(values)
            average = mean(values) if values else None
            standard_error = None
            if len(values) > 1:
                variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
                standard_error = sqrt(variance / len(values))
            summaries.append(RatingSummary(condition, dimension, len(values), average, standard_error, missing))
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
    by_key = {(row.get("item_id"), row.get("rater_id"), row.get("condition")): row for row in rows}
    output: dict[str, dict[str, float | int | None]] = {}
    for dimension in DIMENSIONS:
        deltas = []
        for (item_id, rater_id, condition), row in by_key.items():
            if condition != baseline_condition:
                continue
            other = by_key.get((item_id, rater_id, comparison_condition))
            left = row.get("scores", {}).get(dimension)
            right = other.get("scores", {}).get(dimension) if other else None
            if _numeric(left) and _numeric(right):
                deltas.append(right - left)
        output[dimension] = {
            "paired_count": len(deltas),
            "mean_delta": mean(deltas) if deltas else None,
        }
    return output


def bootstrap_mean_ci(values: Iterable[float], *, seed: int = 0, samples: int = 2000, confidence: float = 0.95) -> tuple[float, float] | None:
    values = [float(value) for value in values]
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
    human = {(row.get("item_id"), row.get("condition")): row for row in human_ratings}
    judge = {(row.get("item_id"), row.get("condition")): row for row in model_judgments}
    output: dict[str, Any] = {"status": "NOT_COMPARABLE", "matched_count": 0, "dimensions": {}}
    for dimension in DIMENSIONS:
        errors: list[float] = []
        signed: list[float] = []
        for key, human_row in human.items():
            judge_row = judge.get(key)
            human_score = human_row.get("scores", {}).get(dimension)
            judge_score = judge_row.get("scores", {}).get(dimension) if judge_row else None
            if _numeric(human_score) and _numeric(judge_score):
                errors.append(abs(judge_score - human_score))
                signed.append(judge_score - human_score)
        output["dimensions"][dimension] = {
            "matched_count": len(errors),
            "mean_absolute_error": mean(errors) if errors else None,
            "mean_signed_bias": mean(signed) if signed else None,
        }
    output["matched_count"] = max((item["matched_count"] for item in output["dimensions"].values()), default=0)
    output["status"] = "COMPARABLE" if output["matched_count"] else "NOT_COMPARABLE"
    return output


def summarize_cost_and_editing(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Summarize cost proxies while preserving unknown billing as unknown."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("condition") or ""), []).append(row)
    output: dict[str, dict[str, Any]] = {}
    for condition, items in sorted(grouped.items()):
        confirmed = [float(item["confirmed_tokens"]) for item in items if isinstance(item.get("confirmed_tokens"), (int, float))]
        token_per_char = [
            (float(item["confirmed_tokens"]) / float(item["output_chars"])) * 1000
            for item in items
            if isinstance(item.get("confirmed_tokens"), (int, float))
            and isinstance(item.get("output_chars"), (int, float))
            and item["output_chars"] > 0
        ]
        editing = [float(item["editing_minutes"]) for item in items if isinstance(item.get("editing_minutes"), (int, float))]
        output[condition] = {
            "run_count": len(items),
            "completed_count": sum(item.get("status") == "COMPLETED" for item in items),
            "failure_count": sum(item.get("status") in {"FAILED", "BLOCKED"} for item in items),
            "mean_confirmed_tokens": mean(confirmed) if confirmed else None,
            "mean_confirmed_tokens_per_1000_chars": mean(token_per_char) if token_per_char else None,
            "mean_editing_minutes": mean(editing) if editing else None,
            "unknown_usage_calls": sum(int(item.get("unknown_usage_calls", 0) or 0) for item in items),
            "reserved_tokens": sum(int(item.get("reserved_tokens", 0) or 0) for item in items),
            "provider_cost_status": "UNKNOWN",
        }
    return output


def evaluate_experiment(record: dict[str, Any]) -> dict[str, Any]:
    """Produce a status-aware report separating human, model, and engineering data."""
    ratings = list(record.get("human_ratings") or [])
    validation = [issue for rating in ratings for issue in validate_rating(rating)]
    if validation:
        return {
            "status": "INVALID_HUMAN_DATA",
            "human_reader": {"status": "INVALID", "issues": [issue.__dict__ for issue in validation]},
            "model_judge": {"status": "UNREPORTED"},
            "engineering": record.get("engineering_metrics") or {},
            "cost_and_editing": record.get("cost_and_editing") or {},
        }
    human_status = "NOT_COLLECTED" if not ratings else "COLLECTED"
    model_status = "REPORTED" if record.get("model_judgments") else "NOT_REPORTED"
    status = "HUMAN_RATED" if ratings else "MODEL_ONLY" if record.get("model_judgments") else "NOT_EVALUATED"
    model_comparison = compare_model_judgments(ratings, record.get("model_judgments") or []) if ratings else {"status": "NOT_COMPARABLE", "matched_count": 0, "dimensions": {}}
    return {
        "status": status,
        "human_reader": {"status": human_status, "rating_count": len(ratings), "summaries": [summary.__dict__ for summary in summarize_ratings(ratings)]},
        "model_judge": {"status": model_status, "judgments": record.get("model_judgments") or [], "human_comparison": model_comparison},
        "engineering": record.get("engineering_metrics") or {},
        "cost_and_editing": summarize_cost_and_editing(record.get("cost_and_editing") or []),
    }
