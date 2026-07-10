"""Hook mechanism for scheduler observability and forced progress."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.state import _store_for_workspace, append_event, load_state, now_iso, save_state


class HookManager:
    """Default hook manager that records scheduler events in state."""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)

    def before_transition(self, from_phase: str | None, to_phase: str, state: dict[str, Any]) -> None:
        self._record("before_transition", {"from": from_phase, "to": to_phase})

    def after_transition(self, from_phase: str | None, to_phase: str, state: dict[str, Any]) -> None:
        self._record("after_transition", {"from": from_phase, "to": to_phase})

    def before_agent_run(self, agent: str, state: dict[str, Any]) -> None:
        self._record("before_agent_run", {"agent": agent, "phase": state.get("phase")})

    def after_agent_run(self, agent: str, state: dict[str, Any], result: str | None = None) -> None:
        data = {"agent": agent, "phase": state.get("phase")}
        if result:
            data["result_preview"] = result[:500]
        self._record("after_agent_run", data)

    def on_stall(self, state: dict[str, Any], reason: str) -> None:
        self._record("stall", {"reason": reason, "phase": state.get("phase")})

    def on_error(self, state: dict[str, Any], error: Exception) -> None:
        self._record("error", {"type": type(error).__name__, "message": str(error)[:1000]})

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
