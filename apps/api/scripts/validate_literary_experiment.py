"""Validate a frozen literary comparison record before unblinding or analysis."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.literary_evaluation import experiment_fingerprint, validate_experiment_files, validate_experiment_record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    record = json.loads(args.record.read_text(encoding="utf-8"))
    issues = [*validate_experiment_record(record), *validate_experiment_files(record, args.record.resolve().parent)]
    report = {"valid": not issues, "experiment_status": record.get("status", "PLANNED"), "fingerprint": experiment_fingerprint(record), "issues": issues}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
