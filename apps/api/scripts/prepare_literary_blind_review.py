"""Prepare condition-masked Chinese-reader packets from a frozen experiment record."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.literary_evaluation import experiment_fingerprint, prepare_blind_assignments, validate_experiment_files, validate_experiment_record


def _read_outputs(record_path: Path, outputs: list[dict]) -> list[dict]:
    root = record_path.resolve().parent
    completed = []
    for row in outputs:
        if row.get("status") != "COMPLETED":
            continue
        relative_path = row.get("text_path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("COMPLETED_OUTPUT_TEXT_PATH_REQUIRED")
        text_path = (root / relative_path).resolve()
        if not text_path.is_relative_to(root):
            raise ValueError("OUTPUT_TEXT_PATH_OUTSIDE_RECORD_DIRECTORY")
        data = text_path.read_bytes()
        expected_hash = row.get("text_sha256")
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("COMPLETED_OUTPUT_HASH_MISMATCH")
        text = data.decode("utf-8-sig")
        if not text.strip():
            raise ValueError("COMPLETED_OUTPUT_TEXT_EMPTY")
        if len(text) != row.get("output_chars"):
            raise ValueError("COMPLETED_OUTPUT_LENGTH_MISMATCH")
        completed.append({**row, "text": text})
    if not completed:
        raise ValueError("NO_COMPLETED_OUTPUTS")
    return completed


def prepare_packets(record_path: Path, output_directory: Path, rater_ids: list[str], seed: int) -> dict[str, int]:
    record_path = record_path.resolve()
    output_directory = output_directory.resolve()
    if not record_path.is_file():
        raise ValueError("EXPERIMENT_RECORD_NOT_FOUND")
    if output_directory.exists():
        raise ValueError("OUTPUT_DIRECTORY_ALREADY_EXISTS")
    if not output_directory.parent.is_dir():
        raise ValueError("OUTPUT_PARENT_DIRECTORY_NOT_FOUND")
    if not rater_ids or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) for item in rater_ids):
        raise ValueError("RATER_ID_INVALID")

    record = json.loads(record_path.read_text(encoding="utf-8"))
    if str(record.get("status") or "").upper() not in {"FROZEN", "COMPLETE", "COMPLETED_WITH_FAILURES"}:
        raise ValueError("EXPERIMENT_NOT_FROZEN")
    design_issues = validate_experiment_record(record)
    if design_issues:
        raise ValueError(f"EXPERIMENT_RECORD_INVALID:{design_issues[0]['code']}:{design_issues[0]['field']}")
    file_issues = validate_experiment_files(record, record_path.parent)
    if file_issues:
        raise ValueError(f"EXPERIMENT_FILES_INVALID:{file_issues[0]['code']}:{file_issues[0]['field']}")
    outputs = _read_outputs(record_path, record.get("outputs") or [])
    assignment = prepare_blind_assignments(outputs, rater_ids, seed=seed)
    text_by_source = {(row["item_id"], row["condition"]): row["text"] for row in outputs}
    text_by_id = {
        row["anonymous_text_id"]: text_by_source[(row["item_id"], row["condition"])]
        for row in assignment["unblinding_map"]
    }
    temporary_directory = Path(tempfile.mkdtemp(prefix=f".{output_directory.name}-", dir=output_directory.parent))
    output_created = False
    try:
        packets_dir = temporary_directory / "reader-packets"
        researcher_dir = temporary_directory / "researcher-only"
        packets_dir.mkdir()
        researcher_dir.mkdir()
        for packet in assignment["assignments"]:
            items = [
                {"anonymous_text_id": anonymous_id, "text": text_by_id[anonymous_id]}
                for anonymous_id in packet["order"]
            ]
            path = packets_dir / f"rater-{packet['rater_id']}.json"
            path.write_text(json.dumps({"schema_version": "blind-reader-packet-v1", "rater_id": packet["rater_id"], "items": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        mapping_path = researcher_dir / "unblinding-map.json"
        mapping_path.write_text(json.dumps(assignment["unblinding_map"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assignment_path = researcher_dir / "blind-assignments.json"
        assignment_path.write_text(json.dumps(assignment["assignments"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        counts = {
            "experiment_fingerprint": experiment_fingerprint(record),
            "packet_count": len(rater_ids),
            "completed_text_count": len(outputs),
            "noncompleted_output_count": len(record.get("outputs") or []) - len(outputs),
        }
        summary_path = researcher_dir / "preparation-summary.json"
        summary_path.write_text(json.dumps({"seed": seed, **counts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_directory.mkdir()
        output_created = True
        for source in (packets_dir, researcher_dir):
            os.replace(source, output_directory / source.name)
        temporary_directory.rmdir()
        return counts
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        if output_created:
            shutil.rmtree(output_directory, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="Frozen experiment record with output text_path entries")
    parser.add_argument("output_directory", type=Path, help="New directory for reader packets and private unblinding map")
    parser.add_argument("--raters", required=True, help="Comma-separated blinded rater IDs")
    parser.add_argument("--seed", type=int, required=True, help="Frozen integer seed used to randomize IDs and order")
    args = parser.parse_args()
    try:
        counts = prepare_packets(args.record, args.output_directory, [item.strip() for item in args.raters.split(",") if item.strip()], args.seed)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
