#!/usr/bin/env python3
"""Repeatable OFF/ON benchmark for the acceptance progress controller.

The benchmark deliberately keeps independent acceptance outside the agent run.
Every pair starts from byte-identical fixtures and only changes
``HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.verification import is_verification_command


DEFAULT_MANIFEST = Path(__file__).with_name("acceptance_longtask_scenarios.json")
VARIANTS = (("off", False), ("on", True))
SAFE_ENV_PREFIXES = ("HARNESS_", "PROFILE_")
SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    suites: tuple[str, ...]
    categories: tuple[str, ...]
    profile: str
    prompt: str
    setup_files: dict[str, str]
    acceptance_command: tuple[str, ...]
    timeout_seconds: int
    acceptance_timeout_seconds: int
    environment: dict[str, str]
    target_repair_rounds: tuple[int, int] | None = None
    resume_interrupt_seconds: int | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "full"), default="smoke")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Run only this scenario id (repeatable). Overrides --suite selection.",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workspace-root", type=Path, default=Path("benchmark_runs/acceptance_longtask"))
    parser.add_argument("--output", type=Path, default=Path("benchmark_runs/acceptance_longtask_report.json"))
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override per-run timeout in seconds. Larger samples still require --suite full.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    if args.timeout is not None and args.timeout < 1:
        raise SystemExit("--timeout must be at least 1 second")

    scenarios = load_scenarios(args.manifest)
    selected = select_scenarios(scenarios, suite=args.suite, ids=args.scenario)
    root = args.workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    runs: list[dict[str, Any]] = []
    for repeat_index in range(1, args.repeat + 1):
        for scenario in selected:
            pair = run_pair(
                scenario,
                repeat_index=repeat_index,
                root=root,
                timeout_override=args.timeout,
            )
            runs.extend(pair)
            _print_pair(pair)

    report = build_report(
        runs,
        suite=args.suite,
        repeat=args.repeat,
        selected_scenarios=selected,
        manifest=args.manifest,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n" + format_summary(report))
    print(f"\nReport saved to {output}")
    return 0


def load_scenarios(path: Path) -> list[Scenario]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("manifest must contain a non-empty scenarios list")

    scenarios = [_parse_scenario(item) for item in raw_scenarios]
    ids = [item.scenario_id for item in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be unique")
    return scenarios


def _parse_scenario(raw: dict[str, Any]) -> Scenario:
    required = ("id", "suites", "categories", "profile", "prompt", "setup_files", "acceptance_command")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"scenario missing required fields: {', '.join(missing)}")

    setup_files = raw["setup_files"]
    if not isinstance(setup_files, dict):
        raise ValueError(f"{raw['id']}: setup_files must be an object")
    for relative_path, content in setup_files.items():
        _safe_relative_path(str(relative_path))
        if not isinstance(content, str):
            raise ValueError(f"{raw['id']}: setup file contents must be strings")

    command = raw["acceptance_command"]
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError(f"{raw['id']}: acceptance_command must be a non-empty string list")

    environment = {str(key): str(value) for key, value in (raw.get("environment") or {}).items()}
    for key in environment:
        if not key.startswith(SAFE_ENV_PREFIXES) or SECRET_NAME.search(key):
            raise ValueError(f"{raw['id']}: unsafe benchmark environment key: {key}")

    target = raw.get("target_repair_rounds")
    target_tuple = None
    if target is not None:
        if not isinstance(target, list) or len(target) != 2 or not all(isinstance(v, int) for v in target):
            raise ValueError(f"{raw['id']}: target_repair_rounds must be [min, max]")
        if target[0] < 0 or target[1] < target[0]:
            raise ValueError(f"{raw['id']}: invalid target_repair_rounds range")
        target_tuple = (target[0], target[1])

    resume = raw.get("resume") or {}
    interrupt = resume.get("interrupt_after_seconds")
    if interrupt is not None and (not isinstance(interrupt, int) or interrupt < 1):
        raise ValueError(f"{raw['id']}: resume interrupt must be a positive integer")

    return Scenario(
        scenario_id=str(raw["id"]),
        suites=tuple(str(value) for value in raw["suites"]),
        categories=tuple(str(value) for value in raw["categories"]),
        profile=str(raw["profile"]),
        prompt=str(raw["prompt"]),
        setup_files={str(key): value for key, value in setup_files.items()},
        acceptance_command=tuple(command),
        timeout_seconds=int(raw.get("timeout_seconds") or 900),
        acceptance_timeout_seconds=int(raw.get("acceptance_timeout_seconds") or 120),
        environment=environment,
        target_repair_rounds=target_tuple,
        resume_interrupt_seconds=interrupt,
    )


def select_scenarios(scenarios: list[Scenario], *, suite: str, ids: Iterable[str]) -> list[Scenario]:
    selected_ids = list(ids)
    if selected_ids:
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("duplicate --scenario ids are not allowed")
        by_id = {scenario.scenario_id: scenario for scenario in scenarios}
        unknown = sorted(set(selected_ids) - set(by_id))
        if unknown:
            raise ValueError(f"unknown scenario ids: {', '.join(unknown)}")
        return [by_id[scenario_id] for scenario_id in selected_ids]
    return [scenario for scenario in scenarios if suite in scenario.suites]


def run_pair(
    scenario: Scenario,
    *,
    repeat_index: int,
    root: Path,
    timeout_override: int | None = None,
) -> list[dict[str, Any]]:
    fixture = root / "_fixtures" / scenario.scenario_id / f"r{repeat_index}"
    _reset_directory(fixture, root)
    write_fixture(scenario, fixture)
    fixture_hash = fixture_fingerprint(fixture)

    workspaces: dict[str, Path] = {}
    initial_hashes: dict[str, str] = {}
    for variant, _enabled in VARIANTS:
        workspace = root / variant / scenario.scenario_id / f"r{repeat_index}"
        _reset_directory(workspace, root)
        shutil.copytree(fixture, workspace, dirs_exist_ok=True)
        workspaces[variant] = workspace
        initial_hashes[variant] = fixture_fingerprint(workspace)

    pair_hash_ok = len(set(initial_hashes.values()) | {fixture_hash}) == 1
    results = []
    execution_order = VARIANTS if repeat_index % 2 else tuple(reversed(VARIANTS))
    for order_index, (variant, enabled) in enumerate(execution_order, start=1):
        result = run_once(
            scenario,
            variant=variant,
            controller_enabled=enabled,
            repeat_index=repeat_index,
            workspace=workspaces[variant],
            initial_fixture_hash=initial_hashes[variant],
            timeout_seconds=timeout_override or scenario.timeout_seconds,
        )
        if not pair_hash_ok:
            result["valid"] = False
            result["invalid_reason"] = "fixture_hash_mismatch"
        result["execution_order"] = order_index
        results.append(result)

    pair_valid = pair_hash_ok and all(result["valid"] for result in results)
    pair_id = f"{scenario.scenario_id}:r{repeat_index}"
    for result in results:
        result["pair_id"] = pair_id
        result["pair_valid"] = pair_valid
        if not pair_valid and result["valid"]:
            result["comparison_exclusion_reason"] = "paired_sample_invalid"
    return sorted(results, key=lambda result: result["variant"])


def write_fixture(scenario: Scenario, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for relative_path, content in scenario.setup_files.items():
        target = destination / _safe_relative_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def fixture_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((path for path in root.rglob("*") if path.is_file()), key=lambda item: item.as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_once(
    scenario: Scenario,
    *,
    variant: str,
    controller_enabled: bool,
    repeat_index: int,
    workspace: Path,
    initial_fixture_hash: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "HARNESS_WORKSPACE": str(workspace),
            "HARNESS_DOTENV_OVERRIDE_ENV": "0",
            "HARNESS_FLAT_WORKSPACE": "1",
            "HARNESS_METRICS_ENABLED": "1",
            "HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER": "1" if controller_enabled else "0",
        }
    )
    env.update(scenario.environment)
    prompt = scenario.prompt.replace("{python}", "py -3.12" if os.name == "nt" else "python3")
    command = [sys.executable, str(REPO_ROOT / "harness.py"), "--profile", scenario.profile, prompt]

    started = time.perf_counter()
    resume_attempted = False
    interrupted = False
    timed_out = False
    stdout = ""
    stderr = ""
    returncode: int | None = None

    if scenario.resume_interrupt_seconds is not None:
        first = _run_interrupted(command, cwd=REPO_ROOT, env=env, interrupt_after=scenario.resume_interrupt_seconds)
        stdout += first["stdout"]
        stderr += first["stderr"]
        interrupted = first["interrupted"]
        if interrupted:
            resume_attempted = True
            remaining = max(1, timeout_seconds - int(time.perf_counter() - started))
            resumed = _run_command(
                [sys.executable, str(REPO_ROOT / "harness.py"), "--resume", str(workspace)],
                cwd=REPO_ROOT,
                env=env,
                timeout=remaining,
            )
            stdout += "\n--- RESUME ---\n" + resumed["stdout"]
            stderr += "\n--- RESUME ---\n" + resumed["stderr"]
            timed_out = resumed["timed_out"]
            returncode = resumed["returncode"]
        else:
            returncode = first["returncode"]
    else:
        completed = _run_command(command, cwd=REPO_ROOT, env=env, timeout=timeout_seconds)
        stdout = completed["stdout"]
        stderr = completed["stderr"]
        timed_out = completed["timed_out"]
        returncode = completed["returncode"]

    wall_time_ms = int((time.perf_counter() - started) * 1000)
    acceptance = run_independent_acceptance(
        scenario.acceptance_command,
        workspace=workspace,
        timeout=scenario.acceptance_timeout_seconds,
    )
    state = _read_json(workspace / "harness_state.json")
    metrics = _read_json(workspace / ".harness" / "metrics.json")
    trace = read_jsonl(workspace / ".harness" / "canonical_trace.jsonl")

    invalid_reason = None
    if timed_out:
        invalid_reason = "agent_timeout"
    elif scenario.resume_interrupt_seconds is not None and not interrupted:
        invalid_reason = "resume_checkpoint_not_reached"
    elif acceptance["timed_out"]:
        invalid_reason = "acceptance_timeout"
    elif state is None:
        invalid_reason = "missing_state"
    elif metrics is None:
        invalid_reason = "missing_metrics"

    summary = (metrics or {}).get("summary") or {}
    reported_completed = bool(
        (state or {}).get("status") == "completed" or (metrics or {}).get("task_success") is True
    )
    acceptance_passed = acceptance["returncode"] == 0 and not acceptance["timed_out"]
    correct_completion = reported_completed and acceptance_passed
    erroneous_completion = reported_completed and not acceptance_passed
    trace_metrics = trace_statistics(trace)
    state_metrics = state_event_statistics((state or {}).get("events") or [])
    state_recovery = (state or {}).get("recovery") or {}
    # Controller-ON emits explicit acceptance repair events, while the legacy
    # path records the same retry as evidence-guided recovery.  Count both so
    # the A/B metric does not structurally under-report OFF repairs.
    repair_count = max(
        trace_metrics["repair_count"],
        state_metrics["repair_count"],
        int((state or {}).get("acceptance_repair_rounds") or 0),
        int(state_recovery.get("recovery_attempt_count") or 0),
    )
    recovery_attempts = max(
        int(summary.get("recovery_attempt_count") or 0),
        int(state_recovery.get("recovery_attempt_count") or 0),
        repair_count,
    )
    llm_call_count = (
        trace_metrics["llm_call_count"]
        if trace_metrics["llm_call_count"]
        else int(summary.get("llm_call_count") or 0)
    )
    total_tokens = (
        trace_metrics["total_tokens"]
        if trace_metrics["llm_call_count"]
        else int(summary.get("total_tokens") or 0)
    )

    return {
        "scenario_id": scenario.scenario_id,
        "categories": list(scenario.categories),
        "target_repair_rounds": list(scenario.target_repair_rounds) if scenario.target_repair_rounds else None,
        "variant": variant,
        "controller_enabled": controller_enabled,
        "repeat": repeat_index,
        "workspace": str(workspace),
        "initial_fixture_hash": initial_fixture_hash,
        "valid": invalid_reason is None,
        "invalid_reason": invalid_reason,
        "timed_out": timed_out,
        "returncode": returncode,
        "reported_completed": reported_completed,
        "independent_acceptance_passed": acceptance_passed,
        "correct_completion": correct_completion,
        "erroneous_completion": erroneous_completion,
        "llm_call_count": llm_call_count,
        "total_tokens": total_tokens,
        "wall_time_ms": wall_time_ms,
        "verification_attempt_count": trace_metrics["verification_attempt_count"],
        "repeated_verification_count": trace_metrics["repeated_verification_count"],
        "repair_count": repair_count,
        "delegation_count": trace_metrics["delegation_count"],
        "recovery_attempt_count": recovery_attempts,
        "recovery_success_count": max(
            int(summary.get("recovery_success_count") or 0),
            int(state_recovery.get("recovery_success_count") or 0),
        ),
        "resume_attempted": resume_attempted,
        "resume_succeeded": resume_attempted and correct_completion,
        "acceptance": acceptance,
        "stdout_tail": redact_tail(stdout),
        "stderr_tail": redact_tail(stderr),
    }


def run_independent_acceptance(command: tuple[str, ...], *, workspace: Path, timeout: int) -> dict[str, Any]:
    expanded = [sys.executable if arg == "{python}" else arg for arg in command]
    result = _run_command(expanded, cwd=workspace, env=os.environ.copy(), timeout=timeout)
    return {
        "command": ["{python}" if arg == sys.executable else arg for arg in expanded],
        "returncode": result["returncode"],
        "timed_out": result["timed_out"],
        "wall_time_ms": result["wall_time_ms"],
        "stdout_tail": redact_tail(result["stdout"]),
        "stderr_tail": redact_tail(result["stderr"]),
    }


def _run_command(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return {
            "returncode": proc.returncode,
            "timed_out": False,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "wall_time_ms": int((time.perf_counter() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "timed_out": True,
            "stdout": _decode_timeout_output(exc.stdout),
            "stderr": _decode_timeout_output(exc.stderr),
            "wall_time_ms": int((time.perf_counter() - started) * 1000),
        }


def _run_interrupted(
    command: list[str], *, cwd: Path, env: dict[str, str], interrupt_after: int
) -> dict[str, Any]:
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=interrupt_after)
        return {"returncode": proc.returncode, "interrupted": False, "stdout": stdout or "", "stderr": stderr or ""}
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        stdout, stderr = proc.communicate()
        return {"returncode": proc.returncode, "interrupted": True, "stdout": stdout or "", "stderr": stderr or ""}


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def trace_statistics(events: list[dict[str, Any]]) -> dict[str, int]:
    pending_verifications: dict[str, str] = {}
    commands: list[str] = []
    repair_count = 0
    delegation_count = 0
    llm_call_count = 0
    total_tokens = 0
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if event_type == "tool_requested":
            tool = str(payload.get("tool") or "")
            if tool == "delegate_task":
                delegation_count += 1
            command = str(payload.get("command") or "")
            if tool == "run_bash" and is_verification_command(command):
                pending_verifications[str(payload.get("tool_call_id") or "")] = _normalize_command(command)
        elif event_type == "tool_completed":
            tool_call_id = str(payload.get("tool_call_id") or "")
            if tool_call_id in pending_verifications:
                commands.append(pending_verifications.pop(tool_call_id))
        elif event_type == "llm_request_completed":
            llm_call_count += 1
            total_tokens += int(payload.get("input_tokens") or 0)
            total_tokens += int(payload.get("output_tokens") or 0)
            total_tokens += int(payload.get("reasoning_tokens") or 0)
        elif event_type in {"acceptance_retry_scheduled", "acceptance_decision"}:
            decision = payload.get("decision")
            action = payload.get("action")
            if not action:
                action = decision.get("action") if isinstance(decision, dict) else decision
            if action == "repair" and event_type == "acceptance_retry_scheduled":
                repair_count += 1

    return {
        "verification_attempt_count": len(commands),
        "repeated_verification_count": len(commands) - len(set(commands)),
        "repair_count": repair_count,
        "delegation_count": delegation_count,
        "llm_call_count": llm_call_count,
        "total_tokens": total_tokens,
    }


def state_event_statistics(events: list[dict[str, Any]]) -> dict[str, int]:
    repair_count = 0
    for event in events:
        if event.get("type") != "acceptance_retry_scheduled":
            continue
        data = event.get("data") or {}
        decision = data.get("decision")
        action = data.get("action") or (decision.get("action") if isinstance(decision, dict) else decision)
        if action == "repair":
            repair_count += 1
    return {"repair_count": repair_count}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def aggregate_runs(runs: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    variant_runs = [run for run in runs if run["variant"] == variant]
    comparable = [run for run in variant_runs if run.get("pair_valid")]

    def total(key: str) -> int:
        return sum(int(run.get(key) or 0) for run in comparable)

    count = len(comparable)
    reported = sum(1 for run in comparable if run["reported_completed"])
    correct = sum(1 for run in comparable if run["correct_completion"])
    erroneous = sum(1 for run in comparable if run["erroneous_completion"])
    resume_attempts = sum(1 for run in comparable if run["resume_attempted"])
    resume_successes = sum(1 for run in comparable if run["resume_succeeded"])

    def average(key: str) -> float:
        return total(key) / count if count else 0.0

    return {
        "run_count": len(variant_runs),
        "comparable_count": count,
        "invalid_sample_count": len(variant_runs) - count,
        "timeout_count": sum(1 for run in variant_runs if run["timed_out"]),
        "reported_completion_count": reported,
        "correct_completion_count": correct,
        "completion_rate": correct / count if count else 0.0,
        "erroneous_completion_count": erroneous,
        "erroneous_completion_rate": erroneous / count if count else 0.0,
        "llm_call_count": total("llm_call_count"),
        "avg_llm_calls": average("llm_call_count"),
        "total_tokens": total("total_tokens"),
        "avg_tokens": average("total_tokens"),
        "total_wall_time_ms": total("wall_time_ms"),
        "avg_wall_time_ms": average("wall_time_ms"),
        "verification_attempt_count": total("verification_attempt_count"),
        "repeated_verification_count": total("repeated_verification_count"),
        "repair_count": total("repair_count"),
        "recovery_attempt_count": total("recovery_attempt_count"),
        "recovery_success_count": total("recovery_success_count"),
        "resume_attempt_count": resume_attempts,
        "resume_success_count": resume_successes,
        "resume_success_rate": resume_successes / resume_attempts if resume_attempts else 0.0,
    }


def build_report(
    runs: list[dict[str, Any]],
    *,
    suite: str,
    repeat: int,
    selected_scenarios: list[Scenario],
    manifest: Path,
) -> dict[str, Any]:
    off = aggregate_runs(runs, "off")
    on = aggregate_runs(runs, "on")
    comparable_pairs = len({run["pair_id"] for run in runs if run.get("pair_valid")})
    metrics = (
        "completion_rate",
        "erroneous_completion_rate",
        "avg_llm_calls",
        "avg_tokens",
        "avg_wall_time_ms",
        "repeated_verification_count",
        "repair_count",
        "resume_success_rate",
    )
    return {
        "schema_version": 1,
        "suite": suite,
        "repeat": repeat,
        "manifest": str(manifest.resolve()),
        "scenario_ids": [scenario.scenario_id for scenario in selected_scenarios],
        "pair_count": len(selected_scenarios) * repeat,
        "comparable_pair_count": comparable_pairs,
        "invalid_pair_count": len(selected_scenarios) * repeat - comparable_pairs,
        "runs": runs,
        "aggregate": {"off": off, "on": on},
        "comparison": {
            metric: {"off": off[metric], "on": on[metric], "delta": on[metric] - off[metric]}
            for metric in metrics
        },
    }


def format_summary(report: dict[str, Any]) -> str:
    rows = ["| Metric | OFF | ON | Delta |", "| --- | ---: | ---: | ---: |"]
    for metric, values in report["comparison"].items():
        rows.append(f"| {metric} | {_fmt(values['off'])} | {_fmt(values['on'])} | {_fmt(values['delta'])} |")
    rows.append("")
    rows.append(
        f"Comparable pairs: {report['comparable_pair_count']}/{report['pair_count']} "
        f"(invalid: {report['invalid_pair_count']})"
    )
    return "\n".join(rows)


def _print_pair(pair: list[dict[str, Any]]) -> None:
    for run in pair:
        status = "invalid:" + str(run["invalid_reason"]) if not run["valid"] else (
            "pass" if run["correct_completion"] else "fail"
        )
        print(
            f"[{run['pair_id']}] {run['variant'].upper()}: {status}; "
            f"calls={run['llm_call_count']} tokens={run['total_tokens']} wall={run['wall_time_ms']}ms",
            flush=True,
        )


def redact_tail(value: str, limit: int = 2000) -> str:
    text = str(value or "")[-limit:]
    for key, secret in os.environ.items():
        if SECRET_NAME.search(key) and len(secret) >= 8:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted>", text)
    return text


def _decode_timeout_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_command(command: str) -> str:
    return " ".join(command.lower().split())


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe setup file path: {value}")
    return path


def _reset_directory(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise ValueError(f"refusing to reset path outside benchmark root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def _fmt(value: Any) -> str:
    return f"{value:.3f}" if isinstance(value, float) else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
