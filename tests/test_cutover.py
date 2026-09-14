import unittest

from orchestrator.cutover import compare_shadow_terminal, select_runtime


class CutoverTests(unittest.TestCase):
    def test_project_rollout_is_stable_and_scoped(self):
        first = select_runtime(
            "enabled",
            "request-1",
            default_runtime="legacy",
            durable_projects={"enabled"},
            rollout_percent=50,
        )
        second = select_runtime(
            "enabled",
            "request-1",
            default_runtime="legacy",
            durable_projects={"enabled"},
            rollout_percent=50,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            select_runtime(
                "disabled",
                "request-1",
                default_runtime="legacy",
                durable_projects={"enabled"},
                rollout_percent=100,
            ),
            "legacy",
        )

    def test_shadow_comparison_requires_current_revision_verification(self):
        result = compare_shadow_terminal(
            {"status": "completed", "task_success": True},
            {
                "status": "completed",
                "current_plan_revision": 2,
                "verified_revision": 1,
            },
        )
        self.assertFalse(result["terminal_match"])
        self.assertFalse(result["durable_verified"])


if __name__ == "__main__":
    unittest.main()
