import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest

from app.literary_evaluation import (
    DIMENSIONS,
    bootstrap_mean_ci,
    cohens_kappa,
    condition_means,
    compare_model_judgments,
    compare_model_judgments_by_model,
    evaluate_experiment,
    experiment_fingerprint,
    inter_rater_agreement,
    paired_deltas,
    paired_task_bootstrap,
    prepare_blind_assignments,
    validate_rating,
    validate_blind_rating,
    validate_experiment_record,
    validate_model_judgment,
    unblind_ratings,
    weighted_cohens_kappa,
    summarize_cost_and_editing,
    summarize_blind_coverage,
    summarize_output_status,
)


def rating(item_id="item-1", condition="A_BASELINE", rater_id="rater-1", offset=0):
    return {
        "item_id": item_id,
        "condition": condition,
        "task_id": item_id.split(".", 1)[0],
        "rater_id": rater_id,
        "scores": {dimension: 3 + offset for dimension in DIMENSIONS},
        "evidence": {dimension: f"正文证据 {dimension}" for dimension in DIMENSIONS},
        "confidence": 4,
    }


def complete_output(record, task_id, condition, text="合成冻结正文。"):
    output = next(row for row in record["outputs"] if row["task_id"] == task_id and row["condition"] == condition)
    data = text.encode("utf-8")
    output.update({
        "status": "COMPLETED",
        "text_path": f"texts/{task_id}-{condition}.txt",
        "text_sha256": hashlib.sha256(data).hexdigest(),
        "output_chars": len(text),
    })
    return output


def test_rating_validation_requires_all_dimensions_and_text_evidence():
    value = rating()
    assert validate_rating(value) == []
    value["scores"].pop("emotion")
    assert any(issue.code == "RATING_DIMENSION_MISSING" for issue in validate_rating(value))

    value = rating()
    value["model"] = "judge-model"
    assert any(issue.code == "RATING_MODEL_METADATA_FORBIDDEN" for issue in validate_rating(value))


def test_condition_means_and_paired_deltas_use_same_item_and_rater():
    record = frozen_record()
    for task_id in ("task-1", "task-2"):
        complete_output(record, task_id, "A_BASELINE")
        complete_output(record, task_id, "C_ENGINE")
    rows = [
        rating(item_id="task-1.three-chapter-window", condition="A_BASELINE"),
        rating(item_id="task-1.three-chapter-window", condition="C_ENGINE", offset=1),
        rating(item_id="task-2.three-chapter-window", condition="A_BASELINE", rater_id="rater-2"),
        rating(item_id="task-2.three-chapter-window", condition="C_ENGINE", rater_id="rater-2", offset=-1),
    ]
    report = evaluate_experiment({**record, "human_ratings": rows})
    means = report["condition_means"]
    assert means["A_BASELINE"]["continuity"] == 3
    assert report["paired_deltas"]["continuity"] == {
        "paired_count": 2, "independent_task_count": 2, "mean_delta": 0,
    }


def test_paired_condition_delta_weights_tasks_equally_despite_uneven_reader_counts():
    rows = []
    for index in range(1, 5):
        rows.extend([
            rating(item_id="task-1.three-chapter-window", condition="A_BASELINE", rater_id=f"reader-{index}", offset=0),
            rating(item_id="task-1.three-chapter-window", condition="C_ENGINE", rater_id=f"reader-{index}", offset=2),
        ])
    rows.extend([
        rating(item_id="task-2.three-chapter-window", condition="A_BASELINE", offset=0),
        rating(item_id="task-2.three-chapter-window", condition="C_ENGINE", offset=-2),
    ])
    record = frozen_record()
    complete_output(record, "task-1", "A_BASELINE")
    complete_output(record, "task-1", "C_ENGINE")
    complete_output(record, "task-2", "A_BASELINE")
    complete_output(record, "task-2", "C_ENGINE")

    delta = evaluate_experiment({**record, "human_ratings": rows})["paired_deltas"]["continuity"]

    assert delta == {"paired_count": 5, "independent_task_count": 2, "mean_delta": 0}


