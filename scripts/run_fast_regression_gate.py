"""Run the offline canonical-trace Fast Regression Gate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.regression_gate import evaluate_gate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="append", required=True, help="Canonical JSONL trace; repeat for each sample.")
    parser.add_argument("--candidate", action="append", required=True, help="Canonical JSONL trace; repeat for each sample.")
    parser.add_argument("--tolerance", type=float, default=0.05)
    args = parser.parse_args()
    report = evaluate_gate(args.baseline, args.candidate, performance_tolerance=args.tolerance)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 1 if report["conclusion"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
