#!/usr/bin/env python3
"""Run a baseline-only token and latency attribution audit."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


TOKEN_LABELS = [
    ("static_system_prompt", "Static/system prompt"),
    ("dynamic_task_state", "Dynamic task/state"),
    ("assistant_history", "Assistant history"),
    ("tool_results", "Tool results"),
    ("tool_call_arguments", "Tool-call arguments"),
    ("middleware_injections", "Middleware injections"),
    ("compression_reset", "Compression/reset"),
    ("other", "Other"),
]

LATENCY_LABELS = [
    ("llm_calls_ms", "LLM calls"),
    ("tool_execution_ms", "Tool execution"),
    ("context_processing_ms", "Context processing"),
    ("middleware_ms", "Middleware"),
    ("compression_reset_ms", "Compression/reset"),
    ("scheduler_state_transitions_ms", "Scheduler/state transitions"),
    ("state_file_io_ms", "State file I/O"),
    ("trace_log_persistence_ms", "Trace/log persistence"),
    ("agent_initialization_ms", "Agent initialization"),
    ("api_preflight_ms", "API preflight"),
    ("explicit_sleep_polling_ms", "Explicit sleep/polling"),
    ("benchmark_harness_overhead_ms", "Benchmark harness overhead"),
    ("orchestration_overhead_ms", "Unclassified gaps"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", required=True)
    parser.add_argument("--profile", default="terminal")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--workspace-root", default="benchmark_runs/baseline_attribution")
    parser.add_argument("--output", default="benchmark_runs/baseline_attribution_report.json")
    args = parser.parse_args()

    root = Path(args.workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    runs = []
    for repeat_idx in range(args.repeat):
        for task_idx, task in enumerate(args.task):
            workspace = root / f"r{repeat_idx + 1}_t{task_idx + 1}"
            if workspace.exists():
                _rmtree(workspace)
            workspace.mkdir(parents=True)
            runs.append(_run_once(args, task, workspace))

    report = {
        "profile": args.profile,
        "repeat": args.repeat,
        "task_count": len(args.task),
        "runs": runs,
        "aggregate": _aggregate(runs),
        "static_prompt_duplication": _static_prompt_duplication_audit(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(_format_report(report))
    print(f"\nReport saved to {output}")
    return 0


def _run_once(args: argparse.Namespace, task: str, workspace: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["HARNESS_DOTENV_OVERRIDE_ENV"] = "0"
    env["HARNESS_WORKSPACE"] = str(workspace)
    env["HARNESS_FLAT_WORKSPACE"] = "1"
    env["HARNESS_METRICS_ENABLED"] = "1"
    env["HARNESS_PROMPT_PREFIX_V2"] = "0"
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
    subprocess_wall_ms = int((time.perf_counter() - started) * 1000)
    metrics_path = workspace / ".harness" / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    return {
        "task": task,
        "workspace": str(workspace),
        "returncode": proc.returncode,
        "subprocess_wall_time_ms": subprocess_wall_ms,
        "metrics_path": str(metrics_path) if metrics_path.exists() else None,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
        "metrics": metrics,
    }


def _rmtree(path: Path) -> None:
    def on_error(_func, failing_path, _exc_info):
        try:
            os.chmod(failing_path, 0o700)
            os.unlink(failing_path)
        except Exception:
            pass

    shutil.rmtree(path, onerror=on_error)


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [(run.get("metrics") or {}).get("summary") or {} for run in runs]
    token_totals = {key: 0 for key, _label in TOKEN_LABELS}
    latency_totals = {key: 0 for key, _label in LATENCY_LABELS}
    llm_by_phase_role: dict[str, dict[str, int]] = {}
    middleware_totals: dict[str, dict[str, int]] = {}
    low_progress_calls = []
    call_sequences = []
    total_input = 0
    wall = 0
    for run, summary in zip(runs, summaries):
        total_input += int(summary.get("total_input_tokens") or 0)
        wall += int(run.get("subprocess_wall_time_ms") or (run.get("metrics") or {}).get("total_run_wall_time_ms") or 0)
        for key, _label in TOKEN_LABELS:
            token_totals[key] += int((summary.get("token_attribution") or {}).get(key) or 0)
        for key, _label in LATENCY_LABELS:
            latency_totals[key] += int((summary.get("latency_attribution") or {}).get(key) or 0)
        metrics_wall = int((run.get("metrics") or {}).get("total_run_wall_time_ms") or 0)
        latency_totals["benchmark_harness_overhead_ms"] = latency_totals.get("benchmark_harness_overhead_ms", 0) + max(
            0,
            int(run.get("subprocess_wall_time_ms") or 0) - metrics_wall,
        )
        call_sequences.append(summary.get("llm_call_sequence") or [])
        _merge_phase_role(llm_by_phase_role, summary.get("llm_by_phase_role") or {})
        _merge_middleware(middleware_totals, summary.get("middleware_attribution") or {})
        low_progress_calls.extend(_classify_low_progress_calls(run))
    return {
        "run_count": len(runs),
        "success_count": sum(1 for run in runs if (run.get("metrics") or {}).get("task_success") is True),
        "total_input_tokens": total_input,
        "total_wall_time_ms": wall,
        "token_attribution": token_totals,
        "latency_attribution": latency_totals,
        "llm_by_phase_role": llm_by_phase_role,
        "llm_call_sequences": call_sequences,
        "low_progress_calls": low_progress_calls,
        "middleware_attribution": middleware_totals,
    }


def _format_report(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    total_input = aggregate["total_input_tokens"]
    wall = aggregate["total_wall_time_ms"]
    lines = [
        "| Category | Tokens | % of Input Tokens |",
        "| --- | ---: | ---: |",
    ]
    for key, label in TOKEN_LABELS:
        value = aggregate["token_attribution"][key]
        lines.append(f"| {label} | {value} | {_pct(value, total_input)} |")
    lines.extend(["", "| Latency Category | Total Time | % of Wall Time |", "| --- | ---: | ---: |"])
    for key, label in LATENCY_LABELS:
        value = aggregate["latency_attribution"][key]
        lines.append(f"| {label} | {value / 1000:.3f}s | {_pct(value, wall)} |")
    lines.extend(["", "| Phase/Role | Calls | Input Tokens | Cached Tokens | Uncached Tokens | Output Tokens | Total LLM Time |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for label in ["Router", "Planner", "Contract", "Builder", "Evaluator", "Analyze", "Summarizer", "Other"]:
        row = aggregate["llm_by_phase_role"].get(label, {})
        lines.append(
            f"| {label} | {row.get('calls', 0)} | {row.get('input_tokens', 0)} | "
            f"{row.get('cached_tokens', 0)} | {row.get('uncached_tokens', 0)} | "
            f"{row.get('output_tokens', 0)} | {row.get('total_llm_time_ms', 0) / 1000:.3f}s |"
        )
    lines.append("")
    lines.append("LLM call sequences:")
    for idx, sequence in enumerate(aggregate["llm_call_sequences"], 1):
        display = " -> ".join(_sequence_label(item) for item in sequence) if sequence else "(none)"
        lines.append(f"- Run {idx}: {display}")
    lines.extend(["", "| Middleware | Injection Count | Injected Tokens | Repeated Identical/Near-identical Injections |", "| --- | ---: | ---: | ---: |"])
    for middleware, row in sorted(aggregate["middleware_attribution"].items()):
        lines.append(
            f"| {middleware} | {row.get('injection_count', 0)} | {row.get('injected_tokens', 0)} | "
            f"{row.get('repeated_identical_or_near_identical', 0)} |"
        )
    lines.extend(["", "| Duplicate Group | Locations | Estimated Repeated Tokens | Risk of Removal |", "| --- | --- | ---: | --- |"])
    for row in report.get("static_prompt_duplication") or []:
        lines.append(
            f"| {row['duplicate_group']} | {row['locations']} | {row['estimated_repeated_tokens']} | {row['risk_of_removal']} |"
        )
    lines.append("")
    lines.append("Potential no-progress / low-progress LLM calls:")
    if aggregate["low_progress_calls"]:
        for call in aggregate["low_progress_calls"][:20]:
            lines.append(
                f"- {call['run']} call {call['call_index']} {call['phase_role']}: {call['reason']} "
                f"(input={call['input_tokens']}, output={call['output_tokens']}, latency={call['latency_ms']}ms)"
            )
    else:
        lines.append("- None detected by programmatic heuristics.")
    return "\n".join(lines)


def _pct(value: int | float, total: int | float) -> str:
    return "0.00%" if not total else f"{(value / total) * 100:.2f}%"


def _merge_phase_role(target: dict[str, dict[str, int]], source: dict[str, dict[str, int]]) -> None:
    for label, row in source.items():
        bucket = target.setdefault(label, {
            "calls": 0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "uncached_tokens": 0,
            "output_tokens": 0,
            "total_llm_time_ms": 0,
        })
        for key in bucket:
            bucket[key] += int(row.get(key) or 0)


def _merge_middleware(target: dict[str, dict[str, int]], source: dict[str, dict[str, int]]) -> None:
    for label, row in source.items():
        bucket = target.setdefault(label, {
            "injection_count": 0,
            "injected_tokens": 0,
            "repeated_identical_or_near_identical": 0,
        })
        for key in bucket:
            bucket[key] += int(row.get(key) or 0)


def _classify_low_progress_calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    metrics = run.get("metrics") or {}
    for call in metrics.get("llm_calls") or []:
        progress = call.get("progress") or {}
        reasons = []
        if not progress.get("new_effective_tool_call"):
            reasons.append("no tool call")
        if progress.get("repeated_tool_call"):
            reasons.append("repeated tool call")
        if not any([
            progress.get("new_effective_tool_call"),
            progress.get("workspace_modified"),
            progress.get("failure_evidence_found"),
            progress.get("phase_advanced"),
            progress.get("task_acceptance_status_changed"),
        ]):
            reasons.append("no programmatic progress signal")
        if reasons:
            result.append({
                "run": Path(run.get("workspace", "")).name,
                "call_index": call.get("call_index"),
                "phase_role": _sequence_label(f"{call.get('phase') or 'other'}:{call.get('role') or 'other'}"),
                "reason": ", ".join(sorted(set(reasons))),
                "input_tokens": call.get("input_tokens", 0),
                "output_tokens": call.get("output_tokens", 0),
                "latency_ms": call.get("request_latency_ms", 0),
            })
    return result


def _sequence_label(item: str) -> str:
    value = item.lower()
    if "route" in value or "router" in value:
        return "Router"
    if "plan" in value or "planner" in value:
        return "Planner"
    if "contract" in value:
        return "Contract"
    if "build" in value or "builder" in value:
        return "Builder"
    if "evaluate" in value or "evaluator" in value:
        return "Evaluator"
    if "analyze" in value:
        return "Analyze"
    if "summarizer" in value:
        return "Summarizer"
    return "Other"


def _static_prompt_duplication_audit() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    prompts_text = (root / "prompts.py").read_text(encoding="utf-8", errors="replace")
    terminal_text = (root / "profiles" / "terminal.py").read_text(encoding="utf-8", errors="replace")
    middleware_text = (root / "middlewares.py").read_text(encoding="utf-8", errors="replace")
    tools_text = (root / "tools.py").read_text(encoding="utf-8", errors="replace")
    skills_count = len(list((root / "skills").glob("*/SKILL.md")))
    rows = []
    checks = [
        (
            "Verification instructions repeated",
            ["verify", "verification", "test", "correct"],
            {
                "prompts.py": prompts_text,
                "profiles/terminal.py": terminal_text,
                "middlewares.py": middleware_text,
            },
            "Medium",
        ),
        (
            "Tool usage instructions repeated",
            ["run_bash", "read_file", "write_file", "tool"],
            {
                "prompts.py": prompts_text,
                "profiles/terminal.py": terminal_text,
                "tools.py": tools_text,
            },
            "High",
        ),
        (
            "No TODO/placeholders repeated",
            ["TODO", "placeholder", "NotImplementedError"],
            {
                "prompts.py": prompts_text,
                "profiles/terminal.py": terminal_text,
                "middlewares.py": middleware_text,
            },
            "Medium",
        ),
        (
            "Autonomous/no talking rule repeated",
            ["NEVER ask", "STOP TALKING", "actually DO", "Just DO"],
            {
                "profiles/terminal.py": terminal_text,
                "middlewares.py": middleware_text,
            },
            "High",
        ),
        (
            "Skill catalog available to broad roles",
            ["Available Skills", "SKILL.md", "skill"],
            {
                "skills catalog": "\n".join(str(p) for p in (root / "skills").glob("*/SKILL.md")),
                "harness.py": (root / "harness.py").read_text(encoding="utf-8", errors="replace"),
            },
            "Medium",
        ),
    ]
    for group, markers, locations, risk in checks:
        hit_locations = []
        repeated_tokens = 0
        for location, text in locations.items():
            hits = sum(len(re.findall(re.escape(marker), text, flags=re.IGNORECASE)) for marker in markers)
            if hits:
                hit_locations.append(f"{location} ({hits} hits)")
                repeated_tokens += max(0, hits - 1) * 8
        if group.startswith("Skill catalog"):
            repeated_tokens += skills_count * 18
        rows.append({
            "duplicate_group": group,
            "locations": "; ".join(hit_locations) or "not detected",
            "estimated_repeated_tokens": repeated_tokens,
            "risk_of_removal": risk,
        })
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
