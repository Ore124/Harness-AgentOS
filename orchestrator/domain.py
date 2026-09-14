"""Versioned durable-workflow domain contracts.

The module is intentionally free of database, network, and agent imports so
plans and event streams can be validated and replayed deterministically.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping


class DomainError(ValueError):
    """Raised when a durable workflow contract is invalid."""


class ConcurrencyError(RuntimeError):
    """Raised when an optimistic aggregate version is stale."""


class StaleLeaseError(RuntimeError):
    """Raised when an old worker tries to commit with an obsolete fence."""


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkItemStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkItemKind(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    DIAGNOSE = "diagnose"
    INTEGRATE = "integrate"
    APPROVAL = "approval"


class DecisionType(str, Enum):
    CONTINUE = "continue"
    REPAIR = "repair"
    REPLAN = "replan"
    WAIT_APPROVAL = "wait_approval"
    COMPLETE = "complete"
    FAIL = "fail"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}
TERMINAL_WORK_STATUSES = {
    WorkItemStatus.SUCCEEDED.value,
    WorkItemStatus.FAILED.value,
    WorkItemStatus.CANCELLED.value,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkBudget:
    max_seconds: int = 900
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_attempts: int = 3

    def validate(self) -> None:
        if self.max_seconds <= 0:
            raise DomainError("budget.max_seconds must be positive")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise DomainError("budget.max_tokens must be positive")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise DomainError("budget.max_cost_usd must be positive")
        if not 1 <= self.max_attempts <= 20:
            raise DomainError("budget.max_attempts must be between 1 and 20")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "WorkBudget":
        data = dict(value or {})
        return cls(
            max_seconds=int(data.get("max_seconds", 900)),
            max_tokens=_optional_int(data.get("max_tokens")),
            max_cost_usd=_optional_float(data.get("max_cost_usd")),
            max_attempts=int(data.get("max_attempts", 3)),
        )


@dataclass(frozen=True)
class WorkItem:
    id: str
    title: str
    kind: str
    agent_role: str
    dependencies: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    risk_level: str = RiskLevel.LOW.value
    budget: WorkBudget = field(default_factory=WorkBudget)

    def validate(self) -> None:
        if not self.id or not self.id.strip():
            raise DomainError("work item id is required")
        if not self.title.strip():
            raise DomainError(f"work item {self.id!r} requires a title")
        if self.kind not in {item.value for item in WorkItemKind}:
            raise DomainError(f"work item {self.id!r} has unknown kind {self.kind!r}")
        if not self.agent_role.strip():
            raise DomainError(f"work item {self.id!r} requires an agent_role")
        if self.id in self.dependencies:
            raise DomainError(f"work item {self.id!r} cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise DomainError(f"work item {self.id!r} has duplicate dependencies")
        if self.risk_level not in {item.value for item in RiskLevel}:
            raise DomainError(f"work item {self.id!r} has unknown risk level")
        self.budget.validate()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "dependencies",
            "expected_outputs",
            "acceptance_checks",
            "required_capabilities",
        ):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkItem":
        item = cls(
            id=str(value.get("id", "")),
            title=str(value.get("title", "")),
            kind=str(value.get("kind", WorkItemKind.EXECUTE.value)),
            agent_role=str(value.get("agent_role", "executor")),
            dependencies=_string_tuple(value.get("dependencies")),
            inputs=dict(value.get("inputs") or {}),
            expected_outputs=_string_tuple(value.get("expected_outputs")),
            acceptance_checks=_string_tuple(value.get("acceptance_checks")),
            required_capabilities=_string_tuple(value.get("required_capabilities")),
            risk_level=str(value.get("risk_level", RiskLevel.LOW.value)),
            budget=WorkBudget.from_dict(value.get("budget")),
        )
        item.validate()
        return item


@dataclass(frozen=True)
class CompletionPolicy:
    required_work_item_ids: tuple[str, ...] = ()
    require_verifier: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_work_item_ids": list(self.required_work_item_ids),
            "require_verifier": self.require_verifier,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "CompletionPolicy":
        data = dict(value or {})
        return cls(
            required_work_item_ids=_string_tuple(data.get("required_work_item_ids")),
            require_verifier=bool(data.get("require_verifier", True)),
        )


@dataclass(frozen=True)
class PlanDocumentV1:
    revision: int
    goal: str
    success_criteria: tuple[str, ...]
    assumptions: tuple[str, ...]
    work_items: tuple[WorkItem, ...]
    completion_policy: CompletionPolicy = field(default_factory=CompletionPolicy)
    schema_version: int = 1

    def validate(
        self,
        *,
        max_total_seconds: int | None = None,
        max_total_tokens: int | None = None,
        max_total_cost_usd: float | None = None,
    ) -> None:
        if self.schema_version != 1:
            raise DomainError(f"unsupported plan schema version: {self.schema_version}")
        if self.revision < 1:
            raise DomainError("plan revision must be positive")
        if not self.goal.strip():
            raise DomainError("plan goal is required")
        if not self.success_criteria:
            raise DomainError("plan requires at least one success criterion")
        if not self.work_items:
            raise DomainError("plan requires at least one work item")
        by_id: dict[str, WorkItem] = {}
        for item in self.work_items:
            item.validate()
            if item.id in by_id:
                raise DomainError(f"duplicate work item id: {item.id}")
            by_id[item.id] = item
        for item in self.work_items:
            missing = sorted(set(item.dependencies) - set(by_id))
            if missing:
                raise DomainError(
                    f"work item {item.id!r} has missing dependencies: {', '.join(missing)}"
                )
        _assert_acyclic(by_id)
        required = set(self.completion_policy.required_work_item_ids)
        missing_required = sorted(required - set(by_id))
        if missing_required:
            raise DomainError(
                "completion policy references missing work items: "
                + ", ".join(missing_required)
            )
        if self.completion_policy.require_verifier and not any(
            item.kind == WorkItemKind.VERIFY.value for item in self.work_items
        ):
            raise DomainError("completion policy requires a verifier work item")
        total_seconds = sum(item.budget.max_seconds for item in self.work_items)
        if max_total_seconds is not None and total_seconds > max_total_seconds:
            raise DomainError(
                f"plan budget {total_seconds}s exceeds run budget {max_total_seconds}s"
            )
        total_tokens = sum(item.budget.max_tokens or 0 for item in self.work_items)
        if max_total_tokens is not None and total_tokens > max_total_tokens:
            raise DomainError(
                f"plan budget {total_tokens} tokens exceeds run budget {max_total_tokens} tokens"
            )
        total_cost = sum(item.budget.max_cost_usd or 0 for item in self.work_items)
        if max_total_cost_usd is not None and total_cost > max_total_cost_usd:
            raise DomainError(
                f"plan budget ${total_cost:.4f} exceeds run budget ${max_total_cost_usd:.4f}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "assumptions": list(self.assumptions),
            "work_items": [item.to_dict() for item in self.work_items],
            "completion_policy": self.completion_policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanDocumentV1":
        plan = cls(
            schema_version=int(value.get("schema_version", 1)),
            revision=int(value.get("revision", 0)),
            goal=str(value.get("goal", "")),
            success_criteria=_string_tuple(value.get("success_criteria")),
            assumptions=_string_tuple(value.get("assumptions")),
            work_items=tuple(
                WorkItem.from_dict(item) for item in (value.get("work_items") or [])
            ),
            completion_policy=CompletionPolicy.from_dict(
                value.get("completion_policy")
            ),
        )
        plan.validate()
        return plan


@dataclass(frozen=True)
class PlanPatchV1:
    base_revision: int
    reason: str
    evidence_refs: tuple[str, ...] = ()
    add: tuple[WorkItem, ...] = ()
    update: tuple[WorkItem, ...] = ()
    cancel: tuple[str, ...] = ()
    schema_version: int = 1

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanPatchV1":
        if int(value.get("schema_version", 1)) != 1:
            raise DomainError("unsupported plan patch schema version")
        return cls(
            base_revision=int(value.get("base_revision", 0)),
            reason=str(value.get("reason", "")),
            evidence_refs=_string_tuple(value.get("evidence_refs")),
            add=tuple(WorkItem.from_dict(item) for item in value.get("add") or []),
            update=tuple(
                WorkItem.from_dict(item) for item in value.get("update") or []
            ),
            cancel=_string_tuple(value.get("cancel")),
        )


@dataclass(frozen=True)
class WorkflowEvent:
    event_type: str
    payload: dict[str, Any]
    run_id: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int | None = None
    actor: str = "system"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str = "system",
        event_id: str | None = None,
    ) -> "WorkflowEvent":
        return cls(
            event_id=event_id or str(uuid.uuid4()),
            event_type=event_type,
            payload=dict(payload or {}),
            run_id=run_id,
            actor=actor,
        )


@dataclass(frozen=True)
class LeasedWorkItem:
    run_id: str
    work_item_id: str
    attempt_id: str
    fencing_token: int
    lease_expires_at: str
    item: dict[str, Any]
    checkpoint: dict[str, Any] | None = None


@dataclass(frozen=True)
class Run:
    id: str
    project_id: str
    goal: str
    status: str = RunStatus.QUEUED.value
    version: int = 0
    current_plan_revision: int | None = None
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanRevision:
    run_id: str
    revision: int
    plan: PlanDocumentV1
    reason: str


@dataclass(frozen=True)
class Attempt:
    id: str
    run_id: str
    work_item_id: str
    worker_id: str
    fencing_token: int
    status: str
    checkpoint: dict[str, Any] | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    run_id: str
    kind: str
    payload: dict[str, Any]
    work_item_id: str | None = None


@dataclass(frozen=True)
class Decision:
    kind: str
    reason: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Artifact:
    id: str
    run_id: str
    uri: str
    sha256: str
    size: int
    sensitivity: str = "internal"


@dataclass(frozen=True)
class Approval:
    id: str
    run_id: str
    plan_revision: int
    effect_digest: str
    status: str = "pending"


@dataclass(frozen=True)
class AuditEvent:
    id: str
    project_id: str
    event_type: str
    actor: str
    created_at: str
    run_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxEvent:
    id: str
    event_id: str
    run_id: str
    event_type: str
    payload: dict[str, Any]
    published_at: str | None = None


def _assert_acyclic(items: Mapping[str, WorkItem]) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            start = visiting.index(item_id)
            cycle = visiting[start:] + [item_id]
            raise DomainError("plan contains dependency cycle: " + " -> ".join(cycle))
        visiting.append(item_id)
        for dependency in items[item_id].dependencies:
            visit(dependency)
        visiting.pop()
        visited.add(item_id)

    for item_id in items:
        visit(item_id)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Iterable):
        raise DomainError("expected a string collection")
    return tuple(str(item) for item in value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
