from app.literary_evaluation import (
    DIMENSIONS,
    bootstrap_mean_ci,
    cohens_kappa,
    condition_means,
    compare_model_judgments,
    evaluate_experiment,
    paired_deltas,
    validate_rating,
    summarize_cost_and_editing,
)


def rating(item_id="item-1", condition="A_BASELINE", rater_id="rater-1", offset=0):
    return {
        "item_id": item_id,
        "condition": condition,
        "rater_id": rater_id,
        "scores": {dimension: 3 + offset for dimension in DIMENSIONS},
        "evidence": {dimension: f"正文证据 {dimension}" for dimension in DIMENSIONS},
        "confidence": 4,
    }


def test_rating_validation_requires_all_dimensions_and_text_evidence():
    value = rating()
    assert validate_rating(value) == []
    value["scores"].pop("emotion")
    assert any(issue.code == "RATING_DIMENSION_MISSING" for issue in validate_rating(value))

    value = rating()
    value["model"] = "judge-model"
    assert any(issue.code == "RATING_MODEL_METADATA_FORBIDDEN" for issue in validate_rating(value))


def test_condition_means_and_paired_deltas_use_same_item_and_rater():
    rows = [
        rating(condition="A_BASELINE"),
        rating(condition="C_ENGINE", offset=1),
        rating(item_id="item-2", condition="A_BASELINE", rater_id="rater-2"),
        rating(item_id="item-2", condition="C_ENGINE", rater_id="rater-2", offset=-1),
    ]
    means = condition_means(rows)
    assert means["A_BASELINE"]["continuity"] == 3
    assert paired_deltas(rows, "A_BASELINE", "C_ENGINE")["continuity"] == {"paired_count": 2, "mean_delta": 0}


def test_bootstrap_and_kappa_are_deterministic_and_handle_missing_data():
    assert bootstrap_mean_ci([1, 2, 3], seed=42, samples=100) == bootstrap_mean_ci([1, 2, 3], seed=42, samples=100)
    assert bootstrap_mean_ci([]) is None
    assert cohens_kappa([1, 2, 3], [1, 2, 3]) == 1.0
    assert cohens_kappa([], []) is None


def test_experiment_report_does_not_turn_model_judgment_into_human_result():
    record = {"model_judgments": [{"condition": "C_ENGINE", "overall": 5}], "engineering_metrics": {"chapters": 3}}
    report = evaluate_experiment(record)
    assert report["status"] == "MODEL_ONLY"
    assert report["human_reader"]["status"] == "NOT_COLLECTED"


def test_invalid_human_data_is_explicitly_blocked():
    value = rating()
    value["evidence"]["continuity"] = ""
    report = evaluate_experiment({"human_ratings": [value]})
    assert report["status"] == "INVALID_HUMAN_DATA"
    assert any(issue["code"] == "RATING_EVIDENCE_MISSING" for issue in report["human_reader"]["issues"])


def test_model_judge_comparison_reports_bias_without_replacing_human_scores():
    human = rating()
    judge = {"item_id": human["item_id"], "condition": human["condition"], "scores": {dimension: 4 for dimension in DIMENSIONS}}
    report = compare_model_judgments([human], [judge])
    assert report["status"] == "COMPARABLE"
    assert report["dimensions"]["emotion"]["mean_absolute_error"] == 1
    assert report["dimensions"]["emotion"]["mean_signed_bias"] == 1


def test_cost_summary_keeps_provider_billing_unknown():
    summary = summarize_cost_and_editing([
        {"condition": "A_BASELINE", "status": "COMPLETED", "confirmed_tokens": 100, "output_chars": 1000, "editing_minutes": 12, "unknown_usage_calls": 1, "reserved_tokens": 500},
        {"condition": "A_BASELINE", "status": "FAILED", "confirmed_tokens": 40, "output_chars": 0, "editing_minutes": 0, "unknown_usage_calls": 0, "reserved_tokens": 0},
    ])
    assert summary["A_BASELINE"]["failure_count"] == 1
    assert summary["A_BASELINE"]["mean_confirmed_tokens_per_1000_chars"] == 100
    assert summary["A_BASELINE"]["provider_cost_status"] == "UNKNOWN"
