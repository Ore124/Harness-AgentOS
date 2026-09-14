import tempfile
import unittest

from orchestrator.controller import DurableController
from orchestrator.domain import (
    CompletionPolicy,
    DomainError,
    PlanDocumentV1,
    PlanPatchV1,
    WorkItem,
    WorkItemKind,
)
from orchestrator.workflow_repository import SqlWorkflowRepository


def _plan(*, high_risk=False):
    return PlanDocumentV1(
        revision=1,
        goal="ship",
        success_criteria=("tests pass",),
        assumptions=(),
        work_items=(
            WorkItem(
                "build",
                "Build",
                "execute",
                "executor",
                risk_level="high" if high_risk else "low",
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


class DurableControllerTests(unittest.TestCase):
    def setUp(self):
        self.repository = SqlWorkflowRepository("sqlite://")
        self.controller = DurableController(self.repository)

    def tearDown(self):
        self.repository.close()

    def _create(self, plan=None):
        state = self.controller.create_run("p1", "ship", run_id="r1")
        self.controller.commit_plan("r1", plan or _plan(), expected_version=state["version"])
        return self.controller.reconcile("r1")

    def test_reconcile_schedules_dependencies_and_only_completes_after_verification(self):
        decision = self._create()
        self.assertEqual(decision.work_item_ids, ("build",))
        build = self.repository.claim_work("w1", set())[0]
        self.repository.start_attempt(build.attempt_id, build.fencing_token)
        self.repository.complete_attempt(
            build.attempt_id, build.fencing_token, {"status": "succeeded"}
        )

        second = self.controller.reconcile("r1")
        self.assertEqual(second.work_item_ids, ("verify",))
        verify = self.repository.claim_work("w2", set())[0]
        self.repository.start_attempt(verify.attempt_id, verify.fencing_token)
        self.repository.complete_attempt(
            verify.attempt_id, verify.fencing_token, {"status": "succeeded"}
        )

        completed = self.controller.reconcile("r1")
        self.assertEqual(completed.action, "complete")
        self.assertEqual(self.repository.get_snapshot("r1")["status"], "completed")

    def test_high_risk_work_waits_for_revision_bound_approval(self):
        decision = self._create(_plan(high_risk=True))
        self.assertEqual(decision.action, "wait_approval")
        snapshot = self.repository.get_snapshot("r1")
        self.assertEqual(snapshot["status"], "waiting_approval")
        approval_id = next(iter(snapshot["approvals"]))

        self.controller.decide_approval(
            "r1", approval_id, approved=True, decided_by="alice"
        )
        resumed = self.controller.reconcile("r1")
        self.assertEqual(resumed.work_item_ids, ("build",))

    def test_replan_invalidates_pending_approval(self):
        self._create(_plan(high_risk=True))
        snapshot = self.repository.get_snapshot("r1")
        approval_id = next(iter(snapshot["approvals"]))
        updated_build = WorkItem(
            "build",
            "Build safely",
            "execute",
            "executor",
            risk_level="high",
        )
        patch = PlanPatchV1(
            base_revision=1,
            reason="change risky effect",
            update=(updated_build,),
        )
        # A waiting approval is non-terminal and may be replanned.
        self.controller.apply_patch("r1", patch)
        current = self.repository.get_snapshot("r1")
        self.assertEqual(current["approvals"][approval_id]["status"], "invalidated")
        with self.assertRaisesRegex(DomainError, "invalidated"):
            self.controller.decide_approval(
                "r1", approval_id, approved=True, decided_by="alice"
            )

    def test_plan_budget_is_enforced_from_run_policy(self):
        state = self.controller.create_run(
            "p1", "ship", run_id="r1", policy={"max_total_seconds": 100}
        )
        with self.assertRaisesRegex(DomainError, "exceeds run budget"):
            self.controller.commit_plan("r1", _plan(), expected_version=state["version"])


if __name__ == "__main__":
    unittest.main()
