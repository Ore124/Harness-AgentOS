"""Hook mechanism for scheduler observability and forced progress.

Hooks are allowed to return a small decision object. The scheduler consumes
that decision at phase boundaries so policy stays out of the core harness loop.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.state import _store_for_workspace, append_event, load_state, now_iso, save_state


@dataclass
class HookResult:
    """Decision returned by a scheduler hook."""

    action: str = "continue"
    reason: str = ""
    patch: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def continue_(cls, reason: str = "", patch: dict[str, Any] | None = None) -> "HookResult":
        return cls("continue", reason, patch or {})

    @classmethod
    def pause(cls, reason: str, patch: dict[str, Any] | None = None) -> "HookResult":
        return cls("pause", reason, patch or {})

    @classmethod
    def retry(cls, reason: str, patch: dict[str, Any] | None = None) -> "HookResult":
        return cls("retry", reason, patch or {})

    @classmethod
    def fail(cls, reason: str, patch: dict[str, Any] | None = None) -> "HookResult":
        return cls("fail", reason, patch or {})

    @classmethod
    def require_confirmation(cls, reason: str, patch: dict[str, Any] | None = None) -> "HookResult":
        return cls("require_confirmation", reason, patch or {})


class HookManager:
    """Default hook manager that records scheduler events in state."""

    DEFAULT_PHASE_TIMEOUT_SECONDS = 60 * 60
    DEFAULT_HEARTBEAT_SECONDS = 15 * 60
    DEFAULT_MAX_RECOVERY_ATTEMPTS = 1
    DEFAULT_MAX_RUN_SECONDS = 6 * 60 * 60
    DEFAULT_MAX_TOOL_CALLS = 2000

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)

    # ------------------------------------------------------------------
    # P0: phase heartbeat/timeout, recovery, artifacts
    # ------------------------------------------------------------------

    def before_step(self, state: dict[str, Any]) -> HookResult:
        """Run policy checks before the scheduler advances a state file."""
        if state.get("status") == "running" and state.get("phase") in {"plan", "contract", "build", "evaluate"}:
            timeout_result = self.on_phase_timeout(state)
            if timeout_result.action != "continue":
                return timeout_result
            heartbeat_result = self.on_heartbeat_missing(state)
            if heartbeat_result.action != "continue":
                return heartbeat_result
        return self.on_budget_check(state)

    def on_phase_timeout(self, state: dict[str, Any]) -> HookResult:
        policy = state.get("hook_policy", {}) or {}
        timeout = int(policy.get("phase_timeout_seconds", self.DEFAULT_PHASE_TIMEOUT_SECONDS))
        phase_started_at = state.get("phase_started_at")
        if not phase_started_at:
            return HookResult.continue_()
        elapsed = _elapsed_seconds(phase_started_at)
        if elapsed is None or elapsed <= timeout:
            return HookResult.continue_()

        reason = f"phase {state.get('phase')} exceeded timeout ({elapsed:.0f}s > {timeout}s)"
        self._record("phase_timeout", {"reason": reason, "elapsed_seconds": elapsed})
        return HookResult.retry(
            reason,
            {
                "last_error": {"type": "PhaseTimeout", "message": reason},
                "recovery": {"reason": reason},
            },
        )

    def on_heartbeat_missing(self, state: dict[str, Any]) -> HookResult:
        policy = state.get("hook_policy", {}) or {}
        heartbeat = int(policy.get("heartbeat_seconds", self.DEFAULT_HEARTBEAT_SECONDS))
        last_event_at = state.get("last_event_at") or state.get("updated_at")
        elapsed = _elapsed_seconds(last_event_at)
        if elapsed is None or elapsed <= heartbeat:
            return HookResult.continue_()

        reason = f"no scheduler heartbeat for {elapsed:.0f}s"
        self._record("heartbeat_missing", {"reason": reason, "elapsed_seconds": elapsed})
        return HookResult.pause(
            reason,
            {"last_error": {"type": "HeartbeatMissing", "message": reason}},
        )

    def on_recovery(self, state: dict[str, Any], error: Exception | None = None, reason: str | None = None) -> HookResult:
        policy = state.get("hook_policy", {}) or {}
        max_attempts = int(policy.get("max_recovery_attempts", self.DEFAULT_MAX_RECOVERY_ATTEMPTS))
        recovery = dict(state.get("recovery") or {})
        attempts = int(recovery.get("attempts", 0) or 0)
        message = reason or (str(error) if error else "phase failed")

        if attempts < max_attempts:
            attempts += 1
            self._record("recovery_retry", {"attempts": attempts, "reason": message})
            return HookResult.retry(
                message,
                {
                    "recovery": {
                        **recovery,
                        "attempts": attempts,
                        "last_reason": message,
                        "last_retry_at": _now_iso(),
                    },
                    "last_error": {"type": type(error).__name__ if error else "Recovery", "message": message},
                },
            )

        self._record("recovery_exhausted", {"attempts": attempts, "reason": message})
        return HookResult.fail(
            f"recovery attempts exhausted: {message}",
            {
                "recovery": {
                    **recovery,
                    "attempts": attempts,
                    "exhausted": True,
                    "last_reason": message,
                },
                "last_error": {"type": type(error).__name__ if error else "RecoveryExhausted", "message": message},
            },
        )

    def on_artifact_changed(
        self,
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> HookResult:
        before_map = {item.get("name"): item for item in before}
        after_map = {item.get("name"): item for item in after}
        created = sorted(name for name in after_map if name not in before_map)
        changed = sorted(
            name for name, item in after_map.items()
            if name in before_map and item.get("size") != before_map[name].get("size")
        )
        if created or changed:
            data = {"created": created, "changed": changed, "phase": state.get("phase")}
            self._record("artifact_changed", data)
            return HookResult.continue_("artifacts changed", {"artifact_changes": data})
        return HookResult.continue_()

    # ------------------------------------------------------------------
    # P1: validation, route, memory
    # ------------------------------------------------------------------

    def before_route(self, state: dict[str, Any]) -> HookResult:
        self._record("before_route", {"prompt_preview": str(state.get("prompt", ""))[:200]})
        return HookResult.continue_()

    def after_route(self, state: dict[str, Any]) -> HookResult:
        decision = state.get("route_decision") or {}
        self._record("after_route", decision)
        if decision.get("requires_confirmation"):
            return self.on_low_confidence_route(state)
        return HookResult.continue_()

    def on_low_confidence_route(self, state: dict[str, Any]) -> HookResult:
        decision = state.get("route_decision") or {}
        reason = f"route confidence too low: {decision.get('confidence')}"
        self._record("low_confidence_route", {"reason": reason, "decision": decision})
        return HookResult.require_confirmation(reason)

    def on_profile_confirmed(self, state: dict[str, Any], profile: str) -> HookResult:
        self._record("profile_confirmed", {"profile": profile})
        return HookResult.continue_()

    def after_validation(self, state: dict[str, Any], analysis: dict[str, Any] | None = None) -> HookResult:
        validation = _validation_summary(state, analysis)
        self._record("validation_checked", validation)
        if validation["status"] == "missing":
            self._record("validation_missing", validation)
        elif validation["status"] == "failed":
            self._record("validation_failed", validation)
        return HookResult.continue_("validation checked", {"validation": validation})

    def before_memory_update(self, state: dict[str, Any], analysis: dict[str, Any]) -> HookResult:
        self._record("before_memory_update", {
            "profile": state.get("profile"),
            "status": state.get("status"),
            "score_history": state.get("score_history", []),
        })
        if state.get("skip_memory_update"):
            return HookResult.continue_("memory update skipped by state flag", {"memory_update_skipped": True})
        return HookResult.continue_()

    def after_memory_update(self, state: dict[str, Any], analysis: dict[str, Any]) -> HookResult:
        self._record("after_memory_update", {
            "profile": state.get("profile"),
            "tool_calls": (analysis.get("tool_calls") or {}).get("total", 0),
        })
        return HookResult.continue_()

    # ------------------------------------------------------------------
    # P2: human approval, budget
    # ------------------------------------------------------------------

    def on_human_approval_required(self, state: dict[str, Any]) -> HookResult:
        approval = state.get("human_approval") or {}
        reason = approval.get("reason") or "human approval required"
        self._record("human_approval_required", approval)
        return HookResult.require_confirmation(
            reason,
            {
                "requires_human_approval": True,
                "status": "waiting_approval",
            },
        )

    def on_budget_check(self, state: dict[str, Any]) -> HookResult:
        policy = state.get("hook_policy", {}) or {}
        elapsed = _elapsed_seconds(state.get("created_at")) or 0
        max_run_seconds = int(policy.get("max_run_seconds", self.DEFAULT_MAX_RUN_SECONDS))
        if elapsed > max_run_seconds:
            reason = f"run budget exceeded ({elapsed:.0f}s > {max_run_seconds}s)"
            self._record("budget_exceeded", {"reason": reason, "elapsed_seconds": elapsed})
            return HookResult.pause(
                reason,
                {"last_error": {"type": "BudgetExceeded", "message": reason}},
            )

        max_tool_calls = int(policy.get("max_tool_calls", self.DEFAULT_MAX_TOOL_CALLS))
        tool_calls = _count_trace_tool_calls(Path(state.get("workspace", "")))
        if tool_calls > max_tool_calls:
            reason = f"tool-call budget exceeded ({tool_calls} > {max_tool_calls})"
            self._record("budget_exceeded", {"reason": reason, "tool_calls": tool_calls})
            return HookResult.pause(
                reason,
                {"last_error": {"type": "BudgetExceeded", "message": reason}},
            )
        return HookResult.continue_()

    def before_new_round(self, state: dict[str, Any]) -> HookResult:
        self._record("before_new_round", {
            "next_round": int(state.get("round_num", 1)) + 1,
            "score_history": state.get("score_history", []),
        })
        return self.on_budget_check(state)

    # ------------------------------------------------------------------
    # Existing scheduler events
    # ------------------------------------------------------------------

    def before_transition(self, from_phase: str | None, to_phase: str, state: dict[str, Any]) -> HookResult:
        self._record("before_transition", {"from": from_phase, "to": to_phase})
        return HookResult.continue_()

    def after_transition(self, from_phase: str | None, to_phase: str, state: dict[str, Any]) -> HookResult:
        self._record("after_transition", {"from": from_phase, "to": to_phase})
        return HookResult.continue_()

    def before_agent_run(self, agent: str, state: dict[str, Any]) -> HookResult:
        self._record("before_agent_run", {"agent": agent, "phase": state.get("phase")})
        return HookResult.continue_()

    def after_agent_run(self, agent: str, state: dict[str, Any], result: str | None = None) -> HookResult:
        data = {"agent": agent, "phase": state.get("phase")}
        if result:
            data["result_preview"] = result[:500]
        self._record("after_agent_run", data)
        return HookResult.continue_()

    def on_stall(self, state: dict[str, Any], reason: str) -> HookResult:
        self._record("stall", {"reason": reason, "phase": state.get("phase")})
        return HookResult.continue_()

    def on_error(self, state: dict[str, Any], error: Exception) -> HookResult:
        self._record("error", {"type": type(error).__name__, "message": str(error)[:1000]})
        return self.on_recovery(state, error=error)

    def _record(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            state = load_state(self.state_path)
            _store_for_workspace(state["workspace"]).append_event(
                state["run_id"],
                {"t": now_iso(), "type": event_type, "data": data},
            )
            append_event(state, event_type, data)
            save_state(self.state_path, state)
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return time.time() - datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _count_trace_tool_calls(workspace: Path) -> int:
    if not workspace.exists():
        return 0
    count = 0
    for trace in workspace.glob("_trace_*.jsonl"):
        try:
            for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    if json.loads(line).get("event") == "tool_call":
                        count += 1
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return count


def _validation_summary(state: dict[str, Any], analysis: dict[str, Any] | None) -> dict[str, Any]:
    analysis = analysis or {}
    by_tool = (analysis.get("tool_calls") or {}).get("by_tool", {})
    scores = analysis.get("scores") or state.get("score_history") or []
    feedback = str(analysis.get("feedback_preview", ""))
    trace_text = json.dumps(analysis.get("agents", {}), ensure_ascii=False).lower()
    evidence = []

    if by_tool.get("browser_test"):
        evidence.append("browser_test")
    if by_tool.get("run_bash"):
        evidence.append("run_bash")
    if scores:
        evidence.append("score")
    if any(term in feedback.lower() for term in ["verification", "test", "passed", "failed"]):
        evidence.append("feedback")

    failed = any(term in feedback.lower() for term in ["failed", "error", "traceback", "exception"])
    if failed:
        status = "failed"
    elif evidence:
        status = "verified"
    elif state.get("profile") == "reasoning":
        status = "not_required"
    else:
        status = "missing"

    return {
        "status": status,
        "evidence": evidence,
        "scores": scores,
        "trace_agents": sorted((analysis.get("agents") or {}).keys()),
        "trace_summary": trace_text[:300],
    }
