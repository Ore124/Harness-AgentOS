"""Pure DAG validation, patching, and scheduling helpers."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from orchestrator.domain import (
    ConcurrencyError,
    DomainError,
    PlanDocumentV1,
    PlanPatchV1,
    WorkItem,
    WorkItemKind,
    WorkItemStatus,
)


def apply_plan_patch(
    plan: PlanDocumentV1,
    patch: PlanPatchV1,
    statuses: Mapping[str, str] | None = None,
    *,
    max_total_seconds: int | None = None,
    max_total_tokens: int | None = None,
    max_total_cost_usd: float | None = None,
) -> PlanDocumentV1:
    """Apply a replan patch without rewriting completed work history."""
    if patch.base_revision != plan.revision:
        raise ConcurrencyError(
            f"patch targets revision {patch.base_revision}, current is {plan.revision}"
        )
    if not patch.reason.strip():
        raise DomainError("plan patch requires a reason")

    statuses = dict(statuses or {})
    succeeded = {
        item_id
        for item_id, status in statuses.items()
        if status == WorkItemStatus.SUCCEEDED.value
    }
    items: dict[str, WorkItem] = {item.id: item for item in plan.work_items}

    for item_id in patch.cancel:
        if item_id in succeeded:
            raise DomainError(f"cannot cancel succeeded work item {item_id!r}")
        if item_id not in items:
            raise DomainError(f"cannot cancel missing work item {item_id!r}")
        del items[item_id]

    for item in patch.update:
        if item.id not in items:
            raise DomainError(f"cannot update missing work item {item.id!r}")
        if item.id in succeeded and item != items[item.id]:
            raise DomainError(f"cannot modify succeeded work item {item.id!r}")
        items[item.id] = item

    for item in patch.add:
        if item.id in items:
            raise DomainError(f"cannot add duplicate work item {item.id!r}")
        items[item.id] = item

    required = tuple(
        item_id
        for item_id in plan.completion_policy.required_work_item_ids
        if item_id in items
    ) + tuple(item.id for item in patch.add)
    updated = replace(
        plan,
        revision=plan.revision + 1,
        assumptions=plan.assumptions + (
            f"Replan: {patch.reason.strip()}",
        ),
        work_items=tuple(items.values()),
        completion_policy=replace(
            plan.completion_policy,
            required_work_item_ids=required,
        ),
    )
    updated.validate()
    if max_total_seconds is not None:
        active_seconds = sum(
            item.budget.max_seconds
            for item in updated.work_items
            if statuses.get(item.id) != WorkItemStatus.SUCCEEDED.value
        )
        if active_seconds > max_total_seconds:
            raise DomainError(
                f"active plan budget {active_seconds}s exceeds run budget {max_total_seconds}s"
            )
    active_items = tuple(
        item
        for item in updated.work_items
        if statuses.get(item.id) != WorkItemStatus.SUCCEEDED.value
    )
    active_tokens = sum(item.budget.max_tokens or 0 for item in active_items)
    if max_total_tokens is not None and active_tokens > max_total_tokens:
        raise DomainError(
            f"active plan budget {active_tokens} tokens exceeds run budget {max_total_tokens} tokens"
        )
    active_cost = sum(item.budget.max_cost_usd or 0 for item in active_items)
    if max_total_cost_usd is not None and active_cost > max_total_cost_usd:
        raise DomainError(
            f"active plan budget ${active_cost:.4f} exceeds run budget ${max_total_cost_usd:.4f}"
        )
    return updated


def ready_work_item_ids(
    plan: PlanDocumentV1,
    statuses: Mapping[str, str],
) -> tuple[str, ...]:
    """Return deterministic ready work in declaration order."""
    ready: list[str] = []
    for item in plan.work_items:
        status = statuses.get(item.id, WorkItemStatus.PENDING.value)
        if status not in {
            WorkItemStatus.PENDING.value,
            WorkItemStatus.READY.value,
        }:
            continue
        dependency_statuses = [statuses.get(dep) for dep in item.dependencies]
        if any(
            value in {
                WorkItemStatus.FAILED.value,
                WorkItemStatus.CANCELLED.value,
                WorkItemStatus.BLOCKED.value,
            }
            for value in dependency_statuses
        ):
            continue
        if all(value == WorkItemStatus.SUCCEEDED.value for value in dependency_statuses):
            ready.append(item.id)
    return tuple(ready)


def blocked_work_item_ids(
    plan: PlanDocumentV1,
    statuses: Mapping[str, str],
) -> tuple[str, ...]:
    blocked: list[str] = []
    for item in plan.work_items:
        if statuses.get(item.id, WorkItemStatus.PENDING.value) not in {
            WorkItemStatus.PENDING.value,
            WorkItemStatus.READY.value,
        }:
            continue
        if any(
            statuses.get(dep)
            in {
                WorkItemStatus.FAILED.value,
                WorkItemStatus.CANCELLED.value,
                WorkItemStatus.BLOCKED.value,
            }
            for dep in item.dependencies
        ):
            blocked.append(item.id)
    return tuple(blocked)


def plan_is_complete(
    plan: PlanDocumentV1,
    statuses: Mapping[str, str],
    *,
    verified_revision: int | None,
) -> bool:
    required = plan.completion_policy.required_work_item_ids or tuple(
        item.id for item in plan.work_items
    )
    if not required or not all(
        statuses.get(item_id) == WorkItemStatus.SUCCEEDED.value
        for item_id in required
    ):
        return False
    if not plan.completion_policy.require_verifier:
        return True
    verifier_ids = {
        item.id for item in plan.work_items if item.kind == WorkItemKind.VERIFY.value
    }
    return bool(
        verifier_ids
        and any(statuses.get(item_id) == WorkItemStatus.SUCCEEDED.value for item_id in verifier_ids)
        and verified_revision == plan.revision
    )


def graph_view(plan: PlanDocumentV1, statuses: Mapping[str, str]) -> dict[str, Any]:
    return {
        "revision": plan.revision,
        "nodes": [
            {
                **item.to_dict(),
                "status": statuses.get(item.id, WorkItemStatus.PENDING.value),
            }
            for item in plan.work_items
        ],
        "edges": [
            {"from": dependency, "to": item.id}
            for item in plan.work_items
            for dependency in item.dependencies
        ],
    }
