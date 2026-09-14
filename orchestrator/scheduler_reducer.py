"""Pure control-flow reducer for one scheduler step.

The reducer never performs I/O and never calls scheduler collaborators.  It
only decides which effect the executor must perform next.  Effect results are
fed back as events until the step reaches a terminal reduction.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any


class StepEffect(str, Enum):
    START_TRACE = "start_trace"
    SAVE_CHECKPOINT = "save_checkpoint"
    SYNC_ACCEPTANCE = "sync_acceptance"
    HANDLE_APPROVAL = "handle_approval"
    STALL = "stall"
    BEFORE_STEP = "before_step"
    EXECUTE_ACTION = "execute_action"
    HANDLE_FAILURE = "handle_failure"
    SAVE_AND_RETURN = "save_and_return"


class StepEvent(str, Enum):
    START = "start"
    TRACE_STARTED = "trace_started"
    CHECKPOINT_SAVED = "checkpoint_saved"
    ACCEPTANCE_SYNCED = "acceptance_synced"
    BEFORE_STEP_APPLIED = "before_step_applied"
    ACTION_SUCCEEDED = "action_succeeded"
    ACTION_FAILED = "action_failed"


@dataclass(frozen=True)
class Reduction:
    """The next effect requested by the pure reducer."""

    state: dict[str, Any]
    effect: StepEffect
    action: str | None = None
    reason: str | None = None
    error: BaseException | None = None


def reduce_step(
    state: dict[str, Any],
    event: StepEvent = StepEvent.START,
    *,
    result_state: dict[str, Any] | None = None,
    action: str | None = None,
    error: BaseException | None = None,
) -> Reduction:
    """Reduce scheduler state and an execution event into the next effect.

    A defensive copy makes input immutability part of the reducer contract.
    ``result_state`` is the state returned by a completed side effect.
    """
    current = deepcopy(result_state if result_state is not None else state)

    if event == StepEvent.START:
        if not current.get("canonical_trace_started"):
            return Reduction(current, StepEffect.START_TRACE)
        return Reduction(current, StepEffect.SYNC_ACCEPTANCE)

    if event == StepEvent.TRACE_STARTED:
        current["canonical_trace_started"] = True
        return Reduction(current, StepEffect.SAVE_CHECKPOINT)

    if event == StepEvent.CHECKPOINT_SAVED:
        return Reduction(current, StepEffect.SYNC_ACCEPTANCE)

    if event == StepEvent.ACCEPTANCE_SYNCED:
        return _reduce_ready_state(current)

    if event == StepEvent.BEFORE_STEP_APPLIED:
        if current.get("status") in {
            "paused",
            "error",
            "waiting_confirmation",
            "waiting_approval",
        }:
            return Reduction(current, StepEffect.SAVE_AND_RETURN)
        # Deliberately use subscription: before the extraction, a pre-step
        # hook that removed this key raised KeyError before action recovery.
        next_action = current["next_action"]
        return Reduction(current, StepEffect.EXECUTE_ACTION, action=next_action)

    if event == StepEvent.ACTION_SUCCEEDED:
        return Reduction(current, StepEffect.SAVE_AND_RETURN, action=action)

    if event == StepEvent.ACTION_FAILED:
        return Reduction(
            current,
            StepEffect.HANDLE_FAILURE,
            action=action,
            error=error,
        )

    raise ValueError(f"Unknown scheduler reducer event: {event}")


def _reduce_ready_state(state: dict[str, Any]) -> Reduction:
    if state.get("requires_human_approval") and not state.get(
        "human_approval", {}
    ).get("approved"):
        return Reduction(state, StepEffect.HANDLE_APPROVAL)
    if not state.get("active"):
        return Reduction(state, StepEffect.STALL, reason="active flag is false")
    if state.get("requires_confirmation"):
        return Reduction(
            state,
            StepEffect.STALL,
            reason="profile confirmation required",
        )
    if not state.get("next_action"):
        return Reduction(state, StepEffect.STALL, reason="no next action")
    return Reduction(state, StepEffect.BEFORE_STEP)
