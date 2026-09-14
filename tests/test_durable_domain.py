import unittest

from orchestrator.domain import (
    CompletionPolicy,
    ConcurrencyError,
    DomainError,
    PlanDocumentV1,
    PlanPatchV1,
    WorkItem,
    WorkItemKind,
    WorkItemStatus,
    WorkBudget,
)
from orchestrator.plan_graph import apply_plan_patch, plan_is_complete, ready_work_item_ids


def _item(item_id, *, kind="execute", dependencies=()):
    return WorkItem(
        id=item_id,
        title=item_id,
        kind=kind,
        agent_role=kind,
        dependencies=tuple(dependencies),
    )


def _plan():
    return PlanDocumentV1(
        revision=1,
        goal="ship a verified change",
        success_criteria=("tests pass",),
        assumptions=(),
        work_items=(
            _item("build"),
            _item("verify", kind=WorkItemKind.VERIFY.value, dependencies=("build",)),
        ),
        completion_policy=CompletionPolicy(("build", "verify"), True),
    )


class DurableDomainTests(unittest.TestCase):
    def test_plan_rejects_missing_dependency_and_explicit_cycle(self):
        with self.assertRaisesRegex(DomainError, "missing dependencies"):
            PlanDocumentV1(
                revision=1,
                goal="x",
                success_criteria=("done",),
                assumptions=(),
                work_items=(_item("build", dependencies=("missing",)),),
                completion_policy=CompletionPolicy(require_verifier=False),
            ).validate()

        cyclic = PlanDocumentV1(
            revision=1,
            goal="x",
            success_criteria=("done",),
            assumptions=(),
            work_items=(
                _item("a", dependencies=("b",)),
                _item("b", dependencies=("a",)),
            ),
            completion_policy=CompletionPolicy(require_verifier=False),
        )
        with self.assertRaisesRegex(DomainError, r"a.*b.*a"):
            cyclic.validate()

    def test_ready_nodes_follow_dependencies_deterministically(self):
        plan = _plan()
        self.assertEqual(ready_work_item_ids(plan, {}), ("build",))
        self.assertEqual(
            ready_work_item_ids(
                plan, {"build": WorkItemStatus.SUCCEEDED.value}
            ),
            ("verify",),
        )

    def test_completion_requires_current_revision_verification(self):
        plan = _plan()
        statuses = {
            "build": WorkItemStatus.SUCCEEDED.value,
            "verify": WorkItemStatus.SUCCEEDED.value,
        }
        self.assertFalse(plan_is_complete(plan, statuses, verified_revision=None))
        self.assertTrue(plan_is_complete(plan, statuses, verified_revision=1))

    def test_patch_rejects_stale_revision_and_completed_item_mutation(self):
        plan = _plan()
        with self.assertRaises(ConcurrencyError):
            apply_plan_patch(plan, PlanPatchV1(0, "stale"))

        changed = _item("build", dependencies=("verify",))
        with self.assertRaisesRegex(DomainError, "succeeded"):
            apply_plan_patch(
                plan,
                PlanPatchV1(1, "change completed", update=(changed,)),
                {"build": WorkItemStatus.SUCCEEDED.value},
            )

    def test_patch_adds_diagnosis_as_new_plan_revision(self):
        plan = _plan()
        diagnosis = _item("diagnose", kind=WorkItemKind.DIAGNOSE.value)
        updated = apply_plan_patch(
            plan,
            PlanPatchV1(1, "verification failed", add=(diagnosis,)),
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual([item.id for item in updated.work_items][-1], "diagnose")

    def test_plan_enforces_token_and_cost_budgets(self):
        plan = PlanDocumentV1(
            revision=1,
            goal="bounded",
            success_criteria=("done",),
            assumptions=(),
            work_items=(
                WorkItem(
                    "work",
                    "Work",
                    "execute",
                    "executor",
                    budget=WorkBudget(max_tokens=100, max_cost_usd=2.5),
                ),
            ),
            completion_policy=CompletionPolicy(("work",), False),
        )
        with self.assertRaisesRegex(DomainError, "tokens"):
            plan.validate(max_total_tokens=99)
        with self.assertRaisesRegex(DomainError, "run budget"):
            plan.validate(max_total_cost_usd=2.0)


if __name__ == "__main__":
    unittest.main()
