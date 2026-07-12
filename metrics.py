"""Low-overhead runtime metrics for Harness runs.

Metrics are collected in memory and flushed to ``.harness/metrics.json`` at
phase/run boundaries. The recorder is intentionally passive: it never injects
data into model context and failures are swallowed so instrumentation cannot
change agent behavior.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import config


def enabled() -> bool:
    return bool(getattr(config, "HARNESS_METRICS_ENABLED", True))


class MetricsRecorder:
    def __init__(self) -> None:
        self.run_id: str | None = None
        self.workspace: Path | None = None
        self.started_at: float | None = None
        self.repeated_tool_call_count = 0
        self.context_compression_count = 0
        self.context_reset_count = 0
        self._tool_call_keys: dict[str, int] = {}
        self._next_llm_call_index = 0
        self.data: dict[str, Any] = {}

    def start_run(self, run_id: str, workspace: str | Path, profile: str | None = None) -> None:
        if not enabled():
            return
        if self.run_id == run_id and self.workspace == Path(workspace):
            return
        self.run_id = run_id
        self.workspace = Path(workspace)
        self.started_at = time.perf_counter()
        self.repeated_tool_call_count = 0
        self.context_compression_count = 0
        self.context_reset_count = 0
        self._tool_call_keys = {}
        self._next_llm_call_index = 0
        self.data = {
            "run_id": run_id,
            "profile": profile,
            "workspace": str(Path(workspace).resolve()),
            "llm_calls": [],
            "token_attribution": [],
            "tool_calls": [],
            "middleware_injections": [],
            "latency": {
                "llm_calls_ms": 0,
                "tool_execution_ms": 0,
                "context_processing_ms": 0,
                "middleware_ms": 0,
                "compression_reset_ms": 0,
                "scheduler_state_transitions_ms": 0,
                "state_file_io_ms": 0,
                "trace_log_persistence_ms": 0,
                "agent_initialization_ms": 0,
                "api_preflight_ms": 0,
                "explicit_sleep_polling_ms": 0,
                "benchmark_harness_overhead_ms": 0,
            },
            "agent_rounds": {},
            "phase_rounds": {},
            "phases": {},
            "task_success": None,
            "verification_success": None,
            "recovery": {
                "failed_attempt_count": 0,
                "recovery_attempt_count": 0,
                "recovery_success_count": 0,
                "repeated_failure_count": 0,
                "same_failure_escalation_count": 0,
                "retries_per_successful_recovery": 0.0,
                "failure_evidence": [],
            },
        }

    def start_phase(self, phase: str) -> None:
        if not enabled() or not self.data:
            return
        self.data.setdefault("phases", {}).setdefault(phase, {})["started_at_perf"] = time.perf_counter()
        self.data.setdefault("phase_rounds", {})[phase] = self.data.setdefault("phase_rounds", {}).get(phase, 0) + 1

    def end_phase(self, phase: str) -> None:
        if not enabled() or not self.data:
            return
        phase_data = self.data.setdefault("phases", {}).setdefault(phase, {})
        start = phase_data.pop("started_at_perf", None)
        if start is not None:
            phase_data["phase_wall_time_ms"] = int((time.perf_counter() - start) * 1000)
        self.flush()

    def record_agent_round(self, role: str, phase: str | None, iteration: int) -> None:
        if not enabled() or not self.data:
            return
        rounds = self.data.setdefault("agent_rounds", {})
        rounds[role] = max(int(rounds.get(role, 0)), iteration)
        if phase:
            key = f"{phase}:{role}"
            rounds[key] = max(int(rounds.get(key, 0)), iteration)

    def record_context_event(self, event_type: str) -> None:
        if not enabled() or not self.data:
            return
        if event_type == "compact":
            self.context_compression_count += 1
        elif event_type == "reset":
            self.context_reset_count += 1

    def add_latency(self, category: str, elapsed_ms: int) -> None:
        if not enabled() or not self.data:
            return
        latency = self.data.setdefault("latency", {})
        latency[category] = int(latency.get(category) or 0) + int(elapsed_ms)

    def record_token_attribution(
        self,
        *,
        role: str,
        phase: str | None,
        categories: dict[str, int],
        estimated_input_tokens: int,
    ) -> int | None:
        if not enabled() or not self.data:
            return None
        self.data.setdefault("token_attribution", []).append({
            "role": role,
            "phase": phase,
            "estimated_input_tokens": estimated_input_tokens,
            "categories": dict(categories),
        })

    def record_llm_call(
        self,
        *,
        role: str,
        phase: str | None,
        model: str,
        latency_ms: int,
        usage: Any,
        time_to_first_token_ms: int | None = None,
    ) -> None:
        if not enabled() or not self.data:
            return
        prompt_tokens = _get_usage_value(usage, "prompt_tokens", "input_tokens")
        completion_tokens = _get_usage_value(usage, "completion_tokens", "output_tokens")
        reasoning_tokens = _get_nested_usage_value(usage, "completion_tokens_details", "reasoning_tokens")
        cached_tokens = _get_nested_usage_value(usage, "prompt_tokens_details", "cached_tokens")
        cache_write_tokens = (
            _get_nested_usage_value(usage, "prompt_tokens_details", "cache_write_tokens")
            or _get_nested_usage_value(usage, "input_tokens_details", "cache_write_tokens")
        )
        self._next_llm_call_index += 1
        call_index = self._next_llm_call_index
        self.data.setdefault("llm_calls", []).append({
            "call_index": call_index,
            "role": role,
            "phase": phase,
            "model": model,
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "request_latency_ms": latency_ms,
            "time_to_first_token_ms": time_to_first_token_ms,
            "progress": {},
        })
        self.add_latency("llm_calls_ms", latency_ms)
        return call_index

    def record_llm_result(
        self,
        *,
        call_index: int | None,
        tool_names: list[str],
        finish_reason: str | None,
        content: str | None,
    ) -> None:
        if not enabled() or not self.data or call_index is None:
            return
        for call in self.data.setdefault("llm_calls", []):
            if call.get("call_index") == call_index:
                call["tool_names"] = list(tool_names)
                call["finish_reason"] = finish_reason
                call["content_chars"] = len(content or "")
                progress = call.setdefault("progress", {})
                progress["new_effective_tool_call"] = bool(tool_names)
                progress["repeated_or_explanatory"] = not bool(tool_names)
                return

    def record_llm_tool_progress(
        self,
        *,
        call_index: int | None,
        workspace_modified: bool = False,
        failure_evidence_found: bool = False,
        repeated_tool_call: bool = False,
    ) -> None:
        if not enabled() or not self.data or call_index is None:
            return
        for call in self.data.setdefault("llm_calls", []):
            if call.get("call_index") == call_index:
                progress = call.setdefault("progress", {})
                progress["workspace_modified"] = bool(progress.get("workspace_modified") or workspace_modified)
                progress["failure_evidence_found"] = bool(progress.get("failure_evidence_found") or failure_evidence_found)
                progress["repeated_tool_call"] = bool(progress.get("repeated_tool_call") or repeated_tool_call)
                if workspace_modified or failure_evidence_found:
                    progress["repeated_or_explanatory"] = False
                return

    def record_phase_progress(self, phase: str, advanced: bool) -> None:
        if not enabled() or not self.data:
            return
        calls = self.data.setdefault("llm_calls", [])
        for call in reversed(calls):
            if call.get("phase") == phase:
                progress = call.setdefault("progress", {})
                progress["phase_advanced"] = bool(progress.get("phase_advanced") or advanced)
                if advanced:
                    progress["repeated_or_explanatory"] = False
                return

    def record_status_change(self, changed: bool) -> None:
        if not enabled() or not self.data:
            return
        calls = self.data.setdefault("llm_calls", [])
        if not calls:
            return
        progress = calls[-1].setdefault("progress", {})
        progress["task_acceptance_status_changed"] = bool(
            progress.get("task_acceptance_status_changed") or changed
        )
        if changed:
            progress["repeated_or_explanatory"] = False

    def record_failure_evidence(self, evidence: dict[str, Any], *, recovery_attempt_planned: bool = False) -> None:
        if not enabled() or not self.data:
            return
        recovery = self.data.setdefault("recovery", {})
        recovery["failed_attempt_count"] = int(recovery.get("failed_attempt_count") or 0) + 1
        if int(evidence.get("same_failure_count") or 0) > 1:
            recovery["repeated_failure_count"] = int(recovery.get("repeated_failure_count") or 0) + 1
        if int(evidence.get("same_failure_count") or 0) >= 3:
            recovery["same_failure_escalation_count"] = int(recovery.get("same_failure_escalation_count") or 0) + 1
        rows = recovery.setdefault("failure_evidence", [])
        rows.append({
            "failure_type": evidence.get("failure_type"),
            "failure_signature": evidence.get("failure_signature"),
            "same_failure_count": evidence.get("same_failure_count"),
            "recovery_strategy": evidence.get("recovery_strategy"),
            "retry_planned": bool(recovery_attempt_planned),
        })
        if len(rows) > 20:
            del rows[:-20]

    def record_recovery_attempt(self) -> None:
        if not enabled() or not self.data:
            return
        recovery = self.data.setdefault("recovery", {})
        recovery["recovery_attempt_count"] = int(recovery.get("recovery_attempt_count") or 0) + 1

    def record_recovery_result(self, *, task_success: bool) -> None:
        if not enabled() or not self.data:
            return
        recovery = self.data.setdefault("recovery", {})
        attempts = int(recovery.get("recovery_attempt_count") or 0)
        success_count = 1 if task_success and attempts > 0 else 0
        recovery["recovery_success_count"] = success_count
        recovery["recovery_success_rate"] = (success_count / attempts) if attempts else 0.0
        recovery["retries_per_successful_recovery"] = (attempts / success_count) if success_count else 0.0

    def record_tool_call(
        self,
        *,
        role: str,
        phase: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        latency_ms: int,
        result: str,
    ) -> None:
        if not enabled() or not self.data:
            return
        success = not str(result).lstrip().lower().startswith("[error]")
        key = json.dumps([role, tool_name, arguments], sort_keys=True, default=str)
        self._tool_call_keys[key] = self._tool_call_keys.get(key, 0) + 1
        if self._tool_call_keys[key] == 2:
            self.repeated_tool_call_count += 1
            repeated_tool_call = True
        else:
            repeated_tool_call = self._tool_call_keys[key] > 2
        failure_evidence = _contains_failure_evidence(result)
        workspace_modified = name_mutates_workspace(tool_name, result)
        self.data.setdefault("tool_calls", []).append({
            "role": role,
            "phase": phase,
            "tool_name": tool_name,
            "tool_latency_ms": latency_ms,
            "result_size_chars": len(result),
            "result_estimated_tokens": _estimate_tokens(result),
            "success": success,
            "workspace_modified": workspace_modified,
            "failure_evidence_found": failure_evidence,
            "repeated_tool_call": repeated_tool_call,
        })
        self.add_latency("tool_execution_ms", latency_ms)
        self.record_llm_tool_progress(
            call_index=self._current_llm_call_index(role, phase),
            workspace_modified=workspace_modified,
            failure_evidence_found=failure_evidence,
            repeated_tool_call=repeated_tool_call,
        )

    def record_middleware_injection(self, source: str, hook: str, message: str) -> None:
        if not enabled() or not self.data:
            return
        self.data.setdefault("middleware_injections", []).append({
            "source": source,
            "hook": hook,
            "message_hash": str(hash(message)),
            "message_preview": message[:160],
            "estimated_tokens": _estimate_tokens(message),
        })

    def _current_llm_call_index(self, role: str, phase: str | None) -> int | None:
        for call in reversed(self.data.get("llm_calls") or []):
            if call.get("role") == role and call.get("phase") == phase:
                return int(call.get("call_index") or 0)
        return None

    def finish_run(self, *, task_success: bool | None = None, verification_success: bool | None = None) -> None:
        if not enabled() or not self.data:
            return
        if self.started_at is not None:
            self.data["total_run_wall_time_ms"] = int((time.perf_counter() - self.started_at) * 1000)
        self.data["task_success"] = task_success
        self.data["verification_success"] = verification_success
        self.data["repeated_tool_call_count"] = self.repeated_tool_call_count
        self.data["context_compression_count"] = self.context_compression_count
        self.data["context_reset_count"] = self.context_reset_count
        self.data["summary"] = summarize(self.data)
        self.flush()

    def flush(self) -> None:
        if not enabled() or not self.workspace or not self.data:
            return
        try:
            path = self.workspace / ".harness" / "metrics.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(self.data)
            payload["repeated_tool_call_count"] = self.repeated_tool_call_count
            payload["context_compression_count"] = self.context_compression_count
            payload["context_reset_count"] = self.context_reset_count
            payload["summary"] = summarize(payload)
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    llm_calls = data.get("llm_calls") or []
    tool_calls = data.get("tool_calls") or []
    input_tokens = sum(int(call.get("input_tokens") or 0) for call in llm_calls)
    output_tokens = sum(int(call.get("output_tokens") or 0) for call in llm_calls)
    reasoning_tokens = sum(int(call.get("reasoning_tokens") or 0) for call in llm_calls)
    cached_tokens = sum(int(call.get("cached_tokens") or 0) for call in llm_calls)
    cache_write_tokens = sum(int(call.get("cache_write_tokens") or 0) for call in llm_calls)
    agent_rounds = data.get("agent_rounds") or {}
    token_attribution = aggregate_token_attribution(data, actual_input_tokens=input_tokens)
    latency = aggregate_latency(data)
    llm_by_phase_role = aggregate_llm_by_phase_role(data)
    middleware_summary = aggregate_middleware(data)
    return {
        "llm_call_count": len(llm_calls),
        "tool_call_count": len(tool_calls),
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "total_reasoning_tokens": reasoning_tokens,
        "total_cached_tokens": cached_tokens,
        "total_cache_write_tokens": cache_write_tokens,
        "total_tokens": input_tokens + output_tokens + reasoning_tokens,
        "cache_hit_ratio": (cached_tokens / input_tokens) if input_tokens else 0.0,
        "agent_rounds": sum(v for k, v in agent_rounds.items() if ":" not in k),
        "phase_rounds": sum((data.get("phase_rounds") or {}).values()),
        "repeated_tool_call_count": int(data.get("repeated_tool_call_count") or 0),
        "context_compression_count": int(data.get("context_compression_count") or 0),
        "context_reset_count": int(data.get("context_reset_count") or 0),
        "token_attribution": token_attribution,
        "latency_attribution": latency,
        "llm_by_phase_role": llm_by_phase_role,
        "llm_call_sequence": [
            f"{call.get('phase') or 'other'}:{call.get('role') or 'other'}"
            for call in llm_calls
        ],
        "middleware_attribution": middleware_summary,
        "failed_attempt_count": int((data.get("recovery") or {}).get("failed_attempt_count") or 0),
        "recovery_attempt_count": int((data.get("recovery") or {}).get("recovery_attempt_count") or 0),
        "recovery_success_count": int((data.get("recovery") or {}).get("recovery_success_count") or 0),
        "recovery_success_rate": float((data.get("recovery") or {}).get("recovery_success_rate") or 0.0),
        "repeated_failure_count": int((data.get("recovery") or {}).get("repeated_failure_count") or 0),
        "same_failure_escalation_count": int((data.get("recovery") or {}).get("same_failure_escalation_count") or 0),
        "retries_per_successful_recovery": float(
            (data.get("recovery") or {}).get("retries_per_successful_recovery") or 0.0
        ),
    }


def aggregate_token_attribution(data: dict[str, Any], actual_input_tokens: int | None = None) -> dict[str, int]:
    totals = _empty_token_categories()
    for row in data.get("token_attribution") or []:
        for key, value in (row.get("categories") or {}).items():
            totals[key] = totals.get(key, 0) + int(value or 0)
    if actual_input_tokens is not None:
        estimated = sum(totals.values())
        if actual_input_tokens > estimated:
            totals["other"] += actual_input_tokens - estimated
    return totals


def aggregate_latency(data: dict[str, Any]) -> dict[str, int]:
    latency = dict(data.get("latency") or {})
    if "scheduler_state_transitions_ms" in latency and "state_file_io_ms" in latency:
        latency["scheduler_state_transitions_ms"] = max(
            0,
            int(latency.get("scheduler_state_transitions_ms") or 0)
            - int(latency.get("state_file_io_ms") or 0),
        )
    wall = int(data.get("total_run_wall_time_ms") or 0)
    known = sum(int(v or 0) for v in latency.values())
    latency["orchestration_overhead_ms"] = max(0, wall - known)
    return latency


def aggregate_llm_by_phase_role(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    buckets: dict[str, dict[str, int]] = {}
    for call in data.get("llm_calls") or []:
        label = _phase_role_label(call.get("phase"), call.get("role"))
        bucket = buckets.setdefault(label, {
            "calls": 0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "uncached_tokens": 0,
            "output_tokens": 0,
            "total_llm_time_ms": 0,
        })
        input_tokens = int(call.get("input_tokens") or 0)
        cached_tokens = int(call.get("cached_tokens") or 0)
        bucket["calls"] += 1
        bucket["input_tokens"] += input_tokens
        bucket["cached_tokens"] += cached_tokens
        bucket["uncached_tokens"] += max(0, input_tokens - cached_tokens)
        bucket["output_tokens"] += int(call.get("output_tokens") or 0)
        bucket["total_llm_time_ms"] += int(call.get("request_latency_ms") or 0)
    for label in ["Router", "Planner", "Contract", "Builder", "Evaluator", "Analyze", "Summarizer", "Other"]:
        buckets.setdefault(label, {
            "calls": 0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "uncached_tokens": 0,
            "output_tokens": 0,
            "total_llm_time_ms": 0,
        })
    return buckets


def aggregate_middleware(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    buckets: dict[str, dict[str, int]] = {}
    seen_by_source: dict[str, dict[str, int]] = {}
    for row in data.get("middleware_injections") or []:
        source = row.get("source") or "unknown"
        bucket = buckets.setdefault(source, {
            "injection_count": 0,
            "injected_tokens": 0,
            "repeated_identical_or_near_identical": 0,
        })
        bucket["injection_count"] += 1
        bucket["injected_tokens"] += int(row.get("estimated_tokens") or 0)
        source_seen = seen_by_source.setdefault(source, {})
        key = row.get("message_hash") or row.get("message_preview") or ""
        source_seen[key] = source_seen.get(key, 0) + 1
        if source_seen[key] > 1:
            bucket["repeated_identical_or_near_identical"] += 1
    return buckets


def _phase_role_label(phase: str | None, role: str | None) -> str:
    value = (phase or role or "other").lower()
    role_value = (role or "").lower()
    if value == "route" or role_value == "router":
        return "Router"
    if value == "plan" or role_value == "planner":
        return "Planner"
    if value == "contract" or "contract" in role_value:
        return "Contract"
    if value == "build" or role_value == "builder":
        return "Builder"
    if value == "evaluate" or role_value == "evaluator":
        return "Evaluator"
    if value == "analyze":
        return "Analyze"
    if role_value == "context_summarizer":
        return "Summarizer"
    return "Other"


RECORDER = MetricsRecorder()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def estimate_text_tokens(text: str) -> int:
    return _estimate_tokens(text)


def name_mutates_workspace(tool_name: str, result: str) -> bool:
    if tool_name in {"write_file", "edit_file"}:
        return not str(result).lstrip().lower().startswith("[error]")
    return False


def _contains_failure_evidence(text: str) -> bool:
    lowered = str(text).lower()
    markers = [
        "[error]",
        "[exit code:",
        "traceback",
        "assert",
        "failed",
        "failure",
        "exception",
        "error:",
        "warning:",
    ]
    return any(marker in lowered for marker in markers)


def _empty_token_categories() -> dict[str, int]:
    return {
        "static_system_prompt": 0,
        "dynamic_task_state": 0,
        "assistant_history": 0,
        "tool_results": 0,
        "tool_call_arguments": 0,
        "middleware_injections": 0,
        "compression_reset": 0,
        "other": 0,
    }


def _get_usage_value(usage: Any, *names: str) -> int:
    for name in names:
        value = _get_attr_or_item(usage, name)
        if value is not None:
            return int(value or 0)
    return 0


def _get_nested_usage_value(usage: Any, parent: str, child: str) -> int:
    obj = _get_attr_or_item(usage, parent)
    value = _get_attr_or_item(obj, child)
    return int(value or 0) if value is not None else 0


def _get_attr_or_item(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