def test_condition_summary_weights_independent_tasks_equally():
    rows = [
        rating(item_id="task-1.three-chapter-window", rater_id=f"rater-{index}", offset=-2)
        for index in range(1, 4)
    ]
    rows.append(rating(item_id="task-2.three-chapter-window", rater_id="rater-4", offset=2))
    record = frozen_record()
    complete_output(record, "task-1", "A_BASELINE")
    complete_output(record, "task-2", "A_BASELINE")
    summary = evaluate_experiment({**record, "human_ratings": rows})["human_reader"]["summaries"]
    continuity = next(item for item in summary if item["condition"] == "A_BASELINE" and item["dimension"] == "continuity")
    assert continuity["count"] == 4
    assert continuity["independent_task_count"] == 2
    assert continuity["mean"] == 3


def test_bootstrap_and_kappa_are_deterministic_and_handle_missing_data():
    assert bootstrap_mean_ci([1, 2, 3], seed=42, samples=100) == bootstrap_mean_ci([1, 2, 3], seed=42, samples=100)
    assert bootstrap_mean_ci([]) is None
    assert cohens_kappa([1, 2, 3], [1, 2, 3]) == 1.0
    assert cohens_kappa([], []) is None


def test_experiment_report_does_not_turn_model_judgment_into_human_result():
    quote = {"excerpt": "正文引用。", "start_offset": 0, "end_offset": 5}
    record = frozen_record()
    output = complete_output(record, "task-1", "C_ENGINE", "正文引用。")
    judgment = {
        "item_id": output["item_id"], "condition": "C_ENGINE", "task_id": "task-1",
        "model_name": "judge-v1", "prompt_fingerprint": "sha256:judge-v1",
        "scores": {dimension: 5 for dimension in DIMENSIONS},
        "evidence": {dimension: quote for dimension in DIMENSIONS},
    }
    record["model_judgments"] = [judgment]
    record["engineering_metrics"] = {"chapters": 3}
    report = evaluate_experiment(record, model_texts={(output["item_id"], "C_ENGINE"): "正文引用。"})
    assert report["status"] == "MODEL_ONLY"
    assert report["human_reader"]["status"] == "NOT_COLLECTED"
    assert report["model_judge"]["quote_verification"] == "VERIFIED"


def test_model_judge_requires_per_dimension_score_and_matching_text_quotes():
    quote = {"excerpt": "正文引用。", "start_offset": 0, "end_offset": 5}
    record = frozen_record()
    output = complete_output(record, "task-1", "C_ENGINE", "正文引用。")
    judgment = {
        "item_id": output["item_id"], "condition": "C_ENGINE", "task_id": "task-1",
        "model_name": "judge-v1", "prompt_fingerprint": "sha256:judge-v1",
        "scores": {dimension: 4 for dimension in DIMENSIONS},
        "evidence": {dimension: quote for dimension in DIMENSIONS},
    }
    assert validate_model_judgment(judgment, text="正文引用。") == []
    assert any(issue["code"] == "MODEL_JUDGMENT_QUOTE_MISMATCH" for issue in validate_model_judgment(judgment, text="另一段正文。"))
    record["model_judgments"] = [judgment]
    report = evaluate_experiment(record, model_texts={(output["item_id"], "C_ENGINE"): "另一段正文。"})
    assert report["status"] == "INVALID_MODEL_DATA"
    assert report["model_judge"]["quote_verification"] == "INVALID"


def test_model_judge_quote_offsets_use_unicode_code_points():
    text = "🙂正文引句。"
    quote = {"excerpt": "正文引句。", "start_offset": 1, "end_offset": 6}
    judgment = {
        "item_id": "task-1.window", "condition": "C_ENGINE", "task_id": "task-1",
        "model_name": "judge-v1", "prompt_fingerprint": "sha256:judge-v1",
        "scores": {dimension: 4 for dimension in DIMENSIONS},
        "evidence": {dimension: quote for dimension in DIMENSIONS},
    }
    assert validate_model_judgment(judgment, text=text) == []


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


