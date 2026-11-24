"""Analyze a frozen literary experiment record without model or database access."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.literary_evaluation import evaluate_experiment, validate_experiment_files


def _model_texts(record: dict, record_path: Path) -> dict[tuple[str, str], str]:
    raw_judgments = record.get("model_judgments")
    if not isinstance(raw_judgments, list) or not raw_judgments:
        return {}
    raw_outputs = record.get("outputs")
    outputs = {
        (row.get("item_id"), row.get("condition")): row
        for row in raw_outputs
        if isinstance(row, dict)
    } if isinstance(raw_outputs, list) else {}
    result: dict[tuple[str, str], str] = {}
    root = record_path.resolve().parent
    for key in {
        (row.get("item_id"), row.get("condition"))
        for row in raw_judgments
        if isinstance(row, dict)
    }:
        if not all(isinstance(value, str) for value in key):
            continue
        output = outputs.get(key)
        if not output or output.get("status") != "COMPLETED":
            continue
        relative_path = output.get("text_path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            continue
        text_path = (root / relative_path).resolve()
        if not text_path.is_relative_to(root):
            raise ValueError("OUTPUT_TEXT_PATH_OUTSIDE_RECORD_DIRECTORY")
        text_bytes = text_path.read_bytes()
        if hashlib.sha256(text_bytes).hexdigest() != output.get("text_sha256"):
            raise ValueError("COMPLETED_OUTPUT_HASH_MISMATCH")
        result[key] = text_bytes.decode("utf-8-sig")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze blind-reader ratings for a literary experiment")
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    record = json.loads(args.record.read_text(encoding="utf-8"))
    try:
        file_issues = validate_experiment_files(record, args.record.resolve().parent)
        if file_issues:
            raise ValueError(f"EXPERIMENT_FILES_INVALID:{file_issues[0]['code']}:{file_issues[0]['field']}")
        report = evaluate_experiment(record, model_texts=_model_texts(record, args.record))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
