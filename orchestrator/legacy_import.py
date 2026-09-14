"""Idempotent import of legacy JSON run state into the durable event model."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from orchestrator.controller import DurableController
from orchestrator.canonical_trace import replay_trace
from orchestrator.domain import WorkflowEvent
from orchestrator.runtime import standard_profile_plan


def import_legacy_state(
    state_path: str | Path,
    repository,
    *,
    project_id: str = "legacy-imports",
    actor: str = "migration",
) -> dict[str, Any]:
    path = Path(state_path).expanduser().resolve()
    raw = path.read_bytes()
    legacy = json.loads(raw.decode("utf-8"))
    legacy_run_id = str(legacy.get("run_id") or path.parent.name)
    durable_run_id = "legacy-" + hashlib.sha256(
        f"{path}:{legacy_run_id}".encode("utf-8")
    ).hexdigest()[:24]
    idempotency_key = f"legacy-state:{hashlib.sha256(raw).hexdigest()}"
    goal = str(legacy.get("prompt") or "Imported legacy run")
    profile = str(legacy.get("profile") or "app-builder")
    workspace = str(Path(legacy.get("workspace") or path.parent).resolve())
    controller = DurableController(repository)
    state = controller.create_run(
        project_id,
        goal,
        run_id=durable_run_id,
        idempotency_key=idempotency_key,
        actor=actor,
        policy={
            "profile": profile,
            "workspace": workspace,
            "legacy_run_id": legacy_run_id,
            "imported": True,
            "max_total_seconds": 86400,
        },
    )
    if state.get("plan") is not None:
        return state

    plan = standard_profile_plan(goal, profile, max_seconds=86400)
    state = controller.commit_plan(
        durable_run_id,
        plan,
        expected_version=state["version"],
        reason="legacy state import",
        actor=actor,
    )
    events = [
        WorkflowEvent.create(
            durable_run_id,
            "run_status_changed",
            {"status": "running"},
            actor=actor,
        ),
        WorkflowEvent.create(
            durable_run_id,
            "evidence_recorded",
            {
                "kind": "legacy_state_import",
                "source": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "legacy_status": legacy.get("status"),
                "legacy_phase": legacy.get("phase"),
                "legacy_validation": legacy.get("validation"),
                "legacy_artifacts": legacy.get("artifacts"),
            },
            actor=actor,
        ),
    ]
    events.extend(
        WorkflowEvent.create(durable_run_id, "evidence_recorded", item, actor=actor)
        for item in _legacy_bundle_evidence(Path(workspace), legacy_run_id)
    )
    legacy_status = str(legacy.get("status") or "running")
    legacy_phase = str(legacy.get("phase") or "plan")
    completed_items: list[str] = []
    if legacy_phase in {"build", "evaluate", "analyze", "complete"}:
        completed_items.append("plan")
    if legacy_phase in {"evaluate", "analyze", "complete"}:
        completed_items.append("build")
    verified = (
        legacy_status == "completed"
        and (legacy.get("validation") or {}).get("status") == "verified"
    )
    if verified:
        completed_items = ["plan", "build", "verify"]
    for item_id in completed_items:
        attempt_id = f"import-{item_id}"
        events.extend(
            [
                WorkflowEvent.create(durable_run_id, "work_ready", {"work_item_id": item_id}, actor=actor),
                WorkflowEvent.create(
                    durable_run_id,
                    "work_leased",
                    {"work_item_id": item_id, "attempt_id": attempt_id},
                    actor=actor,
                ),
                WorkflowEvent.create(durable_run_id, "work_started", {"work_item_id": item_id}, actor=actor),
                WorkflowEvent.create(durable_run_id, "work_succeeded", {"work_item_id": item_id}, actor=actor),
            ]
        )
    if verified:
        events.append(
            WorkflowEvent.create(
                durable_run_id,
                "run_status_changed",
                {"status": "completed"},
                actor=actor,
            )
        )
    elif legacy_status == "error":
        events.append(
            WorkflowEvent.create(
                durable_run_id,
                "run_status_changed",
                {"status": "failed"},
                actor=actor,
            )
        )
    elif legacy_status == "paused":
        events.append(
            WorkflowEvent.create(
                durable_run_id,
                "run_status_changed",
                {"status": "paused"},
                actor=actor,
            )
        )
    state = repository.append(durable_run_id, state["version"], events)
    if state["status"] == "running":
        controller.reconcile(durable_run_id)
        state = repository.get_snapshot(durable_run_id)
    return state


def _legacy_bundle_evidence(workspace: Path, legacy_run_id: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    trace_path = workspace / ".harness" / "canonical_trace.jsonl"
    if trace_path.exists():
        try:
            replay = replay_trace(trace_path)
            evidence.append(
                {
                    "kind": "legacy_canonical_trace_import",
                    "source": str(trace_path),
                    "sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
                    "valid": replay.get("valid"),
                    "invalid_reasons": replay.get("invalid_reasons", []),
                    "final_status": replay.get("final_status"),
                    "task_success": replay.get("task_success"),
                    "totals": replay.get("totals", {}),
                }
            )
        except (OSError, ValueError, KeyError) as exc:
            evidence.append(
                {
                    "kind": "legacy_canonical_trace_import",
                    "source": str(trace_path),
                    "valid": False,
                    "invalid_reasons": [f"{type(exc).__name__}: {exc}"],
                }
            )
    store_path = workspace / ".harness" / "orchestrator.db"
    if store_path.exists():
        try:
            connection = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
            try:
                event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE run_id = ?", (legacy_run_id,)
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            evidence.append(
                {
                    "kind": "legacy_sqlite_store_import",
                    "source": str(store_path),
                    "sha256": hashlib.sha256(store_path.read_bytes()).hexdigest(),
                    "event_count": event_count,
                }
            )
        except (OSError, sqlite3.Error) as exc:
            evidence.append(
                {
                    "kind": "legacy_sqlite_store_import",
                    "source": str(store_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return evidence
