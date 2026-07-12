#!/usr/bin/env python3
"""Deterministic benchmark for Evidence-Guided Recovery orchestration."""
from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import metrics
from orchestrator.analytics import list_artifacts
from orchestrator.scheduler import Scheduler
from orchestrator.state import create_run_state, save_state, state_path_for_workspace


@dataclass(frozen=True)
class Scenario:
    name: str
    failure_text: str
    max_rounds: int
    pass_condition: str


SCENARIOS = [
    Scenario(
        "test failure after initial implementation",
        "Average: 4/10\nFAILED tests/test_app.py::test_feature - AssertionError\napp.py:3",
        3,
        "any_evidence",
    ),
    Scenario(
        "syntax/build failure",
        "Average: 3/10\nSyntaxError: invalid syntax\nFile \"app.py\", line 2",
        3,
        "any_evidence",
    ),
    Scenario(
        "wrong assumption requiring reinspection",
        "Average: 4/10\nFAILED tests/test_parser.py::test_edge_case - AssertionError: wrong assumption\nparser.py:12",
        4,
        "reinspect_assumptions",
    ),
    Scenario(
        "repeated identical failure",
        "Average: 2/10\nFAILED tests/test_loop.py::test_same_failure - AssertionError\nloop.py:5",
        3,
        "never",
    ),
    Scenario(
        "successful first-pass task",
        "Average: 9/10\nAll checks passed.",
        2,
        "first_pass",
    ),
]


