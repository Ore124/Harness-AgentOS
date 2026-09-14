"""Deterministic reducer for durable workflow event streams."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from orchestrator.domain import (
    DomainError,
    PlanDocumentV1,
    RunStatus,
    TERMINAL_RUN_STATUSES,
    WorkflowEvent,
    WorkItemKind,
    WorkItemStatus,
)
from orchestrator.plan_graph import plan_is_complete


RUN_TRANSITIONS = {
    RunStatus.QUEUED.value: {
        RunStatus.RUNNING.value,
        RunStatus.PAUSED.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.RUNNING.value: {
        RunStatus.PAUSED.value,
        RunStatus.WAITING_APPROVAL.value,
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.PAUSED.value: {
        RunStatus.RUNNING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.WAITING_APPROVAL.value: {
        RunStatus.RUNNING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
}

WORK_TRANSITIONS = {
    WorkItemStatus.PENDING.value: {
        WorkItemStatus.READY.value,
        WorkItemStatus.BLOCKED.value,
        WorkItemStatus.CANCELLED.value,
    },
    WorkItemStatus.READY.value: {
        WorkItemStatus.LEASED.value,
        WorkItemStatus.BLOCKED.value,
        WorkItemStatus.CANCELLED.value,
    },
    WorkItemStatus.LEASED.value: {
        WorkItemStatus.RUNNING.value,
        WorkItemStatus.VERIFYING.value,
        WorkItemStatus.READY.value,
        WorkItemStatus.FAILED.value,
        WorkItemStatus.CANCELLED.value,
    },
    WorkItemStatus.RUNNING.value: {
        WorkItemStatus.SUCCEEDED.value,
        WorkItemStatus.FAILED.value,
        WorkItemStatus.READY.value,
        WorkItemStatus.BLOCKED.value,
        WorkItemStatus.CANCELLED.value,
    },
    WorkItemStatus.VERIFYING.value: {
        WorkItemStatus.SUCCEEDED.value,
        WorkItemStatus.FAILED.value,
        WorkItemStatus.READY.value,
        WorkItemStatus.BLOCKED.value,
        WorkItemStatus.CANCELLED.value,
    },
    WorkItemStatus.FAILED.value: {
        WorkItemStatus.READY.value,
        WorkItemStatus.BLOCKED.value,
        WorkItemStatus.CANCELLED.value,
    },
    WorkItemStatus.BLOCKED.value: {
        WorkItemStatus.READY.value,
        WorkItemStatus.CANCELLED.value,
    },
}


def empty_aggregate(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "version": 0,
        "project_id": None,
        "goal": "",
        "status": None,
        "policy": {},
        "current_plan_revision": None,
        "plan": None,
        "work_item_statuses": {},
        "current_attempts": {},
        "latest_checkpoints": {},
        "verified_revision": None,
        "evidence": [],
        "artifacts": [],
        "approvals": {},
        "created_at": None,
        "updated_at": None,
    }


def apply_event(
    aggregate: Mapping[str, Any] | None,
    event: WorkflowEvent | Mapping[str, Any],
) -> dict[str, Any]:
    event = _coerce_event(event)
    state = deepcopy(dict(aggregate or empty_aggregate(event.run_id)))
    if state["run_id"] != event.run_id:
        raise DomainError("event run_id does not match aggregate")
    expected_version = int(state.get("version", 0)) + 1
    if event.version is not None and event.version != expected_version:
        raise DomainError(
            f"event version {event.version} is not next aggregate version {expected_version}"
        )

    payload = event.payload
    event_type = event.event_type
    if event_type == "run_created":
        if state["version"] != 0:
            raise DomainError("run_created can only be the first event")
        state.update(
            project_id=str(payload["project_id"]),
            goal=str(payload["goal"]),
            status=RunStatus.QUEUED.value,
            policy=deepcopy(payload.get("policy") or {}),
            workspace=(payload.get("policy") or {}).get("workspace"),
            created_at=event.created_at,
        )
    elif state.get("status") is None:
        raise DomainError("run_created must precede other events")
    elif event_type == "plan_committed":
        plan = PlanDocumentV1.from_dict(payload["plan"])
        current = state.get("current_plan_revision")
        if current is not None and plan.revision != int(current) + 1:
            raise DomainError("plan revisions must be contiguous")
        if current is None and plan.revision != 1:
            raise DomainError("first plan revision must be 1")
        old_statuses = state["work_item_statuses"]
        state["plan"] = plan.to_dict()
        state["current_plan_revision"] = plan.revision
        state["verified_revision"] = None
        for approval in state["approvals"].values():
            if approval.get("status") in {"pending", "approved"} and approval.get("plan_revision") != plan.revision:
                approval["status"] = "invalidated"
        state["work_item_statuses"] = {
            item.id: old_statuses.get(item.id, WorkItemStatus.PENDING.value)
            for item in plan.work_items
        }
    elif event_type == "work_ready":
        _transition_work(state, str(payload["work_item_id"]), WorkItemStatus.READY.value)
    elif event_type == "work_leased":
        item_id = str(payload["work_item_id"])
        _transition_work(state, item_id, WorkItemStatus.LEASED.value)
        state["current_attempts"][item_id] = str(payload["attempt_id"])
    elif event_type == "work_started":
        item_id = str(payload["work_item_id"])
        item = _plan_item(state, item_id)
        target = (
            WorkItemStatus.VERIFYING.value
            if item["kind"] == WorkItemKind.VERIFY.value
            else WorkItemStatus.RUNNING.value
        )
        _transition_work(state, item_id, target)
    elif event_type == "checkpoint_committed":
        item_id = str(payload["work_item_id"])
        state["latest_checkpoints"][item_id] = {
            "attempt_id": str(payload["attempt_id"]),
            "sequence": int(payload["sequence"]),
            "checkpoint": deepcopy(payload.get("checkpoint") or {}),
        }
    elif event_type == "work_succeeded":
        item_id = str(payload["work_item_id"])
        _transition_work(state, item_id, WorkItemStatus.SUCCEEDED.value)
        state["current_attempts"].pop(item_id, None)
        if _plan_item(state, item_id)["kind"] == WorkItemKind.VERIFY.value:
            state["verified_revision"] = state["current_plan_revision"]
    elif event_type == "work_failed":
        item_id = str(payload["work_item_id"])
        next_status = str(payload.get("next_status", WorkItemStatus.FAILED.value))
        if next_status not in {
            WorkItemStatus.FAILED.value,
            WorkItemStatus.READY.value,
            WorkItemStatus.BLOCKED.value,
        }:
            raise DomainError(f"invalid work failure target: {next_status}")
        _transition_work(state, item_id, next_status)
        state["current_attempts"].pop(item_id, None)
        state["verified_revision"] = None
        state["evidence"].append(
            {
                "event_id": event.event_id,
                "kind": "execution_failure",
                "work_item_id": item_id,
                "attempt_id": payload.get("attempt_id"),
                "outcome": deepcopy(payload.get("outcome") or {}),
                "next_status": next_status,
            }
        )
    elif event_type == "work_blocked":
        _transition_work(
            state, str(payload["work_item_id"]), WorkItemStatus.BLOCKED.value
        )
    elif event_type == "work_cancelled":
        item_id = str(payload["work_item_id"])
        _transition_work(state, item_id, WorkItemStatus.CANCELLED.value)
        state["current_attempts"].pop(item_id, None)
    elif event_type == "evidence_recorded":
        evidence = deepcopy(payload)
        evidence.setdefault("event_id", event.event_id)
        state["evidence"].append(evidence)
    elif event_type == "artifact_registered":
        artifact = deepcopy(payload)
        artifact.setdefault("event_id", event.event_id)
        if not any(item.get("id") == artifact.get("id") for item in state["artifacts"]):
            state["artifacts"].append(artifact)
    elif event_type == "approval_requested":
        approval_id = str(payload["approval_id"])
        state["approvals"][approval_id] = {**deepcopy(payload), "status": "pending"}
        _transition_run(state, RunStatus.WAITING_APPROVAL.value)
    elif event_type == "approval_decided":
        approval_id = str(payload["approval_id"])
        approval = state["approvals"].get(approval_id)
        if not approval or approval.get("status") != "pending":
            raise DomainError("approval is missing or already decided")
        approval.update(
            status="approved" if payload.get("approved") else "rejected",
            decided_by=payload.get("decided_by"),
            decided_at=event.created_at,
        )
        _transition_run(
            state,
            RunStatus.RUNNING.value if payload.get("approved") else RunStatus.FAILED.value,
        )
    elif event_type == "run_status_changed":
        target = str(payload["status"])
        if target == RunStatus.COMPLETED.value:
            plan_data = state.get("plan")
            if not plan_data or not plan_is_complete(
                PlanDocumentV1.from_dict(plan_data),
                state["work_item_statuses"],
                verified_revision=state.get("verified_revision"),
            ):
                raise DomainError("run cannot complete without current verified plan")
        _transition_run(state, target)
    else:
        raise DomainError(f"unknown workflow event type: {event_type}")

    state["version"] = expected_version
    state["updated_at"] = event.created_at
    return state


def replay_events(
    run_id: str,
    events: Iterable[WorkflowEvent | Mapping[str, Any]],
) -> dict[str, Any]:
    state = empty_aggregate(run_id)
    for event in events:
        state = apply_event(state, event)
    return state


def _transition_run(state: dict[str, Any], target: str) -> None:
    current = state.get("status")
    if current == target:
        return
    if current in TERMINAL_RUN_STATUSES:
        raise DomainError(f"terminal run cannot transition from {current} to {target}")
    if target not in RUN_TRANSITIONS.get(current, set()):
        raise DomainError(f"invalid run transition from {current} to {target}")
    state["status"] = target


def _transition_work(state: dict[str, Any], item_id: str, target: str) -> None:
    if item_id not in state["work_item_statuses"]:
        raise DomainError(f"unknown work item: {item_id}")
    current = state["work_item_statuses"][item_id]
    if current == target:
        return
    if target not in WORK_TRANSITIONS.get(current, set()):
        raise DomainError(f"invalid work transition for {item_id}: {current} to {target}")
    state["work_item_statuses"][item_id] = target


def _plan_item(state: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    for item in (state.get("plan") or {}).get("work_items", []):
        if item.get("id") == item_id:
            return item
    raise DomainError(f"work item {item_id!r} is absent from current plan")


def _coerce_event(value: WorkflowEvent | Mapping[str, Any]) -> WorkflowEvent:
    if isinstance(value, WorkflowEvent):
        return value
    return WorkflowEvent(
        event_id=str(value["event_id"]),
        event_type=str(value["event_type"]),
        payload=deepcopy(value.get("payload") or {}),
        run_id=str(value["run_id"]),
        version=int(value["version"]) if value.get("version") is not None else None,
        actor=str(value.get("actor", "system")),
        created_at=str(value["created_at"]),
    )
