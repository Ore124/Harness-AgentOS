#!/usr/bin/env python3
"""Run baseline vs Phase 1 prompt-prefix benchmark comparisons."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


METRICS = [
    "success_rate",
    "total_input_tokens",
    "total_cached_tokens",
    "total_output_tokens",
    "total_tokens_per_success",
    "llm_calls_per_success",
    "agent_rounds_per_success",
    "tool_calls_per_success",
    "wall_time_per_success_ms",
    "p50_latency_ms",
    "p95_latency_ms",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", required=True, help="Task prompt. Repeat for multiple tasks.")
    parser.add_argument("--profile", default="terminal")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--workspace-root", default="benchmark_runs/phase1")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", default="benchmark_runs/phase1_report.json")
    args = parser.parse_args()

    root = Path(args.workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    baseline = _run_variant("baseline", False, args, root)
    candidate = _run_variant("prompt-prefix-v2", True, args, root)
    report = {
        "profile": args.profile,
        "repeat": args.repeat,
        "task_count": len(args.task),
        "baseline": baseline,
        "candidate": candidate,
        "comparison": _compare(baseline["aggregate"], candidate["aggregate"]),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(_markdown_table(report))
    print(f"\nReport saved to {output}")
    return 0


def _run_variant(name: str, prompt_prefix_v2: bool, args: argparse.Namespace, root: Path) -> dict[str, Any]:
    runs = []
    for repeat_idx in range(args.repeat):
        for task_idx, task in enumerate(args.task):
            workspace = root / name / f"r{repeat_idx + 1}_t{task_idx + 1}"
            if workspace.exists():
                shutil.rmtree(workspace)
            workspace.mkdir(parents=True)
            env = os.environ.copy()
            env["HARNESS_WORKSPACE"] = str(workspace)
            env["HARNESS_DOTENV_OVERRIDE_ENV"] = "0"
            env["HARNESS_FLAT_WORKSPACE"] = "1"
            env["HARNESS_METRICS_ENABLED"] = "1"
            env["HARNESS_PROMPT_PREFIX_V2"] = "1" if prompt_prefix_v2 else "0"
            env["HARNESS_DETERMINISTIC_OUTPUT_COMPRESSION"] = "0"
            env["HARNESS_TOOL_CACHE"] = "0"
            env["HARNESS_STATE_VECTOR"] = "0"
            env["HARNESS_TOKEN_GOVERNOR"] = "0"
            env["HARNESS_PARALLEL_READ_TOOLS"] = "0"

            started = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, "harness.py", "--profile", args.profile, task],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            wall_ms = int((time.perf_counter() - started) * 1000)
            metrics_path = workspace / ".harness" / "metrics.json"
            metrics = {}
            if metrics_path.exists():
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            runs.append({
                "task": task,
                "workspace": str(workspace),
                "returncode": proc.returncode,
                "wall_time_ms": wall_ms,
                "metrics_path": str(metrics_path) if metrics_path.exists() else None,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
                "metrics": metrics,
            })
    return {"runs": runs, "aggregate": _aggregate(runs)}


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [(run.get("metrics") or {}).get("summary") or {} for run in runs]
    successes = [run for run in runs if (run.get("metrics") or {}).get("task_success") is True]
    success_count = len(successes)
    latencies = [int(call.get("request_latency_ms") or 0) for run in runs for call in (run.get("metrics") or {}).get("llm_calls", [])]

    def total(key: str) -> int:
        return sum(int(summary.get(key) or 0) for summary in summaries)

    def per_success(value: int | float) -> float:
        return (value / success_count) if success_count else 0.0

    total_tokens = total("total_tokens")
    wall_time = sum(int(run.get("wall_time_ms") or 0) for run in runs)
    return {
        "run_count": len(runs),
        "success_count": success_count,
        "success_rate": success_count / len(runs) if runs else 0.0,
        "total_input_tokens": total("total_input_tokens"),
        "total_cached_tokens": total("total_cached_tokens"),
        "total_output_tokens": total("total_output_tokens"),
        "total_tokens": total_tokens,
        "total_tokens_per_success": per_success(total_tokens),
        "llm_calls_per_success": per_success(total("llm_call_count")),
        "agent_rounds_per_success": per_success(total("agent_rounds")),
        "tool_calls_per_success": per_success(total("tool_call_count")),
        "wall_time_per_success_ms": per_success(wall_time),
        "p50_latency_ms": statistics.median(latencies) if latencies else 0.0,
        "p95_latency_ms": _percentile(latencies, 95),
        "cache_hit_ratio": (
            total("total_cached_tokens") / total("total_input_tokens")
            if total("total_input_tokens")
            else 0.0
        ),
    }


def _compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {
            "baseline": baseline.get(key, 0),
            "candidate": candidate.get(key, 0),
            "delta": candidate.get(key, 0) - baseline.get(key, 0),
        }
        for key in METRICS
    }


def _percentile(values: list[int], percentile: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = int(round((percentile / 100) * (len(sorted_values) - 1)))
    return float(sorted_values[index])


def _markdown_table(report: dict[str, Any]) -> str:
    rows = ["| Metric | Baseline | Candidate | Delta |", "| --- | ---: | ---: | ---: |"]
    for metric, values in report["comparison"].items():
        rows.append(
            f"| {metric} | {_fmt(values['baseline'])} | {_fmt(values['candidate'])} | {_fmt(values['delta'])} |"
        )
    return "\n".join(rows)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
