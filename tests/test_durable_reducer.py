import unittest

from orchestrator.domain import (
    CompletionPolicy,
    DomainError,
    PlanDocumentV1,
    RunStatus,
    WorkflowEvent,
    WorkItem,
    WorkItemKind,
)
from orchestrator.durable_reducer import apply_event, empty_aggregate, replay_events


def _event(run_id, kind, payload):
    return WorkflowEvent.create(run_id, kind, payload)


def _plan():
    return PlanDocumentV1(
        revision=1,
        goal="verified",
        success_criteria=("pass",),
        assumptions=(),
        work_items=(
            WorkItem("build", "Build", "execute", "executor"),
            WorkItem(
                "verify",
                "Verify",
                WorkItemKind.VERIFY.value,
                "verifier",
                dependencies=("build",),
            ),
        ),
        completion_policy=CompletionPolicy(("build", "verify"), True),
    )


class DurableReducerTests(unittest.TestCase):
    def test_replay_is_deterministic_and_does_not_mutate_events(self):
        events = [
            _event("r1", "run_created", {"project_id": "p1", "goal": "verified"}),
            _event("r1", "plan_committed", {"plan": _plan().to_dict()}),
            _event("r1", "run_status_changed", {"status": "running"}),
            _event("r1", "work_ready", {"work_item_id": "build"}),
        ]
        first = replay_events("r1", events)
        second = replay_events("r1", events)
        self.assertEqual(first, second)
        self.assertEqual(first["version"], 4)
        self.assertIsNone(events[0].version)

    def test_completion_rejects_missing_current_verification(self):
        state = empty_aggregate("r1")
        state = apply_event(
            state,
            _event("r1", "run_created", {"project_id": "p1", "goal": "x"}),
        )
        state = apply_event(state, _event("r1", "plan_committed", {"plan": _plan().to_dict()}))
        state = apply_event(state, _event("r1", "run_status_changed", {"status": "running"}))
        with self.assertRaisesRegex(DomainError, "verified plan"):
            apply_event(
                state,
                _event("r1", "run_status_changed", {"status": RunStatus.COMPLETED.value}),
            )

    def test_full_verified_stream_can_complete(self):
        events = [
            _event("r1", "run_created", {"project_id": "p1", "goal": "x"}),
            _event("r1", "plan_committed", {"plan": _plan().to_dict()}),
            _event("r1", "run_status_changed", {"status": "running"}),
        ]
        for item_id in ("build", "verify"):
            events.extend(
                [
                    _event("r1", "work_ready", {"work_item_id": item_id}),
                    _event(
                        "r1",
                        "work_leased",
                        {"work_item_id": item_id, "attempt_id": "a-" + item_id},
                    ),
                    _event("r1", "work_started", {"work_item_id": item_id}),
                    _event("r1", "work_succeeded", {"work_item_id": item_id}),
                ]
            )
        events.append(_event("r1", "run_status_changed", {"status": "completed"}))
        state = replay_events("r1", events)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["verified_revision"], 1)

    def test_terminal_run_cannot_be_resumed(self):
        state = replay_events(
            "r1",
            [
                _event("r1", "run_created", {"project_id": "p1", "goal": "x"}),
                _event("r1", "run_status_changed", {"status": "failed"}),
            ],
        )
        with self.assertRaisesRegex(DomainError, "terminal run"):
            apply_event(state, _event("r1", "run_status_changed", {"status": "running"}))

    def test_cancelling_leased_work_clears_current_attempt(self):
        events = [
            _event("r1", "run_created", {"project_id": "p1", "goal": "x"}),
            _event("r1", "plan_committed", {"plan": _plan().to_dict()}),
            _event("r1", "run_status_changed", {"status": "running"}),
            _event("r1", "work_ready", {"work_item_id": "build"}),
            _event(
                "r1",
                "work_leased",
                {"work_item_id": "build", "attempt_id": "attempt-1"},
            ),
            _event("r1", "work_started", {"work_item_id": "build"}),
            _event("r1", "work_cancelled", {"work_item_id": "build"}),
        ]
        state = replay_events("r1", events)
        self.assertEqual(state["work_item_statuses"]["build"], "cancelled")
        self.assertEqual(state["current_attempts"], {})


if __name__ == "__main__":
    unittest.main()
