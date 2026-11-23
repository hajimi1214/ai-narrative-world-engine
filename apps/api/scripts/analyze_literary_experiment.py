"""Analyze a frozen literary experiment record without model or database access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.literary_evaluation import condition_means, evaluate_experiment, paired_deltas


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze blind-reader ratings for a literary experiment")
    parser.add_argument("record", type=Path)
    parser.add_argument("--baseline", default="A_BASELINE")
    parser.add_argument("--comparison", default="C_ENGINE")
    args = parser.parse_args()
    record = json.loads(args.record.read_text(encoding="utf-8"))
    ratings = record.get("human_ratings") or []
    report = evaluate_experiment(record)
    report["condition_means"] = condition_means(ratings)
    report["paired_deltas"] = paired_deltas(ratings, args.baseline, args.comparison)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
