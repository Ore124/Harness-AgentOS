import tempfile
import threading
import unittest
from pathlib import Path

from orchestrator.domain import (
    CompletionPolicy,
    ConcurrencyError,
    PlanDocumentV1,
    StaleLeaseError,
    WorkflowEvent,
    WorkItem,
    WorkItemKind,
)
from orchestrator.workflow_repository import SqlWorkflowRepository
from orchestrator.controller import DurableController


def _plan(required_capabilities=()):
    return PlanDocumentV1(
        revision=1,
        goal="ship",
        success_criteria=("verified",),
        assumptions=(),
        work_items=(
            WorkItem(
                "build",
                "Build",
                "execute",
                "executor",
                required_capabilities=tuple(required_capabilities),
            ),
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


def _repository(root):
    return SqlWorkflowRepository("sqlite://")


def _running_run(repository, run_id="r1", capabilities=()):
    state = repository.create_run("p1", "ship", run_id=run_id)
    events = [
        WorkflowEvent.create(run_id, "plan_committed", {"plan": _plan(capabilities).to_dict()}),
        WorkflowEvent.create(run_id, "run_status_changed", {"status": "running"}),
        WorkflowEvent.create(run_id, "work_ready", {"work_item_id": "build"}),
    ]
    return repository.append(run_id, state["version"], events)


def _running_integration(repository, run_id):
    plan = PlanDocumentV1(
        revision=1,
        goal="integrate",
        success_criteria=("integrated",),
        assumptions=(),
        work_items=(WorkItem("integrate", "Integrate", "integrate", "integrator"),),
        completion_policy=CompletionPolicy(("integrate",), False),
    )
    state = repository.create_run("p1", "integrate", run_id=run_id)
    return repository.append(
        run_id,
        state["version"],
        [
            WorkflowEvent.create(run_id, "plan_committed", {"plan": plan.to_dict()}),
            WorkflowEvent.create(run_id, "run_status_changed", {"status": "running"}),
            WorkflowEvent.create(run_id, "work_ready", {"work_item_id": "integrate"}),
        ],
    )


class WorkflowRepositoryTests(unittest.TestCase):
    def test_create_run_is_idempotent_and_append_uses_optimistic_version(self):
        with tempfile.TemporaryDirectory() as root:
            repository = _repository(root)
            first = repository.create_run(
                "p1", "ship", idempotency_key="request-1", run_id="r1"
            )
            second = repository.create_run(
                "p1", "different ignored goal", idempotency_key="request-1"
            )
            self.assertEqual(first["run_id"], second["run_id"])

            event = WorkflowEvent.create(
                "r1", "run_status_changed", {"status": "running"}
            )
            updated = repository.append("r1", 1, [event])
            self.assertEqual(updated["version"], 2)
            with self.assertRaises(ConcurrencyError):
                repository.append(
                    "r1",
                    1,
                    [WorkflowEvent.create("r1", "run_status_changed", {"status": "paused"})],
                )
            replayed = repository.append("r1", 1, [event])
            self.assertEqual(replayed["version"], 2)

    def test_claim_is_capability_aware_and_two_workers_do_not_share_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            repository = _repository(root)
            _running_run(repository, capabilities=("docker",))
            self.assertEqual(repository.claim_work("local", {"local"}), [])

            barrier = threading.Barrier(3)
            claimed = []

            def claim(worker):
                barrier.wait()
                claimed.extend(repository.claim_work(worker, {"docker"}))

            threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0].work_item_id, "build")

    def test_checkpoint_and_completion_require_current_fencing_token(self):
        with tempfile.TemporaryDirectory() as root:
            repository = _repository(root)
            _running_run(repository)
            lease = repository.claim_work("w1", set())[0]
            repository.start_attempt(lease.attempt_id, lease.fencing_token)
            checkpointed = repository.commit_checkpoint(
                lease.attempt_id,
                lease.fencing_token,
                {"sequence": 1, "messages": ["safe"]},
            )
            self.assertEqual(
                checkpointed["latest_checkpoints"]["build"]["sequence"], 1
            )
            completed = repository.complete_attempt(
                lease.attempt_id,
                lease.fencing_token,
                {"status": "succeeded"},
            )
            self.assertEqual(completed["work_item_statuses"]["build"], "succeeded")
            with self.assertRaises(StaleLeaseError):
                repository.renew_lease(lease.attempt_id, lease.fencing_token)

    def test_cancelling_run_invalidates_active_attempt_lease(self):
        repository = _repository(None)
        _running_run(repository)
        lease = repository.claim_work("w1", set())[0]
        repository.start_attempt(lease.attempt_id, lease.fencing_token)
        DurableController(repository).change_status("r1", "cancelled", actor="test")

        snapshot = repository.get_snapshot("r1")
        self.assertEqual(snapshot["current_attempts"], {})
        with self.assertRaises(StaleLeaseError):
            repository.renew_lease(lease.attempt_id, lease.fencing_token)

    def test_failed_attempt_requeues_until_attempt_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as root:
            repository = _repository(root)
            _running_run(repository)
            for attempt_number in range(1, 4):
                lease = repository.claim_work(f"w{attempt_number}", set())[0]
                repository.start_attempt(lease.attempt_id, lease.fencing_token)
                state = repository.complete_attempt(
                    lease.attempt_id,
                    lease.fencing_token,
                    {"status": "failed", "retryable": True},
                )
                expected = "ready" if attempt_number < 3 else "failed"
                self.assertEqual(state["work_item_statuses"]["build"], expected)

    def test_event_and_outbox_are_committed_together(self):
        with tempfile.TemporaryDirectory() as root:
            repository = _repository(root)
            state = repository.create_run("p1", "ship", run_id="r1")
            self.assertEqual(len(repository.list_events("r1")), state["version"])
            pending = repository.list_outbox()
            self.assertEqual(len(pending), 1)
            repository.mark_outbox_published([pending[0]["id"]])
            self.assertEqual(repository.list_outbox(), [])

    def test_project_run_and_active_work_limits_are_enforced(self):
        repository = _repository(None)
        repository.set_project_limits(
            "p1", max_concurrent_runs=1, max_active_work_items=1
        )
        _running_run(repository, run_id="r1")
        with self.assertRaisesRegex(Exception, "concurrent run limit"):
            repository.create_run("p1", "another", run_id="r2")

        first = repository.claim_work("w1", set())
        self.assertEqual(len(first), 1)
        repository.set_project_limits(
            "p1", max_concurrent_runs=2, max_active_work_items=1
        )
        state = repository.create_run("p1", "second", run_id="r2")
        repository.append(
            "r2",
            state["version"],
            [
                WorkflowEvent.create("r2", "plan_committed", {"plan": _plan().to_dict()}),
                WorkflowEvent.create("r2", "run_status_changed", {"status": "running"}),
                WorkflowEvent.create("r2", "work_ready", {"work_item_id": "build"}),
            ],
        )
        self.assertEqual(repository.claim_work("w2", set()), [])

    def test_effect_ledger_is_idempotent_and_result_is_immutable(self):
        repository = _repository(None)
        _running_run(repository)
        lease = repository.claim_work("w1", set())[0]
        effect_id = f"r1/build/{lease.attempt_id}/1"
        reserved = repository.reserve_effect(
            effect_id,
            run_id="r1",
            work_item_id="build",
            attempt_id=lease.attempt_id,
            sequence=1,
        )
        self.assertEqual(reserved["status"], "reserved")
        repository.complete_effect(effect_id, {"succeeded": True})
        replayed = repository.reserve_effect(
            effect_id,
            run_id="r1",
            work_item_id="build",
            attempt_id=lease.attempt_id,
            sequence=1,
        )
        self.assertEqual(replayed["result"], {"succeeded": True})
        with self.assertRaisesRegex(Exception, "immutable"):
            repository.complete_effect(effect_id, {"succeeded": False})

    def test_integration_claims_are_serialized_per_project(self):
        repository = _repository(None)
        _running_integration(repository, "r1")
        _running_integration(repository, "r2")
        first = repository.claim_work("integrator-1", set(), limit=2)
        self.assertEqual(len(first), 1)
        self.assertEqual(repository.claim_work("integrator-2", set()), [])
        repository.start_attempt(first[0].attempt_id, first[0].fencing_token)
        repository.complete_attempt(
            first[0].attempt_id,
            first[0].fencing_token,
            {"status": "succeeded"},
        )
        second = repository.claim_work("integrator-2", set())
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0].run_id, second[0].run_id)


if __name__ == "__main__":
    unittest.main()