def test_report_keeps_judge_models_separate_instead_of_averaging_them_together():
    human = rating()

    def judgment(model_name, score):
        return {
            "item_id": human["item_id"], "condition": human["condition"], "task_id": human["task_id"],
            "model_name": model_name, "prompt_fingerprint": f"sha256:{model_name}",
            "scores": {dimension: score for dimension in DIMENSIONS},
            "evidence": {dimension: {"excerpt": "句子。", "start_offset": 0, "end_offset": 3} for dimension in DIMENSIONS},
        }

    judgments = [judgment("judge-a", 5), judgment("judge-b", 1)]
    report = compare_model_judgments_by_model([human], judgments)
    assert set(report) == {"judge-a|sha256:judge-a", "judge-b|sha256:judge-b"}
    assert report["judge-a|sha256:judge-a"]["dimensions"]["emotion"]["mean_signed_bias"] == 2
    assert report["judge-b|sha256:judge-b"]["dimensions"]["emotion"]["mean_signed_bias"] == -2


def test_cost_summary_keeps_provider_billing_unknown():
    summary = summarize_cost_and_editing([
        {"condition": "A_BASELINE", "status": "COMPLETED", "confirmed_tokens": 100, "output_chars": 1000, "editing_minutes": 12, "unknown_usage_calls": 1, "reserved_tokens": 500},
        {"condition": "A_BASELINE", "status": "FAILED", "confirmed_tokens": 40, "output_chars": 0, "editing_minutes": 0, "unknown_usage_calls": 0, "reserved_tokens": 0},
    ])
    assert summary["A_BASELINE"]["failure_count"] == 1
    assert summary["A_BASELINE"]["mean_confirmed_tokens_per_1000_chars"] == 100
    assert summary["A_BASELINE"]["provider_cost_status"] == "UNKNOWN"


def test_generation_outcomes_separate_scheduled_runs_from_actual_attempts():
    summary = summarize_output_status([
        {"condition": "A_BASELINE", "status": "NOT_STARTED", "attempts": 0},
        {"condition": "A_BASELINE", "status": "FAILED", "attempts": 2},
    ])
    assert summary["A_BASELINE"]["scheduled_count"] == 2
    assert summary["A_BASELINE"]["attempted_count"] == 1
    assert summary["A_BASELINE"]["missing_count"] == 1


def test_blind_rating_template_rejects_condition_and_unblinding_restores_mapping():
    blind = {
        "schema_version": "blind-reader-rating-v2",
        "anonymous_text_id": "blind-0001",
        "rater_id": "rater-a",
        "scores": {dimension: 3 for dimension in DIMENSIONS},
        "evidence": {dimension: f"正文行为 {dimension}" for dimension in DIMENSIONS},
        "confidence": 4,
    }
    assert validate_blind_rating(blind) == []
    assert any(issue.code == "BLIND_RATING_METADATA_FORBIDDEN" for issue in validate_blind_rating({**blind, "condition": "C_ENGINE"}))
    canonical = unblind_ratings([blind], [{
        "anonymous_text_id": "blind-0001", "item_id": "chapter-1",
        "task_id": "task-1", "condition": "A_BASELINE",
    }])
    assert canonical[0]["condition"] == "A_BASELINE"
    assert canonical[0]["task_id"] == "task-1"
    assert summarize_blind_coverage([
        {"rater_id": "rater-a", "order": ["blind-0001", "blind-0002"]},
        {"rater_id": "rater-b", "order": ["blind-0001", "blind-0002"]},
    ], [blind]) == {
        "expected_rating_count": 4, "received_rating_count": 1,
        "missing_rating_count": 3, "unexpected_rating_count": 0,
        "by_rater": {
            "rater-a": {"assigned_count": 2, "received_count": 1, "missing_count": 1},
            "rater-b": {"assigned_count": 2, "received_count": 0, "missing_count": 2},
        },
    }
    with pytest.raises(ValueError, match="BLIND_READER_MINIMUM_NOT_MET"):
        summarize_blind_coverage([{"rater_id": "rater-a", "order": ["blind-0001"]}], [])
    with pytest.raises(ValueError, match="UNBLINDING_MAP_ASSIGNMENT_MISMATCH"):
        unblind_ratings([], [{
            "anonymous_text_id": "blind-0001", "item_id": "chapter-1",
            "condition": "A_BASELINE", "task_id": "task-1",
        }], [
            {"rater_id": "rater-a", "order": ["blind-0001", "blind-0002"]},
            {"rater_id": "rater-b", "order": ["blind-0001", "blind-0002"]},
        ])
    with pytest.raises(ValueError, match="BLIND_RATING_NOT_ASSIGNED"):
        unblind_ratings([blind], [{
            "anonymous_text_id": "blind-0001", "item_id": "chapter-1",
            "task_id": "task-1", "condition": "A_BASELINE",
        }], [{"rater_id": "other-rater", "order": ["blind-0001"]}])


