import unittest
from copy import deepcopy

from orchestrator.scheduler_reducer import (
    StepEffect,
    StepEvent,
    reduce_step,
)


def _state(**overrides):
    state = {
        "canonical_trace_started": True,
        "active": True,
        "status": "running",
        "next_action": "build",
        "requires_confirmation": False,
        "requires_human_approval": False,
    }
    state.update(overrides)
    return state


class SchedulerReducerTests(unittest.TestCase):
    def test_start_requests_trace_without_mutating_input(self):
        state = _state(canonical_trace_started=False)
        original = deepcopy(state)

        reduction = reduce_step(state)

        self.assertEqual(reduction.effect, StepEffect.START_TRACE)
        self.assertEqual(state, original)

    def test_trace_result_marks_copy_and_requests_checkpoint(self):
        state = _state(canonical_trace_started=False)

        reduction = reduce_step(state, StepEvent.TRACE_STARTED)

        self.assertEqual(reduction.effect, StepEffect.SAVE_CHECKPOINT)
        self.assertTrue(reduction.state["canonical_trace_started"])
        self.assertFalse(state["canonical_trace_started"])

    def test_ready_state_requests_before_step(self):
        state = _state()

        reduction = reduce_step(state, StepEvent.ACCEPTANCE_SYNCED)

        self.assertEqual(reduction.effect, StepEffect.BEFORE_STEP)

    def test_unapproved_action_takes_precedence_over_inactive_state(self):
        state = _state(
            active=False,
            requires_human_approval=True,
            human_approval={"approved": False},
        )

        reduction = reduce_step(state, StepEvent.ACCEPTANCE_SYNCED)

        self.assertEqual(reduction.effect, StepEffect.HANDLE_APPROVAL)

    def test_stall_reasons_preserve_guard_order(self):
        cases = [
            (_state(active=False), "active flag is false"),
            (
                _state(requires_confirmation=True),
                "profile confirmation required",
            ),
            (_state(next_action=None), "no next action"),
        ]

        for state, reason in cases:
            with self.subTest(reason=reason):
                reduction = reduce_step(state, StepEvent.ACCEPTANCE_SYNCED)
                self.assertEqual(reduction.effect, StepEffect.STALL)
                self.assertEqual(reduction.reason, reason)

    def test_stopping_before_step_requests_save(self):
        state = _state(status="paused")

        reduction = reduce_step(state, StepEvent.BEFORE_STEP_APPLIED)

        self.assertEqual(reduction.effect, StepEffect.SAVE_AND_RETURN)

    def test_running_before_step_dispatches_current_action(self):
        state = _state(next_action="evaluate")

        reduction = reduce_step(state, StepEvent.BEFORE_STEP_APPLIED)

        self.assertEqual(reduction.effect, StepEffect.EXECUTE_ACTION)
        self.assertEqual(reduction.action, "evaluate")

    def test_action_outcomes_select_persistence_or_recovery(self):
        state = _state()
        error = RuntimeError("boom")

        succeeded = reduce_step(
            state,
            StepEvent.ACTION_SUCCEEDED,
            result_state={**state, "next_action": "evaluate"},
            action="build",
        )
        failed = reduce_step(
            state,
            StepEvent.ACTION_FAILED,
            action="build",
            error=error,
        )

        self.assertEqual(succeeded.effect, StepEffect.SAVE_AND_RETURN)
        self.assertEqual(succeeded.state["next_action"], "evaluate")
        self.assertEqual(failed.effect, StepEffect.HANDLE_FAILURE)
        self.assertIs(failed.error, error)


if __name__ == "__main__":
    unittest.main()
