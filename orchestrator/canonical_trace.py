"""Versioned, append-only events plus pure offline replay for Harness runs."""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import Any

SCHEMA_VERSION = 1
TRACE_NAME = "canonical_trace.jsonl"
TERMINAL_EVENTS = {"run_completed", "run_failed"}
_WRITERS: dict[tuple[str, str], "CanonicalTraceWriter"] = {}


@dataclass
class CanonicalTraceWriter:
    run_id: str
    workspace: Path
    sequence: int = 0

    @property
    def path(self) -> Path:
        return self.workspace / ".harness" / TRACE_NAME

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, role: str | None = None, phase: str | None = None) -> dict[str, Any]:
        self.sequence += 1
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "run_id": self.run_id,
            "seq": self.sequence,
            "ts_ms": time_ns() // 1_000_000,
            "event_type": event_type,
            "role": role,
            "phase": phase,
            "payload": payload or {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event


def writer_for(workspace: str | Path, run_id: str) -> CanonicalTraceWriter:
    key = (str(Path(workspace).resolve()), str(run_id))
    writer = _WRITERS.get(key)
    if writer is None:
        writer = CanonicalTraceWriter(str(run_id), Path(workspace))
        _WRITERS[key] = writer
    return writer


def emit_event(workspace: str | Path, run_id: str, event_type: str, payload: dict[str, Any] | None = None, *, role: str | None = None, phase: str | None = None) -> None:
    """Best-effort instrumentation helper; never changes execution behavior."""
    try:
        writer_for(workspace, run_id).emit(event_type, payload, role=role, phase=phase)
    except Exception:
        pass


def replay_trace(path: str | Path) -> dict[str, Any]:
    """Read events only; this function never executes tools or contacts an LLM."""
    events = _read_events(Path(path))
    errors = _validate(events)
    by_role: dict[str, Counter] = defaultdict(Counter)
    by_phase: dict[str, Counter] = defaultdict(Counter)
    totals = Counter()
    states: list[dict[str, Any]] = []
    for event in events:
        role = event.get("role") or "unattributed"
        phase = event.get("phase") or "unattributed"
        kind = event["event_type"]
        payload = event.get("payload") or {}
        for bucket in (by_role[role], by_phase[phase], totals):
            bucket["events"] += 1
            if kind == "llm_request_started":
                bucket["llm_calls"] += 1
            if kind == "llm_request_completed":
                bucket["input_tokens"] += int(payload.get("input_tokens") or 0)
                bucket["cached_tokens"] += int(payload.get("cached_tokens") or 0)
                bucket["output_tokens"] += int(payload.get("output_tokens") or 0)
                bucket["llm_latency_ms"] += int(payload.get("latency_ms") or 0)
            if kind == "tool_requested":
                bucket["tool_calls"] += 1
            if kind == "agent_round":
                bucket["agent_rounds"] += 1
        if kind in {"state_changed", "phase_started", "phase_completed", *TERMINAL_EVENTS}:
            states.append({"seq": event["seq"], "event_type": kind, "phase": event.get("phase"), "status": payload.get("status")})
    terminal = next((e for e in reversed(events) if e["event_type"] in TERMINAL_EVENTS), None)
    started = next((e for e in events if e["event_type"] == "run_started"), None)
    totals["wall_time_ms"] = max(0, int((terminal or {}).get("ts_ms") or 0) - int((started or {}).get("ts_ms") or 0))
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": events[0].get("run_id") if events else None,
        "start_payload": (started or {}).get("payload", {}),
        "events": events,
        "timeline": states,
        "by_role": {key: dict(value) for key, value in by_role.items()},
        "by_phase": {key: dict(value) for key, value in by_phase.items()},
        "totals": dict(totals),
        "final_status": (terminal or {}).get("payload", {}).get("status"),
        "task_success": (terminal or {}).get("payload", {}).get("task_success"),
        "valid": not errors,
        "invalid_reasons": errors,
    }


def compare_replays(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if not baseline.get("valid") or not candidate.get("valid"):
        return {"comparable": False, "reason": "baseline or candidate trace is invalid"}
    base_start = next(e for e in baseline["events"] if e["event_type"] == "run_started")
    cand_start = next(e for e in candidate["events"] if e["event_type"] == "run_started")
    for key in ("task_id", "model", "initial_workspace", "feature_flags", "schema_version"):
        if base_start.get("payload", {}).get(key) != cand_start.get("payload", {}).get(key):
            return {"comparable": False, "reason": f"mismatched {key}"}
    return {"comparable": True, "delta": {key: int(candidate["totals"].get(key, 0)) - int(baseline["totals"].get(key, 0)) for key in {"llm_calls", "tool_calls", "agent_rounds", "input_tokens", "output_tokens", "llm_latency_ms", "wall_time_ms"}}}


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"event_type": "invalid_json", "payload": {}})
    return events


def _validate(events: list[dict[str, Any]]) -> list[str]:
    errors = []
    if not events:
        return ["empty trace"]
    ids = [event.get("event_id") for event in events]
    if any(not event_id for event_id in ids) or len(ids) != len(set(ids)):
        errors.append("event IDs are missing or duplicated")
    if any(event.get("schema_version") != SCHEMA_VERSION for event in events):
        errors.append("unsupported schema version")
    if len({event.get("run_id") for event in events}) != 1:
        errors.append("multiple run IDs")
    sequences = [event.get("seq") for event in events]
    if sequences != list(range(1, len(events) + 1)):
        errors.append("event sequence is not contiguous")
    terminals = [event for event in events if event.get("event_type") in TERMINAL_EVENTS]
    if len(terminals) != 1:
        errors.append("missing or multiple terminal events")
    elif terminals[0].get("payload", {}).get("task_success") is None:
        errors.append("terminal task_success is null")
    requests = {event.get("payload", {}).get("llm_call_id") for event in events if event.get("event_type") == "llm_request_started"}
    results = {event.get("payload", {}).get("llm_call_id") for event in events if event.get("event_type") in {"llm_request_completed", "llm_request_failed"}}
    if None in requests or requests != results:
        errors.append("LLM request/result ledger does not balance")
    tools = {event.get("payload", {}).get("tool_call_id") for event in events if event.get("event_type") == "tool_requested"}
    results = {event.get("payload", {}).get("tool_call_id") for event in events if event.get("event_type") in {"tool_completed", "tool_failed"}}
    if None in tools or tools != results:
        errors.append("tool request/result ledger does not balance")
    for event in events:
        if event.get("event_type") != "llm_request_completed":
            continue
        payload = event.get("payload") or {}
        try:
            input_tokens = int(payload.get("input_tokens") or 0)
            cached_tokens = int(payload.get("cached_tokens") or 0)
            output_tokens = int(payload.get("output_tokens") or 0)
        except (TypeError, ValueError):
            errors.append("LLM token values are not integers")
            break
        if min(input_tokens, cached_tokens, output_tokens) < 0 or cached_tokens > input_tokens:
            errors.append("LLM token conservation is invalid")
            break
    return errors