def test_blind_rating_unblinding_rejects_missing_and_duplicate_private_map_entries():
    blind = {
        "schema_version": "blind-reader-rating-v2",
        "anonymous_text_id": "blind-0001",
        "rater_id": "rater-a",
        "scores": {dimension: 3 for dimension in DIMENSIONS},
        "evidence": {dimension: f"正文行为 {dimension}" for dimension in DIMENSIONS},
        "confidence": 4,
    }
    with pytest.raises(ValueError, match="UNBLINDING_MAP_ENTRY_MISSING"):
        unblind_ratings([blind], [])
    mapping = {"anonymous_text_id": "blind-0001", "item_id": "chapter-1", "task_id": "task-1", "condition": "A_BASELINE"}
    with pytest.raises(ValueError, match="DUPLICATE_READER_RATING"):
        unblind_ratings([blind, dict(blind)], [mapping])


def test_blind_assignments_are_reproducible_and_mask_condition_labels():
    outputs = [
        {"item_id": "chapter-1", "task_id": "task-1", "condition": "A_BASELINE"},
        {"item_id": "chapter-1", "task_id": "task-1", "condition": "C_ENGINE"},
    ]
    first = prepare_blind_assignments(outputs, ["rater-a", "rater-b"], seed=31)
    second = prepare_blind_assignments(outputs, ["rater-a", "rater-b"], seed=31)
    assert first == second
    assert all("condition" not in assignment for assignment in first["assignments"])
    assert {row["condition"] for row in first["unblinding_map"]} == {"A_BASELINE", "C_ENGINE"}


def test_frozen_blind_assignments_are_bound_to_the_preparation_seed_and_design():
    record = frozen_record(task_count=4)
    for output in record["outputs"]:
        complete_output(record, output["task_id"], output["condition"])
    assignment = prepare_blind_assignments(record["outputs"], ["reader-a", "reader-b"], seed=19)
    record["blind_assignments"] = assignment["assignments"]
    record["unblinding_map"] = assignment["unblinding_map"]
    record["blind_preparation"] = {
        "seed": 19,
        "experiment_fingerprint": experiment_fingerprint(record),
    }

    assert evaluate_experiment(record)["status"] == "NOT_EVALUATED"
    record["blind_assignments"][0]["order"].reverse()
    report = evaluate_experiment(record)
    assert report["status"] == "INVALID_HUMAN_DATA"
    assert report["human_reader"]["issues"][0]["code"] == "BLIND_PREPARATION_MISMATCH"


def test_weighted_kappa_and_task_cluster_bootstrap_report_independent_units():
    assert weighted_cohens_kappa([1, 2, 3, 4], [1, 2, 3, 5], weight="quadratic") > weighted_cohens_kappa([1, 2, 3, 4], [5, 4, 2, 1], weight="quadratic")
    rows = []
    for task_id, delta in (("task-1", 1), ("task-2", -1), ("task-3", 1)):
        rows.extend([
            rating(item_id=f"{task_id}.chapter", condition="A_BASELINE", rater_id="rater-a", offset=0),
            rating(item_id=f"{task_id}.chapter", condition="C_ENGINE", rater_id="rater-a", offset=delta),
        ])
    result = paired_task_bootstrap(rows, "A_BASELINE", "C_ENGINE", seed=15, samples=100)
    assert result["continuity"]["independent_task_count"] == 3
    assert result["continuity"]["bootstrap_95_ci"] is None
    assert result["continuity"]["status"] == "INSUFFICIENT_TASKS"


