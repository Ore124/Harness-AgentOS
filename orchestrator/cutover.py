"""Deterministic project rollout and shadow-replay comparison helpers."""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping


def select_runtime(
    project_id: str,
    routing_key: str,
    *,
    default_runtime: str,
    durable_projects: Iterable[str] = (),
    rollout_percent: int = 0,
) -> str:
    if default_runtime not in {"legacy", "durable"}:
        raise ValueError("default runtime must be legacy or durable")
    if not 0 <= rollout_percent <= 100:
        raise ValueError("rollout percent must be between 0 and 100")
    if default_runtime == "durable":
        return "durable"
    if project_id not in set(durable_projects) or rollout_percent == 0:
        return "legacy"
    bucket = int.from_bytes(
        hashlib.sha256(f"{project_id}:{routing_key}".encode("utf-8")).digest()[:8],
        "big",
    ) % 100
    return "durable" if bucket < rollout_percent else "legacy"


def compare_shadow_terminal(
    legacy: Mapping[str, Any], durable: Mapping[str, Any]
) -> dict[str, Any]:
    legacy_success = bool(
        legacy.get("task_success")
        if legacy.get("task_success") is not None
        else legacy.get("status") == "completed"
    )
    durable_success = durable.get("status") == "completed" and durable.get(
        "verified_revision"
    ) == durable.get("current_plan_revision")
    return {
        "comparable": bool(legacy.get("status") and durable.get("status")),
        "legacy_success": legacy_success,
        "durable_success": durable_success,
        "terminal_match": legacy_success == durable_success,
        "durable_verified": durable.get("verified_revision")
        == durable.get("current_plan_revision"),
    }
