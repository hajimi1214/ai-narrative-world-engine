"""Deterministic golden-novel baseline checks.

This module deliberately has no database or model dependency.  It validates the
small, serializable contract used by the long-form fiction regression corpus.
The same checks can later be called by an importer, API endpoint, or UI preview.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .quality import AntiAIStyleRuleEngine


GOLDEN_STYLE_RULES: dict[str, Any] = {
    "disabled_expressions": ["不由得", "与此同时", "他意识到"],
    "warning_expressions": ["仿佛", "某种意义上"],
    "frequency_limits": {
        "expressions": {"不由得": 0, "与此同时": 0, "他意识到": 0},
        "rare_words": {
            "攥": {"max_per_1000": 2, "severity": "MINOR"},
            "眸": {"max_per_1000": 2, "severity": "MINOR"},
            "喉结": {"max_per_1000": 1, "severity": "MINOR"},
            "唇角": {"max_per_1000": 2, "severity": "MINOR"},
            "身形": {"max_per_1000": 2, "severity": "MINOR"},
        },
        "scenery": {"max_consecutive_sentences": 3, "max_ratio": 0.35},
        "dialogue": {"max_similarity": 0.86, "max_recap_count": 2},
        "emotion_explanation": {"max_per_1000": 4},
        "cognitive_explanation": {"max_per_1000": 6},
        "template_structures": {"max_per_1000": 3},
        "abstract_summary": {"max_per_1000": 2},
        "repeated_exact_sentence": 3,
        "repeated_sentence_prefix": 3,
        "repeated_paragraph_opening": 3,
    },
    "writing_principles": ["通过行动和对白表达情绪", "避免段尾总结主题"],
    "future_risk_labels": ["TEMPLATE_LIKE_PROSE", "OVER_EXPLAINED_EMOTION"],
}


@dataclass(frozen=True)
class BaselineIssue:
    code: str
    severity: str
    message: str
    source_id: str | None = None


def _issue(code: str, message: str, *, severity: str = "BLOCKING", source_id: str | None = None) -> BaselineIssue:
    return BaselineIssue(code, severity, message, source_id)


def validate_corpus(corpus: dict[str, Any]) -> list[BaselineIssue]:
    """Validate references and cardinalities before a corpus is imported."""
    issues: list[BaselineIssue] = []
    required = ("book", "characters", "canon", "threads", "arcs", "chapters", "timeline", "foreshadowings", "knowledge_matrix", "style_samples")
    for key in required:
        if key not in corpus:
            issues.append(_issue("CORPUS_SECTION_MISSING", f"Missing corpus section: {key}", source_id=key))
    if issues:
        return issues

    expected = {"characters": 10, "canon": 20, "threads": 10, "arcs": 3, "chapters": 30, "timeline": 20, "foreshadowings": 10, "style_samples": 5}
    for key, minimum in expected.items():
        actual = len(corpus[key])
        if actual < minimum:
            issues.append(_issue("CORPUS_CARDINALITY_INVALID", f"{key} has {actual}; expected at least {minimum}", source_id=key))

    ids: dict[str, set[str]] = {}
    for key in ("characters", "canon", "threads", "arcs", "chapters", "timeline", "foreshadowings"):
        values = corpus[key]
        seen: set[str] = set()
        for item in values:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not item_id or item_id in seen:
                issues.append(_issue("CORPUS_ID_INVALID", f"Invalid or duplicate id in {key}", source_id=key))
            elif item_id:
                seen.add(item_id)
        ids[key] = seen

    for chapter in corpus["chapters"]:
        for key, id_key in (("arc_id", "arcs"), ("pov_character_id", "characters")):
            if chapter.get(key) not in ids[id_key]:
                issues.append(_issue("CORPUS_REFERENCE_INVALID", f"Chapter references unknown {key}", source_id=chapter.get("id")))
        for thread_id in chapter.get("thread_ids", []):
            if thread_id not in ids["threads"]:
                issues.append(_issue("CORPUS_REFERENCE_INVALID", "Chapter references unknown thread", source_id=chapter.get("id")))
        for fact_id in chapter.get("required_canon", []):
            if fact_id not in ids["canon"]:
                issues.append(_issue("CORPUS_REFERENCE_INVALID", "Chapter references unknown canon fact", source_id=chapter.get("id")))

    for arc in corpus["arcs"]:
        if any(number not in {chapter["number"] for chapter in corpus["chapters"]} for number in arc.get("chapter_numbers", [])):
            issues.append(_issue("CORPUS_REFERENCE_INVALID", "Arc references an unknown chapter", source_id=arc.get("id")))
    return issues


def check_chapter_coverage(chapter: dict[str, Any]) -> list[BaselineIssue]:
    """Check that a generated chapter result fulfils its task contract."""
    required = set(chapter.get("required_events", []))
    delivered = set(chapter.get("delivered_events", []))
    missing = sorted(required - delivered)
    forbidden = sorted(set(chapter.get("forbidden_events", [])) & delivered)
    issues: list[BaselineIssue] = []
    if missing:
        issues.append(_issue("CHAPTER_REQUIRED_EVENT_MISSING", f"Missing required events: {', '.join(missing)}", source_id=chapter.get("id")))
    if forbidden:
        issues.append(_issue("CHAPTER_FORBIDDEN_EVENT_PRESENT", f"Forbidden events present: {', '.join(forbidden)}", source_id=chapter.get("id")))
    return issues


def check_knowledge_uses(*, character_id: str, uses: list[dict[str, Any]], knowledge_matrix: dict[str, dict[str, str]]) -> list[BaselineIssue]:
    """Reject facts a character cannot know; false beliefs must be explicit."""
    known = knowledge_matrix.get(character_id, {})
    issues: list[BaselineIssue] = []
    for use in uses:
        fact_id, required_status = use.get("fact_id"), use.get("required_status", "KNOWN")
        actual = known.get(fact_id, "UNKNOWN")
        if actual != required_status:
            issues.append(_issue("KNOWLEDGE_LEAK", f"{character_id} uses {fact_id} as {required_status}; actual status is {actual}", source_id=use.get("source_id")))
    return issues


def check_timeline(events: list[dict[str, Any]]) -> list[BaselineIssue]:
    """Detect ordering errors and overlapping locations for one participant."""
    issues: list[BaselineIssue] = []
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    declared_sequences: list[int] = []
    for event in events:
        event_id = event.get("id")
        if not event_id or event_id in seen_ids:
            issues.append(_issue("TIMELINE_ID_INVALID", "Timeline event has a missing or duplicate id", source_id=event_id))
        seen_ids.add(event_id)
        if isinstance(event.get("sequence"), int):
            declared_sequences.append(event["sequence"])
        elif "sequence" in event:
            issues.append(_issue("TIMELINE_SEQUENCE_INVALID", "Timeline event has an invalid sequence", source_id=event_id))
        try:
            start = datetime.fromisoformat(event["start_time"]); end = datetime.fromisoformat(event["end_time"])
            if end < start:
                issues.append(_issue("TIMELINE_RANGE_INVALID", "Timeline event ends before it starts", source_id=event_id))
            parsed.append((start, event))
        except (KeyError, TypeError, ValueError):
            issues.append(_issue("TIMELINE_TIMESTAMP_INVALID", "Timeline event has an invalid start_time or end_time", source_id=event_id))
    if len(parsed) != len(events):
        return issues
    sequence = [item[1].get("id") for item in sorted(parsed, key=lambda item: item[0])]
    declared = [item.get("id") for item in events]
    if sequence != declared:
        issues.append(_issue("TIMELINE_ORDER_INVALID", "Declared event order differs from chronological order"))
    if declared_sequences and declared_sequences != sorted(set(declared_sequences)):
        issues.append(_issue("TIMELINE_SEQUENCE_INVALID", "Declared timeline sequences are not strictly increasing"))

    per_character: dict[str, list[dict[str, Any]]] = {}
    for _, event in parsed:
        for character_id in event.get("participants", []):
            per_character.setdefault(character_id, []).append(event)
    for character_id, rows in per_character.items():
        rows.sort(key=lambda item: item["start_time"])
        for previous, current in zip(rows, rows[1:]):
            previous_end = datetime.fromisoformat(previous["end_time"])
            current_start = datetime.fromisoformat(current["start_time"])
            if current_start < previous_end and current.get("location_id") != previous.get("location_id"):
                issues.append(_issue("LOCATION_CONFLICT", f"{character_id} is in two locations at once", source_id=current.get("id")))
    return issues


def check_foreshadowings(*, foreshadowings: list[dict[str, Any]], chapters: list[dict[str, Any]]) -> list[BaselineIssue]:
    """Check reveal windows and required payoffs against chapter plans."""
    by_number = {chapter.get("number"): chapter for chapter in chapters}
    issues: list[BaselineIssue] = []
    for item in foreshadowings:
        planted = item.get("planted_chapter")
        payoff = item.get("payoff_chapter")
        if not isinstance(planted, int) or not isinstance(payoff, int) or payoff <= planted:
            issues.append(_issue("FORESHADOWING_RANGE_INVALID", "Foreshadowing payoff must follow planting", source_id=item.get("id")))
            continue
        for number in range(planted + 1, payoff):
            if item.get("id") in by_number.get(number, {}).get("reveals", []):
                issues.append(_issue("PREMATURE_REVEAL", f"{item['id']} is revealed before chapter {payoff}", source_id=f"chapter_{number:03d}"))
        if item.get("required", True) and item.get("status") not in {"PLANNED", "PAID_OFF"}:
            issues.append(_issue("FORESHADOWING_STATUS_INVALID", "Required foreshadowing has no valid status", source_id=item.get("id")))
    return issues


def check_style(prose: str) -> list[BaselineIssue]:
    """Run the same deterministic anti-AI rules used by quality gates."""
    report = AntiAIStyleRuleEngine().evaluate(prose, GOLDEN_STYLE_RULES)
    return [
        _issue(
            finding.get("rule_code", "STYLE_FINDING"),
            finding.get("message", "Style rule matched"),
            severity=finding.get("severity", "MINOR"),
        )
        for finding in report["findings"]
    ]