def test_duplicate_ratings_are_rejected_instead_of_silently_overwritten():
    value = rating()
    report = evaluate_experiment({"human_ratings": [value, dict(value)]})
    assert report["status"] == "INVALID_HUMAN_DATA"
    assert any(issue["code"] == "DUPLICATE_READER_RATING" for issue in report["human_reader"]["issues"])


def test_reader_agreement_reports_ordinal_weighted_agreement():
    rows = []
    for index in range(3):
        rows.append(rating(item_id=f"item-{index}", rater_id="rater-a", offset=0))
        rows.append(rating(item_id=f"item-{index}", rater_id="rater-b", offset=1))
    result = inter_rater_agreement(rows)
    assert result["continuity"]["status"] == "LOW_SAMPLE_DESCRIPTIVE"
    assert result["continuity"]["rater_pair_count"] == 1
    assert result["continuity"]["mean_pairwise_quadratic_kappa"] == weighted_cohens_kappa([3, 3, 3], [4, 4, 4])


def test_two_shared_items_report_low_sample_descriptive_agreement():
    rows = []
    for index in range(2):
        rows.append(rating(item_id=f"item-{index}", rater_id="rater-a", offset=0))
        rows.append(rating(item_id=f"item-{index}", rater_id="rater-b", offset=1))
    result = inter_rater_agreement(rows)
    assert result["continuity"]["mean_pairwise_quadratic_kappa"] is not None
    assert result["continuity"]["status"] == "LOW_SAMPLE_DESCRIPTIVE"


def test_five_shared_task_items_report_exploratory_rater_agreement_interval():
    rows = []
    for index in range(5):
        rows.append(rating(item_id=f"task-{index}.chapter", rater_id="rater-a", offset=0))
        rows.append(rating(item_id=f"task-{index}.chapter", rater_id="rater-b", offset=1))
    result = inter_rater_agreement(rows)["continuity"]
    assert result["independent_task_count"] == 5
    assert result["bootstrap_95_ci"] is not None
    assert result["status"] == "EXPLORATORY_INTERVAL"


def test_single_chapter_can_abstain_on_longitudinal_character_growth():
    value = rating()
    value["scores"]["character_growth"] = None
    value["score_abstentions"] = {"character_growth": "单章片段不足以判断长期人物成长。"}
    value["evidence"]["character_growth"] = ""
    assert validate_rating(value) == []
    invalid = dict(value)
    invalid["score_abstentions"] = {}
    assert any(issue.code == "RATING_SCORE_INVALID" for issue in validate_rating(invalid))


def frozen_record(task_count=2):
    conditions = [
        {"id": "A_BASELINE", "memory": False, "formal_world": False, "critic": False},
        {"id": "B_MEMORY", "memory": True, "formal_world": False, "critic": False},
        {"id": "C_ENGINE", "memory": True, "formal_world": True, "critic": True},
    ]
    tasks = [{"id": f"task-{index}", "chapters": 3} for index in range(1, task_count + 1)]
    outputs = []
    for task in tasks:
        for condition in conditions:
            outputs.append({
                "item_id": f"{task['id']}.three-chapter-window", "task_id": task["id"],
                "condition": condition["id"], "status": "FAILED", "attempts": 2,
            "model_name": "frozen-model", "prompt_fingerprint": f"sha256:{condition['id']}",
                "confirmed_tokens": 100, "unknown_usage_calls": 0,
                "reserved_tokens": 0, "elapsed_seconds": 12.5,
            })
    return {
        "experiment_id": "test",
        "system_revision": "1" * 40,
        "corpus_manifest": "pilot-manifest.json",
        "corpus_manifest_sha256": "a" * 64,
        "status": "FROZEN",
        "conditions": conditions,
        "tasks": tasks,
        "analysis": {"minimum_raters_per_text": 2},
        "generation": {
            "model_provider": "fake-compatible", "model_name": "frozen-model",
            "prompt_fingerprint": "sha256:design", "failure_and_retry_policy": "retry twice, then record failure",
            "sampling": {"temperature": 0.7, "top_p": 0.9, "seed": 21},
            "per_condition_token_budget": 10000,
            "condition_prompt_fingerprints": {condition["id"]: f"sha256:{condition['id']}" for condition in conditions},
            "condition_model_names": {condition["id"]: "frozen-model" for condition in conditions},
            "condition_implementation_fingerprints": {condition["id"]: f"{index + 1:064x}" for index, condition in enumerate(conditions)},
        },
        "outputs": outputs,
    }


