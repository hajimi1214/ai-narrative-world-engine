from copy import deepcopy

from app.golden_baseline import (
    check_chapter_coverage,
    check_foreshadowings,
    check_knowledge_uses,
    check_style,
    check_timeline,
    validate_corpus,
)
from golden_novel_fixture import build_golden_novel


def test_golden_corpus_has_required_long_form_shape():
    corpus = build_golden_novel()
    assert not validate_corpus(corpus)
    assert len(corpus["characters"]) == 10
    assert len(corpus["canon"]) >= 20
    assert len(corpus["threads"]) == 10
    assert len(corpus["arcs"]) == 3
    assert len(corpus["chapters"]) == 30
    assert len(corpus["timeline"]) == 20
    assert len(corpus["foreshadowings"]) == 10
    assert len(corpus["style_samples"]) == 5


def test_corpus_references_are_checked():
    corpus = build_golden_novel()
    corpus["chapters"][0]["thread_ids"] = ["thread_missing"]
    issues = validate_corpus(corpus)
    assert any(issue.code == "CORPUS_REFERENCE_INVALID" for issue in issues)


def test_knowledge_leak_is_blocked_but_explicit_false_belief_is_valid():
    corpus = build_golden_novel()
    character_id = "char_lin"
    matrix = corpus["knowledge_matrix"] | {character_id: {"canon_01": "FALSE_BELIEF"}}
    assert check_knowledge_uses(character_id=character_id, uses=[{"fact_id": "canon_01", "required_status": "FALSE_BELIEF"}], knowledge_matrix=matrix) == []
    issues = check_knowledge_uses(character_id=character_id, uses=[{"fact_id": "canon_20", "required_status": "KNOWN", "source_id": "chapter_003"}], knowledge_matrix=matrix)
    assert any(issue.code == "KNOWLEDGE_LEAK" for issue in issues)


def test_timeline_detects_reversed_order_and_location_conflict():
    corpus = build_golden_novel()
    events = deepcopy(corpus["timeline"])
    events[0], events[1] = events[1], events[0]
    events[1]["participants"] = ["char_lin"]
    events[1]["location_id"] = "loc_far"
    events[1]["start_time"] = "2041-03-01T08:10:00"
    events[1]["end_time"] = "2041-03-01T09:00:00"
    events[0]["participants"] = ["char_lin"]
    events[0]["location_id"] = "loc_other"
    events[0]["start_time"] = "2041-03-01T08:20:00"
    events[0]["end_time"] = "2041-03-01T08:50:00"
    issues = check_timeline(events)
    assert any(issue.code == "TIMELINE_ORDER_INVALID" for issue in issues)
    assert any(issue.code == "LOCATION_CONFLICT" for issue in issues)


def test_foreshadowing_detects_premature_reveal_and_invalid_status():
    corpus = build_golden_novel()
    corpus["chapters"][3]["reveals"] = ["foreshadow_01"]
    corpus["foreshadowings"][0]["status"] = "LOST"
    issues = check_foreshadowings(foreshadowings=corpus["foreshadowings"], chapters=corpus["chapters"])
    assert any(issue.code == "PREMATURE_REVEAL" for issue in issues)
    assert any(issue.code == "FORESHADOWING_STATUS_INVALID" for issue in issues)


def test_chapter_task_coverage_detects_missing_and_forbidden_events():
    chapter = build_golden_novel()["chapters"][0]
    chapter["delivered_events"] = [chapter["required_events"][0], "reveal_final_truth"]
    issues = check_chapter_coverage(chapter)
    assert any(issue.code == "CHAPTER_REQUIRED_EVENT_MISSING" for issue in issues)
    assert any(issue.code == "CHAPTER_FORBIDDEN_EVENT_PRESENT" for issue in issues)


def test_style_baseline_detects_template_expression():
    issues = check_style("与此同时，他不由得意识到事情并不简单。")
    codes = {issue.code for issue in issues}
    assert "ANTI_AI_DISABLED_EXPRESSION" in codes
    assert "ANTI_AI_WARNING_EXPRESSION" not in codes or "ANTI_AI_DISABLED_EXPRESSION" in codes
