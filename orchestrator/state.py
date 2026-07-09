"""Persistent run state for the state-driven scheduler.

The scheduler treats ``harness_state.json`` as the source of truth. Every
decision is made from the latest file contents so a process can be stopped and
restarted without depending on chat or Python object memory.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATE_FILE = "harness_state.json"
STATE_VERSION = 1

TERMINAL_STATUSES = {"completed", "error", "waiting_confirmation"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class StateError(ValueError):
    """Raised when a state file is missing required data or is invalid."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run_state(
    prompt: str,
    workspace: str | Path,
    profile: str = "auto",
    run_id: str | None = None,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    """Create the initial persisted state for a run."""
    created_at = now_iso()
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    workspace_path = str(Path(workspace).resolve())
    return {
        "version": STATE_VERSION,
        "run_id": run_id,
        "prompt": prompt,
        "workspace": workspace_path,
        "profile": profile,
        "route_decision": None,
        "requires_confirmation": False,
        "active": True,
        "status": "running",
        "phase": "route" if profile == "auto" else "plan",
        "next_action": "route" if profile == "auto" else "plan",
        "round_num": 1,
        "max_rounds": max_rounds,
        "score_history": [],
        "agents": {},
        "artifacts": {},
        "memory_refs": [],
        "events": [],
        "last_event_at": created_at,
        "last_error": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def state_path_for_workspace(workspace: str | Path) -> Path:
    return Path(workspace) / STATE_FILE


def load_state(path: str | Path) -> dict[str, Any]:
    """Load and validate a state file."""
    state_path = Path(path)
    with _path_lock(state_path):
        try:
            raw = state_path.read_text(encoding="utf-8")
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateError(f"Invalid JSON in state file {state_path}: {exc}") from exc
        except OSError as exc:
            raise StateError(f"Cannot read state file {state_path}: {exc}") from exc

    validate_state(state)
    return state


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    """Atomically write state to disk using a same-directory temp file."""
    state_copy = deepcopy(state)
    state_copy["updated_at"] = now_iso()
    validate_state(state_copy)

    state_path = Path(path)
    with _path_lock(state_path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            dir=str(state_path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state_copy, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            _replace_with_retry(tmp_name, state_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def update_state(path: str | Path, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Load, mutate, save, and return a state file."""
    state = load_state(path)
    mutator(state)
    save_state(path, state)
    return load_state(path)


def append_event(state: dict[str, Any], event_type: str, data: dict[str, Any] | None = None) -> None:
    """Append a bounded event entry to state."""
    event = {
        "t": now_iso(),
        "type": event_type,
        "data": data or {},
    }
    events = state.setdefault("events", [])
    events.append(event)
    if len(events) > 500:
        del events[:-500]
    state["last_event_at"] = event["t"]


def validate_state(state: dict[str, Any]) -> None:
    required = {
        "version",
        "run_id",
        "prompt",
        "workspace",
        "profile",
        "active",
        "status",
        "phase",
        "next_action",
        "round_num",
        "score_history",
        "created_at",
        "updated_at",
    }
    missing = sorted(required - set(state))
    if missing:
        raise StateError(f"State missing required fields: {', '.join(missing)}")
    if state["version"] != STATE_VERSION:
        raise StateError(f"Unsupported state version: {state['version']}")
    if not isinstance(state["score_history"], list):
        raise StateError("State field score_history must be a list")
    if state["next_action"] is not None and not isinstance(state["next_action"], str):
        raise StateError("State field next_action must be a string or null")


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _replace_with_retry(tmp_name: str, state_path: Path) -> None:
    last_error: OSError | None = None
    for _attempt in range(8):
        try:
            os.replace(tmp_name, state_path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error:
        raise last_error
