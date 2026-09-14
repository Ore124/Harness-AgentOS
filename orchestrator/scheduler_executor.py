"""Side-effect executor for one state-driven scheduler step."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import config
from orchestrator.scheduler_reducer import (
    StepEffect,
    StepEvent,
    reduce_step,
)
from orchestrator.state import load_state, save_state


class SchedulerEffects(Protocol):
    """Scheduler operations used by the executor.

    Keeping this structural avoids importing ``Scheduler`` and creating a
    module cycle while the legacy helpers remain on that class.
    """

    state_path: Path
    hooks: Any

    def _trace(self, state, event_type, payload, *, phase=None): ...
    def _sync_acceptance_baseline(self, state): ...
    def _reconcile_durable_acceptance_transition(self, state): ...
    def _apply_hook_result(self, state, result, *, failed_action=None): ...
    def _execute_step_action(self, state, action): ...
    def _handle_step_failure(self, state, action, error): ...


class SchedulerStepExecutor:
    """Execute reducer effects without changing their ordering semantics."""

    def __init__(self, scheduler: SchedulerEffects):
        self.scheduler = scheduler

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        reduction = reduce_step(state)
        while True:
            effect = reduction.effect

            if effect == StepEffect.START_TRACE:
                self.scheduler._trace(
                    reduction.state,
                    "run_started",
                    {
                        "task_id": reduction.state.get("task_id")
                        or reduction.state.get("prompt"),
                        "model": config.MODEL,
                        "initial_workspace": reduction.state.get("workspace"),
                        "feature_flags": _trace_flags(),
                    },
                )
                reduction = reduce_step(
                    reduction.state,
                    StepEvent.TRACE_STARTED,
                )
                continue

            if effect == StepEffect.SAVE_CHECKPOINT:
                save_state(self.scheduler.state_path, reduction.state)
                reduction = reduce_step(
                    reduction.state,
                    StepEvent.CHECKPOINT_SAVED,
                )
                continue

            if effect == StepEffect.SYNC_ACCEPTANCE:
                synced = reduction.state
                if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
                    synced = self.scheduler._sync_acceptance_baseline(synced)
                    synced = self.scheduler._reconcile_durable_acceptance_transition(
                        synced
                    )
                reduction = reduce_step(
                    reduction.state,
                    StepEvent.ACCEPTANCE_SYNCED,
                    result_state=synced,
                )
                continue

            if effect == StepEffect.HANDLE_APPROVAL:
                updated = self.scheduler._apply_hook_result(
                    reduction.state,
                    self.scheduler.hooks.on_human_approval_required(reduction.state),
                )
                save_state(self.scheduler.state_path, updated)
                return load_state(self.scheduler.state_path)

            if effect == StepEffect.STALL:
                self.scheduler.hooks.on_stall(
                    reduction.state,
                    reduction.reason or "scheduler stalled",
                )
                return reduction.state

            if effect == StepEffect.BEFORE_STEP:
                updated = self.scheduler._apply_hook_result(
                    reduction.state,
                    self.scheduler.hooks.before_step(reduction.state),
                )
                reduction = reduce_step(
                    reduction.state,
                    StepEvent.BEFORE_STEP_APPLIED,
                    result_state=updated,
                )
                continue

            if effect == StepEffect.EXECUTE_ACTION:
                try:
                    updated = self.scheduler._execute_step_action(
                        reduction.state,
                        reduction.action or "",
                    )
                except Exception as exc:
                    reduction = reduce_step(
                        reduction.state,
                        StepEvent.ACTION_FAILED,
                        action=reduction.action,
                        error=exc,
                    )
                else:
                    reduction = reduce_step(
                        reduction.state,
                        StepEvent.ACTION_SUCCEEDED,
                        result_state=updated,
                        action=reduction.action,
                    )
                continue

            if effect == StepEffect.HANDLE_FAILURE:
                if reduction.error is None:
                    raise RuntimeError("failure effect is missing its exception")
                return self.scheduler._handle_step_failure(
                    reduction.state,
                    reduction.action or "",
                    reduction.error,
                )

            if effect == StepEffect.SAVE_AND_RETURN:
                save_state(self.scheduler.state_path, reduction.state)
                return load_state(self.scheduler.state_path)

            raise ValueError(f"Unknown scheduler effect: {effect}")


def _trace_flags() -> dict[str, Any]:
    """Read trace flags lazily so patched config remains observable."""
    return {
        name: bool(getattr(config, name))
        for name in dir(config)
        if name.startswith("HARNESS_") and isinstance(getattr(config, name), bool)
    }