class ScenarioRunner:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    def contract(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    def build(self, state: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(state["workspace"])
        evidence = state.get("current_failure_evidence") or {}
        marker = {
            "round": state.get("round_num"),
            "evidence_seen": bool(evidence),
            "strategy": evidence.get("recovery_strategy"),
        }
        (workspace / "build_marker.json").write_text(json.dumps(marker), encoding="utf-8")
        state["artifacts"] = {"files": list_artifacts(workspace)}
        return state

    def evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(state["workspace"])
        passed = self._passed(state)
        feedback = "Average: 9/10\nAll checks passed." if passed else self.scenario.failure_text
        (workspace / config.FEEDBACK_FILE).write_text(feedback, encoding="utf-8")
        state.setdefault("score_history", []).append(9.0 if passed else _score_from_feedback(feedback))
        state["artifacts"] = {"files": list_artifacts(workspace)}
        return state

    def _passed(self, state: dict[str, Any]) -> bool:
        condition = self.scenario.pass_condition
        evidence = state.get("current_failure_evidence") or {}
        if condition == "first_pass":
            return True
        if condition == "any_evidence":
            return bool(evidence)
        if condition == "reinspect_assumptions":
            return evidence.get("recovery_strategy") == "reinspect_assumptions"
        return False


def main() -> int:
    root = Path("benchmark_runs/evidence_recovery").resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    baseline = _run_variant(root, "baseline", enabled=False)
    candidate = _run_variant(root, "evidence-guided-recovery", enabled=True)
    report = {
        "scenario_count": len(SCENARIOS),
        "baseline": baseline,
        "candidate": candidate,
        "comparison": _compare(baseline["aggregate"], candidate["aggregate"]),
    }
    output = Path("benchmark_runs/evidence_recovery_report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(_markdown_table(report))
    print(f"\nReport saved to {output}")
    return 0


def _run_variant(root: Path, name: str, *, enabled: bool) -> dict[str, Any]:
    previous_flag = config.HARNESS_EVIDENCE_GUIDED_RECOVERY
    config.HARNESS_EVIDENCE_GUIDED_RECOVERY = enabled
    runs = []
    try:
        for scenario in SCENARIOS:
            workspace = root / name / _slug(scenario.name)
            workspace.mkdir(parents=True, exist_ok=True)
            state = create_run_state(scenario.name, workspace, profile="terminal", run_id=f"{name}-{_slug(scenario.name)}")
            state["phase"] = "build"
            state["next_action"] = "build"
            state["max_rounds"] = scenario.max_rounds
            state_path = state_path_for_workspace(workspace)
            save_state(state_path, state)
            metrics.RECORDER = metrics.MetricsRecorder()

            started = time.perf_counter()
            final_state = Scheduler(state_path, phase_runner=ScenarioRunner(scenario)).run_until_idle(max_steps=20)
            wall_ms = int((time.perf_counter() - started) * 1000)
            summary = metrics.RECORDER.data.get("summary") or {}
            recovery = final_state.get("recovery") or {}
            success = bool(final_state.get("score_history") and float(final_state["score_history"][-1]) >= 8.0)
            scores = [float(score) for score in final_state.get("score_history") or []]
            fallback_failed = sum(1 for score in scores if score < 8.0)
            fallback_retries = sum(1 for idx, score in enumerate(scores) if score < 8.0 and idx < len(scores) - 1)
            fallback_repeated = max(0, fallback_failed - 1) if scenario.pass_condition != "first_pass" else 0
            recovery_attempt_count = int(
                recovery.get("recovery_attempt_count")
                or summary.get("recovery_attempt_count")
                or fallback_retries
                or 0
            )
            runs.append({
                "scenario": scenario.name,
                "success": success,
                "first_pass": success and len(final_state.get("score_history") or []) == 1,
                "rounds": int(final_state.get("round_num") or 0),
                "wall_time_ms": wall_ms,
                "failed_attempt_count": int(recovery.get("failed_attempt_count") or summary.get("failed_attempt_count") or fallback_failed or 0),
                "recovery_attempt_count": recovery_attempt_count,
                "recovery_success_count": 1 if success and recovery_attempt_count > 0 else 0,
                "repeated_failure_count": int(
                    recovery.get("repeated_failure_count")
                    or summary.get("repeated_failure_count")
                    or fallback_repeated
                    or 0
                ),
                "same_failure_escalation_count": int(recovery.get("same_failure_escalation_count") or summary.get("same_failure_escalation_count") or 0),
                "agent_rounds": int(summary.get("agent_rounds") or final_state.get("round_num") or 0),
                "total_tokens": int(summary.get("total_tokens") or 0),
                "llm_call_count": int(summary.get("llm_call_count") or 0),
            })
    finally:
        config.HARNESS_EVIDENCE_GUIDED_RECOVERY = previous_flag
    return {"runs": runs, "aggregate": _aggregate(runs)}


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    success_count = sum(1 for run in runs if run["success"])
    recovered = sum(1 for run in runs if run["recovery_success_count"])
    recovery_attempts = sum(run["recovery_attempt_count"] for run in runs)
    repeated = sum(run["repeated_failure_count"] for run in runs)
    failed = sum(run["failed_attempt_count"] for run in runs)

    def per_success(total: int) -> float:
        return total / success_count if success_count else 0.0

    return {
        "final_success_rate": success_count / len(runs) if runs else 0.0,
        "first_pass_success_rate": sum(1 for run in runs if run["first_pass"]) / len(runs) if runs else 0.0,
        "recovery_success_rate": recovered / recovery_attempts if recovery_attempts else 0.0,
        "repeated_same_failure_rate": repeated / failed if failed else 0.0,
        "avg_retries_per_recovered_task": recovery_attempts / recovered if recovered else 0.0,
        "agent_rounds_per_success": per_success(sum(run["agent_rounds"] for run in runs)),
        "tokens_per_success": per_success(sum(run["total_tokens"] for run in runs)),
        "wall_time_per_success_ms": per_success(sum(run["wall_time_ms"] for run in runs)),
        "llm_call_count": sum(run["llm_call_count"] for run in runs),
        "same_failure_escalation_count": sum(run["same_failure_escalation_count"] for run in runs),
    }


def _compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, dict[str, float]]:
    metrics_to_compare = [
        "final_success_rate",
        "first_pass_success_rate",
        "recovery_success_rate",
        "repeated_same_failure_rate",
        "avg_retries_per_recovered_task",
        "agent_rounds_per_success",
        "tokens_per_success",
        "wall_time_per_success_ms",
    ]
    return {
        key: {
            "baseline": baseline.get(key, 0.0),
            "evidence_guided_recovery": candidate.get(key, 0.0),
            "delta": candidate.get(key, 0.0) - baseline.get(key, 0.0),
        }
        for key in metrics_to_compare
    }


def _markdown_table(report: dict[str, Any]) -> str:
    labels = {
        "final_success_rate": "Final success rate",
        "first_pass_success_rate": "First-pass success rate",
        "recovery_success_rate": "Recovery success rate",
        "repeated_same_failure_rate": "Repeated same-failure rate",
        "avg_retries_per_recovered_task": "Avg retries per recovered task",
        "agent_rounds_per_success": "Agent rounds / success",
        "tokens_per_success": "Tokens / success",
        "wall_time_per_success_ms": "Wall time / success",
    }
    rows = [
        "| Metric | Baseline | Evidence-Guided Recovery |",
        "| --- | ---: | ---: |",
    ]
    for key, label in labels.items():
        row = report["comparison"][key]
        rows.append(f"| {label} | {_fmt(row['baseline'])} | {_fmt(row['evidence_guided_recovery'])} |")
    return "\n".join(rows)


def _score_from_feedback(text: str) -> float:
    import re

    match = re.search(r"Average:\s*(\d+\.?\d*)\s*/\s*10", text)
    return float(match.group(1)) if match else 0.0


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
