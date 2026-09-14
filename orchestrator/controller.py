"""Application service for durable planning, reconciliation, and control."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from orchestrator.domain import (
    ConcurrencyError,
    DecisionType,
    DomainError,
    PlanDocumentV1,
    PlanPatchV1,
    RiskLevel,
    RunStatus,
    WorkflowEvent,
    WorkBudget,
    WorkItem,
    WorkItemKind,
    WorkItemStatus,
)
from orchestrator.plan_graph import (
    apply_plan_patch,
    blocked_work_item_ids,
    plan_is_complete,
    ready_work_item_ids,
)
from orchestrator.workflow_repository import WorkflowRepository


class PlannerPort(Protocol):
    def create_plan(self, goal: str, snapshot: Mapping[str, Any]) -> PlanDocumentV1: ...
    def replan(
        self,
        plan: PlanDocumentV1,
        snapshot: Mapping[str, Any],
        evidence: tuple[Mapping[str, Any], ...],
    ) -> PlanPatchV1: ...


@dataclass(frozen=True)
class ControllerDecision:
    action: str
    reason: str
    run_id: str
    version: int
    work_item_ids: tuple[str, ...] = ()


class DurableController:
    """Own run decisions; agents and workers only report durable outcomes."""

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        planner: PlannerPort | None = None,
    ):
        self.repository = repository
        self.planner = planner

    def create_run(
        self,
        project_id: str,
        goal: str,
        *,
        idempotency_key: str | None = None,
        policy: Mapping[str, Any] | None = None,
        actor: str = "system",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return self.repository.create_run(
            project_id,
            goal,
            idempotency_key=idempotency_key,
            policy=policy,
            actor=actor,
            run_id=run_id,
        )

    def commit_plan(
        self,
        run_id: str,
        plan: PlanDocumentV1,
        *,
        expected_version: int | None = None,
        reason: str = "initial plan",
        actor: str = "planner",
    ) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        if snapshot.get("status") in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            raise DomainError("cannot commit a plan to a terminal run")
        expected_revision = 1 if snapshot.get("current_plan_revision") is None else int(snapshot["current_plan_revision"]) + 1
        if plan.revision != expected_revision:
            raise ConcurrencyError(
                f"plan revision must be {expected_revision}, got {plan.revision}"
            )
        max_seconds = _optional_int((snapshot.get("policy") or {}).get("max_total_seconds"))
        max_tokens = _optional_int((snapshot.get("policy") or {}).get("max_total_tokens"))
        max_cost = _optional_float((snapshot.get("policy") or {}).get("max_total_cost_usd"))
        plan.validate(
            max_total_seconds=max_seconds,
            max_total_tokens=max_tokens,
            max_total_cost_usd=max_cost,
        )
        event = WorkflowEvent.create(
            run_id,
            "plan_committed",
            {"plan": plan.to_dict(), "reason": reason},
            actor=actor,
        )
        return self.repository.append(
            run_id,
            snapshot["version"] if expected_version is None else expected_version,
            [event],
        )

    def apply_patch(
        self,
        run_id: str,
        patch: PlanPatchV1,
        *,
        actor: str = "planner",
    ) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        if not snapshot.get("plan"):
            raise DomainError("cannot patch a run without a plan")
        current = PlanDocumentV1.from_dict(snapshot["plan"])
        max_seconds = _optional_int((snapshot.get("policy") or {}).get("max_total_seconds"))
        max_tokens = _optional_int((snapshot.get("policy") or {}).get("max_total_tokens"))
        max_cost = _optional_float((snapshot.get("policy") or {}).get("max_total_cost_usd"))
        updated = apply_plan_patch(
            current,
            patch,
            snapshot.get("work_item_statuses") or {},
            max_total_seconds=max_seconds,
            max_total_tokens=max_tokens,
            max_total_cost_usd=max_cost,
        )
        return self.commit_plan(
            run_id,
            updated,
            expected_version=snapshot["version"],
            reason=patch.reason,
            actor=actor,
        )

    def ensure_plan(self, run_id: str) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        if snapshot.get("plan") is not None:
            return snapshot
        if self.planner is None:
            raise DomainError("run has no plan and no planner is configured")
        return self.commit_plan(
            run_id,
            self.planner.create_plan(snapshot["goal"], snapshot),
        )

    def reconcile(self, run_id: str) -> ControllerDecision:
        for _attempt in range(5):
            snapshot = self.repository.get_snapshot(run_id)
            decision, events = self._decide(snapshot)
            if not events:
                return decision
            try:
                updated = self.repository.append(run_id, snapshot["version"], events)
                return ControllerDecision(
                    action=decision.action,
                    reason=decision.reason,
                    run_id=run_id,
                    version=updated["version"],
                    work_item_ids=decision.work_item_ids,
                )
            except ConcurrencyError:
                continue
        raise ConcurrencyError(f"run {run_id} changed repeatedly during reconciliation")

    def advance(self, run_id: str, *, max_decisions: int = 10) -> ControllerDecision:
        """Reconcile and automatically commit bounded recovery replans."""
        for _ in range(max_decisions):
            snapshot = self.repository.get_snapshot(run_id)
            if snapshot.get("plan") is None:
                self.ensure_plan(run_id)
                continue
            decision = self.reconcile(run_id)
            if decision.action != DecisionType.REPLAN.value:
                return decision
            if self.planner is None:
                return decision
            self.replan_from_evidence(run_id)
        raise DomainError(f"controller exceeded {max_decisions} decisions without stabilizing")

    def request_approval(
        self,
        run_id: str,
        work_item_id: str,
        *,
        actor: str = "controller",
    ) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        item = _item_from_snapshot(snapshot, work_item_id)
        digest = _effect_digest(snapshot["current_plan_revision"], item)
        existing = _matching_approval(snapshot, digest)
        if existing:
            return snapshot
        approval_id = str(uuid.uuid4())
        event = WorkflowEvent.create(
            run_id,
            "approval_requested",
            {
                "approval_id": approval_id,
                "work_item_id": work_item_id,
                "plan_revision": snapshot["current_plan_revision"],
                "effect_digest": digest,
                "risk_level": item.get("risk_level", "low"),
            },
            actor=actor,
        )
        return self.repository.append(run_id, snapshot["version"], [event])

    def decide_approval(
        self,
        run_id: str,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason: str = "",
    ) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        approval = (snapshot.get("approvals") or {}).get(approval_id)
        if not approval or approval.get("status") != "pending":
            raise DomainError("approval is missing, invalidated, or already decided")
        if approval.get("plan_revision") != snapshot.get("current_plan_revision"):
            raise DomainError("approval belongs to an obsolete plan revision")
        event = WorkflowEvent.create(
            run_id,
            "approval_decided",
            {
                "approval_id": approval_id,
                "approved": approved,
                "decided_by": decided_by,
                "reason": reason,
            },
            actor=f"user:{decided_by}",
        )
        return self.repository.append(run_id, snapshot["version"], [event])

    def change_status(
        self,
        run_id: str,
        status: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        events = [
            WorkflowEvent.create(
                run_id,
                "run_status_changed",
                {"status": status},
                actor=actor,
            )
        ]
        if status == RunStatus.CANCELLED.value:
            events.extend(
                WorkflowEvent.create(
                    run_id,
                    "work_cancelled",
                    {"work_item_id": item_id, "reason": "run cancelled"},
                    actor=actor,
                )
                for item_id, item_status in (snapshot.get("work_item_statuses") or {}).items()
                if item_status
                not in {WorkItemStatus.SUCCEEDED.value, WorkItemStatus.CANCELLED.value}
            )
        return self.repository.append(
            run_id,
            snapshot["version"],
            events,
        )

    def record_evidence(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        snapshot = self.repository.get_snapshot(run_id)
        return self.repository.append(
            run_id,
            snapshot["version"],
            [WorkflowEvent.create(run_id, "evidence_recorded", payload, actor=actor)],
        )

    def replan_from_evidence(self, run_id: str) -> dict[str, Any]:
        if self.planner is None:
            raise DomainError("no planner is configured")
        snapshot = self.repository.get_snapshot(run_id)
        plan = PlanDocumentV1.from_dict(snapshot["plan"])
        patch = self.planner.replan(
            plan,
            snapshot,
            tuple(snapshot.get("evidence") or ()),
        )
        return self.apply_patch(run_id, patch)

    def _decide(
        self, snapshot: Mapping[str, Any]
    ) -> tuple[ControllerDecision, list[WorkflowEvent]]:
        run_id = str(snapshot["run_id"])
        status = snapshot.get("status")
        version = int(snapshot["version"])
        if status in {
            RunStatus.PAUSED.value,
            RunStatus.WAITING_APPROVAL.value,
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            return ControllerDecision("continue", f"run is {status}", run_id, version), []
        if snapshot.get("plan") is None:
            return ControllerDecision("replan", "run needs an initial plan", run_id, version), []

        plan = PlanDocumentV1.from_dict(snapshot["plan"])
        statuses = dict(snapshot.get("work_item_statuses") or {})
        if plan_is_complete(
            plan,
            statuses,
            verified_revision=snapshot.get("verified_revision"),
        ):
            return (
                ControllerDecision("complete", "current plan is verified", run_id, version),
                [
                    WorkflowEvent.create(
                        run_id,
                        "run_status_changed",
                        {"status": RunStatus.COMPLETED.value},
                        actor="controller",
                    )
                ],
            )

        events: list[WorkflowEvent] = []
        if status == RunStatus.QUEUED.value:
            events.append(
                WorkflowEvent.create(
                    run_id,
                    "run_status_changed",
                    {"status": RunStatus.RUNNING.value},
                    actor="controller",
                )
            )

        blocked = blocked_work_item_ids(plan, statuses)
        for item_id in blocked:
            events.append(
                WorkflowEvent.create(
                    run_id,
                    "work_blocked",
                    {"work_item_id": item_id, "reason": "dependency failed"},
                    actor="controller",
                )
            )

        ready = list(ready_work_item_ids(plan, statuses))
        approved_ready: list[str] = []
        for item_id in ready:
            item = _item_from_snapshot(snapshot, item_id)
            digest = _effect_digest(plan.revision, item)
            if _requires_approval(item) and not _approved(snapshot, digest):
                if _matching_approval(snapshot, digest) is None:
                    approval_id = str(uuid.uuid4())
                    events.append(
                        WorkflowEvent.create(
                            run_id,
                            "approval_requested",
                            {
                                "approval_id": approval_id,
                                "work_item_id": item_id,
                                "plan_revision": plan.revision,
                                "effect_digest": digest,
                                "risk_level": item.get("risk_level", "low"),
                            },
                            actor="controller",
                        )
                    )
                return (
                    ControllerDecision(
                        "wait_approval",
                        f"work item {item_id} requires approval",
                        run_id,
                        version,
                        (item_id,),
                    ),
                    events,
                )
            if statuses.get(item_id) != WorkItemStatus.READY.value:
                events.append(
                    WorkflowEvent.create(
                        run_id,
                        "work_ready",
                        {"work_item_id": item_id},
                        actor="controller",
                    )
                )
            approved_ready.append(item_id)

        terminal_failures = tuple(
            item_id
            for item_id, item_status in statuses.items()
            if item_status == WorkItemStatus.FAILED.value
        )
        if terminal_failures and not ready:
            max_replans = int((snapshot.get("policy") or {}).get("max_replans", 3))
            replans_used = max(0, plan.revision - 1)
            if replans_used >= max_replans:
                events.append(
                    WorkflowEvent.create(
                        run_id,
                        "run_status_changed",
                        {"status": RunStatus.FAILED.value},
                        actor="controller",
                    )
                )
                action = DecisionType.FAIL.value
                reason = "maximum replans exhausted"
            else:
                action = DecisionType.REPLAN.value if self.planner else DecisionType.FAIL.value
                reason = "terminal work item failure requires diagnosis and replan"
            return (
                ControllerDecision(
                    action,
                    reason,
                    run_id,
                    version,
                    terminal_failures,
                ),
                events,
            )
        return (
            ControllerDecision(
                DecisionType.CONTINUE.value,
                "ready work scheduled" if approved_ready else "work is in progress",
                run_id,
                version,
                tuple(approved_ready),
            ),
            events,
        )


def _item_from_snapshot(snapshot: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    for item in (snapshot.get("plan") or {}).get("work_items", []):
        if item.get("id") == item_id:
            return dict(item)
    raise DomainError(f"unknown work item: {item_id}")


def _requires_approval(item: Mapping[str, Any]) -> bool:
    if item.get("risk_level") in {RiskLevel.HIGH.value, RiskLevel.CRITICAL.value}:
        return True
    inputs = item.get("inputs") or {}
    return any(
        bool(inputs.get(key))
        for key in ("external_publish", "destructive", "secret_refs", "budget_override")
    )


def _effect_digest(plan_revision: int, item: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"plan_revision": plan_revision, "item": item},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _matching_approval(
    snapshot: Mapping[str, Any], effect_digest: str
) -> Mapping[str, Any] | None:
    for approval in (snapshot.get("approvals") or {}).values():
        if approval.get("effect_digest") == effect_digest and approval.get("status") in {
            "pending",
            "approved",
        }:
            return approval
    return None


def _approved(snapshot: Mapping[str, Any], effect_digest: str) -> bool:
    approval = _matching_approval(snapshot, effect_digest)
    return bool(approval and approval.get("status") == "approved")


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


class EvidenceRecoveryPlanner:
    """Deterministic recovery planner that makes diagnostic work explicit.

    The diagnostician remains an Agent. This coordinator only transforms
    durable failure evidence into a bounded, auditable plan revision.
    """

    def create_plan(
        self, goal: str, snapshot: Mapping[str, Any]
    ) -> PlanDocumentV1:
        from orchestrator.runtime import standard_profile_plan

        policy = snapshot.get("policy") or {}
        git_settings = _git_settings_from_policy(policy)
        return standard_profile_plan(
            goal,
            str(policy.get("profile", "app-builder")),
            max_seconds=int(policy.get("max_total_seconds", 3600)),
            max_tokens=_optional_int(policy.get("max_total_tokens")),
            max_cost_usd=_optional_float(policy.get("max_total_cost_usd")),
            execution_capability=str(policy.get("execution_capability", "local")),
            git_settings=git_settings or None,
        )

    def replan(
        self,
        plan: PlanDocumentV1,
        snapshot: Mapping[str, Any],
        evidence: tuple[Mapping[str, Any], ...],
    ) -> PlanPatchV1:
        statuses = snapshot.get("work_item_statuses") or {}
        obsolete_set = {
            item_id
            for item_id, status in statuses.items()
            if status in {WorkItemStatus.FAILED.value, WorkItemStatus.BLOCKED.value}
        }
        changed = True
        while changed:
            changed = False
            for item in plan.work_items:
                if item.id not in obsolete_set and any(
                    dependency in obsolete_set for dependency in item.dependencies
                ):
                    if statuses.get(item.id) != WorkItemStatus.SUCCEEDED.value:
                        obsolete_set.add(item.id)
                        changed = True
        obsolete = tuple(
            item.id for item in plan.work_items if item.id in obsolete_set
        )
        if not obsolete:
            raise DomainError("replan requested without failed or blocked work")
        revision = plan.revision + 1
        failure_count = sum(
            1 for item in evidence if item.get("kind") == "execution_failure"
        )
        strategy = (
            "targeted_fix"
            if failure_count <= 1
            else "reinspect_assumptions"
            if failure_count == 2
            else "escalate_analysis"
        )
        policy = snapshot.get("policy") or {}
        total_seconds = int(policy.get("max_total_seconds", 3600))
        total_tokens = _optional_int(policy.get("max_total_tokens"))
        total_cost = _optional_float(policy.get("max_total_cost_usd"))
        execution_capability = str(policy.get("execution_capability", "local"))
        inputs = {
            "goal": snapshot["goal"],
            "profile": policy.get("profile", "app-builder"),
            "failure_work_items": list(obsolete),
            "recovery_strategy": strategy,
            "evidence_refs": [
                item.get("event_id") for item in evidence[-20:] if item.get("event_id")
            ],
            **_git_settings_from_policy(policy),
        }
        diagnose_id = f"diagnose-r{revision}"
        repair_id = f"repair-r{revision}"
        verify_id = f"verify-r{revision}"
        git_enabled = bool(inputs.get("repository"))
        repair_fraction = 0.6 if git_enabled else 0.7
        added_items = [
            WorkItem(
                diagnose_id,
                "Diagnose repeated failure evidence",
                WorkItemKind.DIAGNOSE.value,
                "diagnostician",
                inputs=inputs,
                expected_outputs=("root-cause diagnosis",),
                budget=_recovery_budget(total_seconds, total_tokens, total_cost, 0.1),
            ),
            WorkItem(
                repair_id,
                "Repair the diagnosed root cause",
                WorkItemKind.EXECUTE.value,
                "executor",
                dependencies=(diagnose_id,),
                inputs=inputs,
                expected_outputs=("corrected workspace revision",),
                acceptance_checks=("failed checks now pass",),
                required_capabilities=(execution_capability,),
                budget=_recovery_budget(
                    total_seconds, total_tokens, total_cost, repair_fraction
                ),
            ),
            WorkItem(
                verify_id,
                "Verify the repaired revision independently",
                WorkItemKind.VERIFY.value,
                "verifier",
                dependencies=(repair_id,),
                inputs=inputs,
                expected_outputs=("fresh verification evidence",),
                acceptance_checks=("current revision passes acceptance",),
                required_capabilities=(execution_capability,),
                budget=_recovery_budget(total_seconds, total_tokens, total_cost, 0.2),
            ),
        ]
        if git_enabled:
            added_items.append(
                WorkItem(
                    f"integrate-r{revision}",
                    "Integrate the verified repair through the merge queue",
                    WorkItemKind.INTEGRATE.value,
                    "integrator",
                    dependencies=(verify_id,),
                    inputs={**inputs, "workspace_item_id": repair_id},
                    expected_outputs=("integrated target branch commit",),
                    required_capabilities=(execution_capability,),
                    budget=_recovery_budget(total_seconds, total_tokens, total_cost, 0.1),
                )
            )
        return PlanPatchV1(
            base_revision=plan.revision,
            reason=f"{strategy}: replace terminal work {', '.join(obsolete)}",
            evidence_refs=tuple(inputs["evidence_refs"]),
            add=tuple(added_items),
            cancel=obsolete,
        )


def _git_settings_from_policy(policy: Mapping[str, Any]) -> dict[str, str]:
    if not policy.get("repository"):
        return {}
    return {
        key: str(policy[key])
        for key in ("repository", "base_revision", "target_branch", "worktree_root")
        if policy.get(key)
    }


def _recovery_budget(
    total_seconds: int,
    total_tokens: int | None,
    total_cost: float | None,
    share: float,
) -> WorkBudget:
    return WorkBudget(
        max_seconds=max(1, int(total_seconds * share)),
        max_tokens=max(1, int(total_tokens * share)) if total_tokens is not None else None,
        max_cost_usd=(
            max(0.000001, round(total_cost * share, 6))
            if total_cost is not None
            else None
        ),
    )