def test_frozen_experiment_requires_complete_condition_task_outcome_grid():
    record = frozen_record()
    assert validate_experiment_record(record) == []
    record["outputs"].pop()
    assert any(issue["code"] == "EXPERIMENT_OUTPUT_OUTCOME_MISSING" for issue in validate_experiment_record(record))


def test_frozen_experiment_requires_matching_paired_item_ids_and_records_failures():
    record = frozen_record()
    record["outputs"][0]["status"] = "COMPLETED"
    record["outputs"][0].update({"text_path": "texts/task-1-a.txt", "text_sha256": "a" * 64, "output_chars": 1800})
    record["outputs"][1]["item_id"] = "different-item"
    issues = validate_experiment_record(record)
    assert any(issue["code"] == "EXPERIMENT_PAIRED_ITEM_ID_MISMATCH" for issue in issues)
    record["outputs"][1]["item_id"] = record["outputs"][0]["item_id"]
    assert validate_experiment_record(record) == []


def test_frozen_experiment_rejects_condition_model_drift():
    record = frozen_record()
    record["generation"]["condition_model_names"]["B_MEMORY"] = "different-model"
    assert any(issue["code"] == "EXPERIMENT_CONDITION_MODELS_MUST_MATCH" for issue in validate_experiment_record(record))


def test_frozen_experiment_rejects_condition_models_that_disagree_with_primary_model():
    record = frozen_record()
    record["generation"]["condition_model_names"]["B_MEMORY"] = "other-alias"
    assert any(issue["code"] == "EXPERIMENT_CONDITION_MODELS_MUST_MATCH" for issue in validate_experiment_record(record))


def test_frozen_experiment_rejects_duplicate_task_objects_and_reused_item_ids():
    record = frozen_record(task_count=2)
    record["tasks"].append(dict(record["tasks"][0]))
    assert any(issue["code"] == "EXPERIMENT_TASKS_DUPLICATE" for issue in validate_experiment_record(record))

    record = frozen_record(task_count=2)
    for output in record["outputs"]:
        if output["task_id"] == "task-2":
            output["item_id"] = "task-1.three-chapter-window"
    assert any(issue["code"] == "EXPERIMENT_ITEM_ID_REUSED" for issue in validate_experiment_record(record))


