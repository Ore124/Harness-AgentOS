"""Worker runtime and adapters for durable work items."""
from __future__ import annotations

import logging
import hashlib
import json
import mimetypes
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

from orchestrator.controller import DurableController, EvidenceRecoveryPlanner
from orchestrator.domain import LeasedWorkItem, StaleLeaseError
from orchestrator.observability import correlated_span, work_counter
from orchestrator.run_context import RunContext
from orchestrator.workflow_repository import WorkflowRepository


log = logging.getLogger("harness")


@dataclass(frozen=True)
class AgentExecutionOutcome:
    succeeded: bool
    output: str = ""
    retryable: bool = True
    failure_kind: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()

    def to_repository_outcome(self) -> dict[str, Any]:
        return {
            "status": "succeeded" if self.succeeded else "failed",
            "succeeded": self.succeeded,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
            "output": self.output[-20000:],
            "artifacts": list(self.artifacts),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentExecutionOutcome":
        return cls(
            succeeded=bool(payload.get("succeeded")),
            output=str(payload.get("output", "")),
            retryable=bool(payload.get("retryable", True)),
            failure_kind=payload.get("failure_kind"),
            checkpoint=dict(payload.get("checkpoint") or {}),
            evidence=tuple(dict(item) for item in payload.get("evidence") or ()),
            artifacts=tuple(dict(item) for item in payload.get("artifacts") or ()),
        )


class AgentAdapter(Protocol):
    def run(
        self,
        work_item: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None,
        run_context: RunContext,
    ) -> AgentExecutionOutcome: ...


class ProfileAgentAdapter:
    """Run existing Profile agents behind the durable worker contract."""

    def run(
        self,
        work_item: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None,
        run_context: RunContext,
    ) -> AgentExecutionOutcome:
        from harness import Harness
        from profiles import get_profile

        inputs = dict(work_item.get("inputs") or {})
        profile = get_profile(str(inputs.get("profile", "app-builder")))
        harness = Harness(profile)
        role = str(work_item.get("agent_role", "executor"))
        goal = str(inputs.get("goal") or work_item.get("title") or "")
        checkpoint_note = ""
        if checkpoint:
            checkpoint_note = (
                "\nPrevious-attempt checkpoint metadata follows. Values such as output_size "
                "describe the assistant response, not the completeness of files already in "
                "the workspace. Inspect existing expected outputs first and reuse them when "
                f"valid:\n{dict(checkpoint)}\n"
            )

        if role == "planner":
            agent = harness.planner
            task = f"Create a concrete plan for:\n\n{goal}\n\nSave it to spec.md.{checkpoint_note}"
        elif role == "verifier":
            agent = harness.evaluator
            task = (
                "Independently verify the current immutable workspace revision against "
                f"the original goal and acceptance checks. Write feedback.md.\nGoal: {goal}"
                f"{checkpoint_note}"
            )
        elif role == "diagnostician":
            agent = harness.evaluator or harness.builder
            failed_items = ", ".join(
                str(item_id) for item_id in inputs.get("failure_work_items") or ()
            ) or "unknown"
            failure_evidence = json.dumps(
                inputs.get("failure_evidence") or (),
                ensure_ascii=False,
                indent=2,
            )
            task = (
                f"Diagnose the execution failures for these failed work items: {failed_items}. "
                "Focus on why those nodes failed. Do not evaluate downstream deliverables "
                "that were never scheduled, and do not weaken acceptance criteria. Write a "
                "concise root-cause report to feedback.md and finish immediately after the "
                f"file is written.\nFailure evidence:\n{failure_evidence}\n"
                f"{checkpoint_note}"
            )
        else:
            agent = harness.builder
            task = (
                profile.format_build_task(goal, 1, "", [])
                + "\n\nExecution constraint: work incrementally. Start with a minimal "
                "index.html skeleton under 60 lines, then add CSS, JavaScript, and data "
                "using separate short tool calls. Never put the complete application in "
                "one write_file call."
                + checkpoint_note
            )

        if agent is None:
            return AgentExecutionOutcome(
                False,
                failure_kind="agent_role_disabled",
                retryable=False,
            )
        max_seconds = (work_item.get("budget") or {}).get("max_seconds")
        if max_seconds is not None:
            agent.time_budget = float(max_seconds)
        result = agent.run(
            task,
            run_id=run_context.run_id,
            phase=str(work_item.get("kind") or role),
            run_context=run_context,
        )
        succeeded = bool(getattr(result, "succeeded", isinstance(result, str)))
        output = str(getattr(result, "text", result or ""))
        exit_reason = str(getattr(result, "exit_reason", "no_tool_calls"))

        if role == "verifier" and succeeded:
            feedback = run_context.workspace / "feedback.md"
            text = feedback.read_text(encoding="utf-8", errors="replace") if feedback.exists() else ""
            score = profile.extract_score(text)
            succeeded = score >= profile.pass_threshold()
            if not succeeded:
                exit_reason = "acceptance_failed"
        return AgentExecutionOutcome(
            succeeded=succeeded,
            output=output,
            retryable=exit_reason not in {"agent_role_disabled"},
            failure_kind=None if succeeded else exit_reason,
            checkpoint={
                "output_sha256": hashlib.sha256(
                    output.encode("utf-8", errors="replace")
                ).hexdigest(),
                "output_size": len(output),
                "exit_reason": exit_reason,
            },
            artifacts=_declared_file_artifacts(
                work_item.get("expected_outputs") or (),
                run_context.workspace,
            ),
        )


class DockerProfileAgentAdapter:
    """Execute one Profile role in a constrained, short-lived container."""

    def __init__(self, executor, policy):
        self.executor = executor
        self.policy = policy

    def run(
        self,
        work_item: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None,
        run_context: RunContext,
    ) -> AgentExecutionOutcome:
        from orchestrator.sandbox import EffectRequest

        outcome_name = f"activity-outcome-{work_item['id']}.json"
        outcome_path = run_context.workspace / ".harness" / outcome_name
        journal_name = f"activity-checkpoints-{run_context.attempt_id or work_item['id']}.jsonl"
        journal_path = run_context.workspace / ".harness" / journal_name
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (outcome_path, journal_path):
            if path.exists():
                path.unlink()
        timeout_seconds = int((work_item.get("budget") or {}).get("max_seconds", 900))
        environment = {
            "HARNESS_WORK_ITEM_JSON": json.dumps(dict(work_item), ensure_ascii=False),
            "HARNESS_CHECKPOINT_JSON": json.dumps(dict(checkpoint or {}), ensure_ascii=False),
            "HARNESS_RUN_ID": run_context.run_id,
            "HARNESS_ACTIVITY_OUTCOME": f"/workspace/.harness/{outcome_name}",
            "HARNESS_CHECKPOINT_JOURNAL": f"/workspace/.harness/{journal_name}",
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "HARNESS_MODEL": os.environ.get("HARNESS_MODEL", "gpt-4o"),
            "HARNESS_FLAT_WORKSPACE": "1",
            "HARNESS_WORKSPACE": "/workspace",
            "HARNESS_RUNTIME": "legacy",
        }
        try:
            result = self.executor.execute(
                EffectRequest(
                    effect_id=(
                        f"{run_context.run_id}/{work_item['id']}/"
                        f"{run_context.attempt_id or 'unknown'}/1"
                    ),
                    command=("python", "/opt/harness/durable_activity.py"),
                    workspace=run_context.workspace,
                    environment=environment,
                    secret_refs={"OPENAI_API_KEY": "OPENAI_API_KEY"},
                    network_hosts=tuple(self.policy.allow_network_hosts),
                    timeout_seconds=timeout_seconds,
                    cancel_check=run_context.cancel_check,
                    checkpoint_path=journal_path,
                    checkpoint_callback=run_context.checkpoint_callback,
                ),
                self.policy,
            )
        finally:
            if journal_path.exists():
                journal_path.unlink()
        if not outcome_path.exists():
            return AgentExecutionOutcome(
                False,
                output=(result.stdout + "\n" + result.stderr)[-20000:],
                failure_kind="sandbox_activity_failed",
                retryable=True,
            )
        try:
            payload = json.loads(outcome_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return AgentExecutionOutcome(
                False,
                output=(result.stdout + "\n" + result.stderr)[-20000:],
                failure_kind="invalid_activity_outcome",
                retryable=True,
                evidence=({"kind": "sandbox_protocol_error", "message": str(exc)},),
            )
        return AgentExecutionOutcome(
            succeeded=bool(payload.get("succeeded")),
            output=str(payload.get("output", "")),
            retryable=bool(payload.get("retryable", True)),
            failure_kind=payload.get("failure_kind"),
            checkpoint=dict(payload.get("checkpoint") or {}),
            evidence=tuple(payload.get("evidence") or ()),
            artifacts=tuple(payload.get("artifacts") or ()),
        )


class GitWorktreeAgentAdapter:
    """Isolate write work, verify a fixed commit, then serialize integration."""

    _VERIFIER_WRITABLE = {"feedback.md", "progress.md"}

    def __init__(self, inner: AgentAdapter, manager):
        self.inner = inner
        self.manager = manager

    def run(
        self,
        work_item: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None,
        run_context: RunContext,
    ) -> AgentExecutionOutcome:
        kind = str(work_item.get("kind", "execute"))
        inputs = dict(work_item.get("inputs") or {})
        base_revision = str(inputs.get("base_revision", "HEAD"))
        target_branch = str(inputs.get("target_branch", "main"))
        owner_item_id = str(
            inputs.get("workspace_item_id")
            or (work_item.get("dependencies") or [work_item.get("id")])[-1]
        )
        coordinates = {"run_id": run_context.run_id, "id": owner_item_id}

        if kind == "integrate":
            workspace = self.manager.resolve(coordinates, base_revision)
            if not workspace.path.exists():
                return AgentExecutionOutcome(
                    False,
                    output="integration workspace is missing",
                    retryable=False,
                    failure_kind="integration_workspace_missing",
                )
            result = self.manager.integrate(workspace, target_branch)
            if not result.succeeded:
                return AgentExecutionOutcome(
                    False,
                    output=result.conflict_output,
                    retryable=False,
                    failure_kind="merge_conflict",
                    evidence=(
                        {
                            "kind": "integration_conflict",
                            "branch": workspace.branch,
                            "details": result.conflict_output,
                        },
                    ),
                )
            self.manager.remove(workspace)
            return AgentExecutionOutcome(
                True,
                output=f"integrated {workspace.branch} at {result.commit}",
                checkpoint={"commit": result.commit, "branch": target_branch},
                evidence=(
                    {
                        "kind": "integration_result",
                        "branch": workspace.branch,
                        "commit": result.commit,
                    },
                ),
            )

        if kind == "execute":
            coordinates["id"] = str(work_item["id"])
            workspace = self.manager.prepare(coordinates, base_revision)
            isolated_context = replace(
                run_context,
                workspace=workspace.path,
                trace_dir=workspace.path / ".harness" / "traces",
            )
            outcome = self.inner.run(work_item, checkpoint, isolated_context)
            if not outcome.succeeded:
                return outcome
            commit = self.manager.commit(
                workspace,
                f"Harness {run_context.run_id}/{work_item['id']}",
            )
            return replace(
                outcome,
                checkpoint={**outcome.checkpoint, "commit": commit, "branch": workspace.branch},
                artifacts=_absolute_artifact_paths(outcome.artifacts, workspace.path),
                evidence=outcome.evidence
                + (
                    {
                        "kind": "workspace_revision",
                        "work_item_id": work_item["id"],
                        "commit": commit,
                        "branch": workspace.branch,
                    },
                ),
            )

        if kind == "verify":
            workspace = self.manager.resolve(coordinates, base_revision)
            if not workspace.path.exists():
                return AgentExecutionOutcome(
                    False,
                    output="verification workspace is missing",
                    retryable=False,
                    failure_kind="verification_workspace_missing",
                )
            commit_before = self.manager.head(workspace)
            changes_before = set(self.manager.changed_files(workspace))
            isolated_context = replace(
                run_context,
                workspace=workspace.path,
                trace_dir=workspace.path / ".harness" / "traces",
            )
            outcome = self.inner.run(work_item, checkpoint, isolated_context)
            commit_after = self.manager.head(workspace)
            changes_after = set(self.manager.changed_files(workspace))
            new_changes = {
                path
                for path in changes_after - changes_before
                if path not in self._VERIFIER_WRITABLE and not path.startswith(".harness/")
            }
            if commit_before != commit_after or new_changes:
                return AgentExecutionOutcome(
                    False,
                    output="verifier modified the immutable source snapshot",
                    retryable=False,
                    failure_kind="verifier_mutated_workspace",
                    evidence=(
                        {
                            "kind": "verification_isolation_violation",
                            "commit_before": commit_before,
                            "commit_after": commit_after,
                            "changed_files": sorted(new_changes),
                        },
                    ),
                )
            return replace(
                outcome,
                artifacts=_absolute_artifact_paths(outcome.artifacts, workspace.path),
                evidence=outcome.evidence
                + ({"kind": "verification_revision", "commit": commit_before},),
            )

        return self.inner.run(work_item, checkpoint, run_context)


class DurableWorker:
    HEARTBEAT_SECONDS = 15

    def __init__(
        self,
        worker_id: str,
        repository: WorkflowRepository,
        adapter: AgentAdapter,
        *,
        capabilities: set[str] | None = None,
        controller: DurableController | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        artifact_store=None,
        target_run_id: str | None = None,
    ):
        self.worker_id = worker_id
        self.repository = repository
        self.adapter = adapter
        self.capabilities = capabilities or set()
        self.controller = controller or DurableController(
            repository, planner=EvidenceRecoveryPlanner()
        )
        self.heartbeat_seconds = heartbeat_seconds
        self.artifact_store = artifact_store
        self.target_run_id = target_run_id

    def run_once(self) -> bool:
        self.repository.release_expired_leases()
        leases = self.repository.claim_work(
            self.worker_id,
            self.capabilities,
            limit=1,
            run_id=self.target_run_id,
        )
        if not leases:
            return False
        self._execute(leases[0])
        return True

    def run_forever(
        self,
        *,
        poll_interval: float = 1.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            if not self.run_once():
                stop_event.wait(poll_interval)

    def _execute(self, lease: LeasedWorkItem) -> None:
        self.repository.start_attempt(lease.attempt_id, lease.fencing_token)
        stop_heartbeat = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(lease, stop_heartbeat, lease_lost),
            daemon=True,
            name=f"lease-{lease.attempt_id[:8]}",
        )
        heartbeat.start()
        try:
            snapshot = self.repository.get_snapshot(lease.run_id)
            policy = snapshot.get("policy") or {}
            workspace = Path(
                policy.get("workspace")
                or Path.cwd() / "workspace" / lease.run_id
            ).resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            checkpoint_state = {"sequence": 0}

            def commit_activity_checkpoint(payload: Mapping[str, Any]) -> None:
                checkpoint_state["sequence"] += 1
                self.repository.commit_checkpoint(
                    lease.attempt_id,
                    lease.fencing_token,
                    {**dict(payload), "sequence": checkpoint_state["sequence"]},
                )

            context = RunContext(
                run_id=lease.run_id,
                workspace=workspace,
                trace_dir=workspace / ".harness" / "traces",
                allow_terminal=bool(policy.get("allow_terminal", False)),
                task_id=lease.work_item_id,
                attempt_id=lease.attempt_id,
                cancel_check=lambda: lease_lost.is_set()
                or self.repository.get_snapshot(lease.run_id).get("status") == "cancelled",
                checkpoint_callback=commit_activity_checkpoint,
            )
            enriched_item = dict(lease.item)
            enriched_inputs = dict(enriched_item.get("inputs") or {})
            evidence_work_items = {lease.work_item_id}
            evidence_work_items.update(
                str(item_id)
                for item_id in enriched_inputs.get("failure_work_items") or ()
            )
            enriched_inputs["failure_evidence"] = [
                item
                for item in snapshot.get("evidence", [])
                if item.get("work_item_id") in evidence_work_items
            ][-10:]
            enriched_item["inputs"] = enriched_inputs
            effect_id = f"{lease.run_id}/{lease.work_item_id}/{lease.attempt_id}/1"
            reservation = self.repository.reserve_effect(
                effect_id,
                run_id=lease.run_id,
                work_item_id=lease.work_item_id,
                attempt_id=lease.attempt_id,
                sequence=1,
            )
            if reservation["status"] == "completed":
                outcome = AgentExecutionOutcome.from_dict(reservation["result"] or {})
            else:
                with correlated_span(
                    "work_item.execute",
                    project_id=str(snapshot.get("project_id")),
                    run_id=lease.run_id,
                    work_item_id=lease.work_item_id,
                    attempt_id=lease.attempt_id,
                ):
                    outcome = self.adapter.run(enriched_item, lease.checkpoint, context)
                self.repository.complete_effect(effect_id, asdict(outcome))
            work_counter.add(
                1,
                {
                    "kind": str(enriched_item.get("kind", "execute")),
                    "outcome": "succeeded" if outcome.succeeded else "failed",
                },
            )
            if outcome.checkpoint:
                commit_activity_checkpoint(outcome.checkpoint)
            for item in outcome.evidence:
                self.controller.record_evidence(
                    lease.run_id,
                    {**item, "work_item_id": lease.work_item_id},
                    actor=f"worker:{self.worker_id}",
                )
            for artifact in outcome.artifacts:
                materialized_artifact = self._materialize_artifact(
                    artifact,
                    workspace=workspace,
                    policy=policy,
                )
                self.repository.register_artifact(
                    lease.run_id,
                    materialized_artifact,
                    work_item_id=lease.work_item_id,
                    actor=f"worker:{self.worker_id}",
                )
            self.repository.complete_attempt(
                lease.attempt_id,
                lease.fencing_token,
                outcome.to_repository_outcome(),
            )
        except KeyboardInterrupt:
            try:
                cancelled = (
                    self.repository.get_snapshot(lease.run_id).get("status")
                    == "cancelled"
                )
                self.repository.complete_attempt(
                    lease.attempt_id,
                    lease.fencing_token,
                    {
                        "status": "cancelled" if cancelled else "failed",
                        "succeeded": False,
                        "retryable": not cancelled,
                        "failure_kind": "cancelled" if cancelled else "interrupted",
                    },
                )
            except StaleLeaseError:
                log.warning(
                    "Attempt %s lost its lease while handling interruption",
                    lease.attempt_id,
                )
            raise
        except Exception as exc:
            log.exception(
                "Durable worker %s failed attempt %s",
                self.worker_id,
                lease.attempt_id,
            )
            try:
                self.repository.complete_attempt(
                    lease.attempt_id,
                    lease.fencing_token,
                    {
                        "status": "failed",
                        "retryable": True,
                        "failure_kind": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            except StaleLeaseError:
                log.warning("Attempt %s lost its lease; late failure was discarded", lease.attempt_id)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(1.0, self.heartbeat_seconds))
        self.controller.advance(lease.run_id)

    def _materialize_artifact(
        self,
        artifact: Mapping[str, Any],
        *,
        workspace: Path,
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(artifact)
        if payload.get("id") and payload.get("uri") and payload.get("sha256"):
            return payload
        if self.artifact_store is None or not payload.get("path"):
            raise ValueError("path artifacts require a configured ArtifactStore")
        source = Path(str(payload["path"]))
        if not source.is_absolute():
            source = workspace / source
        source = source.resolve()
        allowed_roots = [workspace.resolve()]
        if policy.get("worktree_root"):
            allowed_roots.append(Path(str(policy["worktree_root"])).resolve())
        if not any(source.is_relative_to(root) for root in allowed_roots):
            raise PermissionError("artifact source is outside approved workspace roots")
        if not source.is_file():
            raise FileNotFoundError(source)
        with source.open("rb") as stream:
            record = self.artifact_store.put(
                stream,
                {
                    "name": payload.get("name") or source.name,
                    "media_type": payload.get("media_type")
                    or mimetypes.guess_type(source.name)[0]
                    or "application/octet-stream",
                    "sensitivity": payload.get("sensitivity", "internal"),
                },
            )
        return record.to_dict()

    def _heartbeat(
        self,
        lease: LeasedWorkItem,
        stop_event: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        while not stop_event.wait(self.heartbeat_seconds):
            try:
                self.repository.renew_lease(
                    lease.attempt_id, lease.fencing_token
                )
            except StaleLeaseError:
                lease_lost.set()
                log.debug("Lease is no longer active for %s", lease.attempt_id)
                return
            except Exception:
                lease_lost.set()
                log.exception("Lease heartbeat failed for %s", lease.attempt_id)
                return


def standard_profile_plan(
    goal: str,
    profile: str,
    *,
    revision: int = 1,
    max_seconds: int = 3600,
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
    execution_capability: str = "local",
    git_settings: Mapping[str, str] | None = None,
) -> "PlanDocumentV1":
    """Build the compatibility DAG used while LLM plan JSON is introduced."""
    from orchestrator.domain import CompletionPolicy, PlanDocumentV1, WorkBudget, WorkItem

    common_inputs = {"goal": goal, "profile": profile, **dict(git_settings or {})}
    plan_seconds = max(1, int(max_seconds * 0.1))
    verify_seconds = max(1, int(max_seconds * 0.2))
    integrate_seconds = max(1, int(max_seconds * 0.1)) if git_settings else 0
    build_seconds = max(1, max_seconds - plan_seconds - verify_seconds - integrate_seconds)
    build_share = 0.6 if git_settings else 0.7
    plan = WorkItem(
        id="plan",
        title="Create an executable specification",
        kind="plan",
        agent_role="planner",
        inputs=common_inputs,
        expected_outputs=("spec.md",),
        required_capabilities=(execution_capability,),
        budget=_work_budget(plan_seconds, 0.1, max_tokens, max_cost_usd),
    )
    build = WorkItem(
        id="build",
        title="Execute the current plan",
        kind="execute",
        agent_role="executor",
        dependencies=("plan",),
        inputs=common_inputs,
        expected_outputs=("workspace changes",),
        acceptance_checks=("profile acceptance commands",),
        required_capabilities=(execution_capability,),
        budget=_work_budget(build_seconds, build_share, max_tokens, max_cost_usd),
    )
    verify = WorkItem(
        id="verify",
        title="Independently verify the result",
        kind="verify",
        agent_role="verifier",
        dependencies=("build",),
        inputs=common_inputs,
        expected_outputs=("feedback.md",),
        acceptance_checks=("current revision passes profile threshold",),
        required_capabilities=(execution_capability,),
        budget=_work_budget(verify_seconds, 0.2, max_tokens, max_cost_usd),
    )
    items = [plan, build, verify]
    if git_settings:
        integration_inputs = {
            **common_inputs,
            "workspace_item_id": "build",
        }
        items.append(
            WorkItem(
                id="integrate",
                title="Integrate the independently verified revision",
                kind="integrate",
                agent_role="integrator",
                dependencies=("verify",),
                inputs=integration_inputs,
                expected_outputs=("integrated target branch commit",),
                required_capabilities=(execution_capability,),
                budget=_work_budget(integrate_seconds, 0.1, max_tokens, max_cost_usd),
            )
        )
    required_ids = tuple(item.id for item in items)
    return PlanDocumentV1(
        revision=revision,
        goal=goal,
        success_criteria=("all required work succeeds", "current revision is independently verified"),
        assumptions=("Legacy Profile agents are used through AgentAdapter",),
        work_items=tuple(items),
        completion_policy=CompletionPolicy(required_ids, True),
    )


def _work_budget(
    max_seconds: int,
    share: float,
    max_tokens: int | None,
    max_cost_usd: float | None,
):
    from orchestrator.domain import WorkBudget

    return WorkBudget(
        max_seconds=max_seconds,
        max_tokens=max(1, int(max_tokens * share)) if max_tokens is not None else None,
        max_cost_usd=(
            max(0.000001, round(max_cost_usd * share, 6))
            if max_cost_usd is not None
            else None
        ),
    )


def _declared_file_artifacts(
    expected_outputs,
    workspace: Path,
) -> tuple[dict[str, Any], ...]:
    artifacts = []
    for expected in expected_outputs:
        candidate = Path(str(expected))
        if candidate.is_absolute() or "*" in str(candidate):
            continue
        resolved = (workspace / candidate).resolve()
        if resolved.is_relative_to(workspace.resolve()) and resolved.is_file():
            artifacts.append(
                {
                    "path": str(candidate).replace("\\", "/"),
                    "name": candidate.name,
                    "media_type": mimetypes.guess_type(candidate.name)[0]
                    or "application/octet-stream",
                    "sensitivity": "internal",
                }
            )
    return tuple(artifacts)


def _absolute_artifact_paths(
    artifacts: tuple[dict[str, Any], ...],
    workspace: Path,
) -> tuple[dict[str, Any], ...]:
    normalized = []
    for artifact in artifacts:
        payload = dict(artifact)
        if payload.get("path") and not Path(str(payload["path"])).is_absolute():
            payload["path"] = str((workspace / str(payload["path"])).resolve())
        normalized.append(payload)
    return tuple(normalized)