def test_experiment_validation_cli_accepts_complete_frozen_grid(tmp_path: Path):
    manifest_bytes = b'{"manifest":"synthetic"}\n'
    (tmp_path / "pilot-manifest.json").write_bytes(manifest_bytes)
    record = frozen_record()
    record["corpus_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    record_path = tmp_path / "frozen.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "validate_literary_experiment.py"

    completed = subprocess.run([sys.executable, str(script), str(record_path)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["valid"] is True
    assert len(report["fingerprint"]) == 64


def test_experiment_analyzer_reports_malformed_judgment_rows_without_crashing(tmp_path: Path):
    record = frozen_record()
    record["model_judgments"] = ["malformed"]
    manifest_bytes = b'{"manifest":"synthetic"}\n'
    (tmp_path / "pilot-manifest.json").write_bytes(manifest_bytes)
    record["corpus_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    record_path = tmp_path / "frozen.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "analyze_literary_experiment.py"

    completed = subprocess.run([sys.executable, str(script), str(record_path)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "INVALID_MODEL_DATA"
    assert report["model_judge"]["validation_issues"][0]["code"] == "MODEL_JUDGMENT_INVALID"


def test_planned_pilot_reports_unfrozen_design_without_suggesting_a_comparison():
    record = json.loads((Path(__file__).parents[3] / "research" / "evaluation" / "experiment-record-template.json").read_text(encoding="utf-8"))
    report = evaluate_experiment(record)
    assert report["status"] == "NOT_EVALUATED"
    assert report["experiment_design"]["status"] == "NOT_FROZEN"
    assert report["human_reader"]["status"] == "NOT_COLLECTED"
    assert report["human_reader"]["blind_reader_coverage"]["status"] == "NOT_PREPARED"
    assert report["condition_means"] == {}


def test_cost_summary_excludes_negative_and_nonfinite_values():
    summary = summarize_cost_and_editing([{
        "condition": "A_BASELINE", "status": "COMPLETED", "confirmed_tokens": -5,
        "output_chars": 100, "editing_minutes": float("inf"),
    }])["A_BASELINE"]
    assert summary["mean_confirmed_tokens"] is None
    assert summary["confirmed_token_missing_count"] == 1
    assert summary["mean_editing_minutes"] is None


def test_report_separates_reader_intent_and_revision_burden_from_dimension_scores():
    value = rating(item_id="task-1.three-chapter-window")
    value.update({"next_chapter_willing": True, "felt_ai_like": False, "revision_burden_minutes": 7})
    record = frozen_record()
    complete_output(record, "task-1", "A_BASELINE")
    report = evaluate_experiment({**record, "human_ratings": [value]})
    outcomes = report["reader_outcomes"]["A_BASELINE"]
    assert outcomes["next_chapter_willing_yes"] == 1
    assert outcomes["next_chapter_willing_count"] == 1
    assert outcomes["felt_ai_like_yes"] == 0
    assert outcomes["mean_revision_burden_minutes"] == 7


def test_frozen_report_compares_each_condition_pair_and_exposes_generation_outcomes():
    record = frozen_record(task_count=4)
    ratings = []
    for index in range(1, 5):
        for condition_index, condition in enumerate(("A_BASELINE", "B_MEMORY", "C_ENGINE")):
            complete_output(record, f"task-{index}", condition)
            value = rating(
                item_id=f"task-{index}.three-chapter-window",
                condition=condition,
                rater_id="reader-a",
                offset=condition_index,
            )
            ratings.append(value)
    report = evaluate_experiment({**record, "human_ratings": ratings})
    assert set(report["all_condition_pairs"]) == {
        "A_BASELINE|B_MEMORY",
        "A_BASELINE|C_ENGINE",
        "B_MEMORY|C_ENGINE",
    }
    assert report["all_condition_pairs"]["A_BASELINE|B_MEMORY"]["paired_task_bootstrap"]["continuity"]["independent_task_count"] == 4
    assert report["generation_outcomes"]["A_BASELINE"]["completed_count"] == 4
    assert report["generation_cost"]["A_BASELINE"]["run_count"] == 4


def test_paired_task_bootstrap_excludes_single_task_from_interval():
    rows = [
        rating(item_id="task-1.chapter", condition="A_BASELINE", rater_id="rater-a"),
        rating(item_id="task-1.chapter", condition="C_ENGINE", rater_id="rater-a", offset=1),
    ]
    result = paired_task_bootstrap(rows, "A_BASELINE", "C_ENGINE")
    assert result["continuity"]["independent_task_count"] == 1
    assert result["continuity"]["bootstrap_95_ci"] is None
    assert result["continuity"]["status"] == "INSUFFICIENT_TASKS"


def test_blind_packet_cli_writes_separate_reader_and_researcher_files(tmp_path: Path):
    text_directory = tmp_path / "texts"
    text_directory.mkdir()
    texts = {"A_BASELINE": "合成样本甲。", "B_MEMORY": "合成样本乙。", "C_ENGINE": "合成样本丙。"}
    outputs = []
    for condition, content in texts.items():
        relative_path = f"texts/{condition}.md"
        data = content.encode("utf-8")
        (tmp_path / relative_path).write_bytes(data)
        outputs.append({
            "item_id": "task-1.three-chapter-window", "task_id": "task-1", "condition": condition,
            "model_name": "frozen-model", "prompt_fingerprint": f"sha256:{condition}",
            "status": "COMPLETED", "text_path": relative_path, "text_sha256": hashlib.sha256(data).hexdigest(),
            "output_chars": len(content), "attempts": 1, "confirmed_tokens": 20,
            "unknown_usage_calls": 0, "reserved_tokens": 0, "elapsed_seconds": 3,
        })
    record = {
        **frozen_record(),
        "status": "FROZEN",
        "conditions": [
            {"id": "A_BASELINE", "memory": False, "formal_world": False, "critic": False},
            {"id": "B_MEMORY", "memory": True, "formal_world": False, "critic": False},
            {"id": "C_ENGINE", "memory": True, "formal_world": True, "critic": True},
        ],
        "tasks": [{"id": "task-1", "chapters": 3}],
        "outputs": outputs,
    }
    manifest_bytes = b'{"manifest":"synthetic"}\n'
    (tmp_path / "pilot-manifest.json").write_bytes(manifest_bytes)
    record["corpus_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    output_directory = tmp_path / "blind-review"
    script = Path(__file__).parents[1] / "scripts" / "prepare_literary_blind_review.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(record_path), str(output_directory), "--raters", "rater-a,rater-b", "--seed", "12"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    packet = json.loads((output_directory / "reader-packets" / "rater-rater-a.json").read_text(encoding="utf-8"))
    assert len(packet["items"]) == 3
    assert packet["schema_version"] == "blind-reader-packet-v1"
    assert all(set(item) == {"anonymous_text_id", "text"} for item in packet["items"])
    assert not any("condition" in item or "item_id" in item or "task_id" in item for item in packet["items"])
    mapping = json.loads((output_directory / "researcher-only" / "unblinding-map.json").read_text(encoding="utf-8"))
    assert {item["condition"] for item in mapping} == {"A_BASELINE", "B_MEMORY", "C_ENGINE"}
    preparation_summary = json.loads((output_directory / "researcher-only" / "preparation-summary.json").read_text(encoding="utf-8"))
    assert len(preparation_summary["experiment_fingerprint"]) == 64

    protected_directory = tmp_path / "existing-review"
    protected_directory.mkdir()
    sentinel = protected_directory / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    repeated = subprocess.run(
        [sys.executable, str(script), str(record_path), str(protected_directory), "--raters", "rater-a,rater-b", "--seed", "12"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert repeated.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_blind_packet_preparation_rejects_text_changed_after_freeze(tmp_path: Path):
    text_directory = tmp_path / "texts"
    text_directory.mkdir()
    text_path = text_directory / "one.md"
    original = "被冻结的合成文本。".encode("utf-8")
    text_path.write_bytes(original)
    record = {
        **frozen_record(),
        "status": "FROZEN",
        "conditions": [
            {"id": "A_BASELINE", "memory": False, "formal_world": False, "critic": False},
            {"id": "B_MEMORY", "memory": True, "formal_world": False, "critic": False},
            {"id": "C_ENGINE", "memory": True, "formal_world": True, "critic": True},
        ],
        "tasks": [{"id": "task-1", "chapters": 3}],
        "outputs": [{
            "item_id": "task-1.window", "task_id": "task-1", "condition": condition,
            "model_name": "frozen-model", "prompt_fingerprint": f"sha256:{condition}",
            "status": "COMPLETED", "text_path": "texts/one.md", "text_sha256": hashlib.sha256(original).hexdigest(),
            "output_chars": 1, "attempts": 1, "confirmed_tokens": 2,
            "unknown_usage_calls": 0, "reserved_tokens": 0, "elapsed_seconds": 1,
        } for condition in ("A_BASELINE", "B_MEMORY", "C_ENGINE")],
    }
    manifest_bytes = b'{"manifest":"synthetic"}\n'
    (tmp_path / "pilot-manifest.json").write_bytes(manifest_bytes)
    record["corpus_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    text_path.write_text("替换后的文本。", encoding="utf-8")
    output_directory = tmp_path / "blind-review"
    script = Path(__file__).parents[1] / "scripts" / "prepare_literary_blind_review.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(record_path), str(output_directory), "--raters", "rater-a,rater-b", "--seed", "2"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "COMPLETED_OUTPUT_HASH_MISMATCH" in completed.stderr
    assert not output_directory.exists()
