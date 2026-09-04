import tempfile
import unittest
from unittest.mock import patch

from orchestrator.scheduler import HarnessPhaseRunner
from orchestrator.state import create_run_state
from profiles.terminal import TerminalProfile


class TerminalBudgetingTests(unittest.TestCase):
    TASKS = {
        "short-task": {
            "agent_timeout_sec": 900.0,
            "difficulty": "medium",
        },
        "long-task": {
            "agent_timeout_sec": 3600.0,
            "difficulty": "hard",
        },
        "very-long-task": {
            "agent_timeout_sec": 12000.0,
            "difficulty": "hard",
        },
    }

    def _prepare(self, task_id, prompt):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        profile = TerminalProfile()
        state = create_run_state(
            prompt,
            workspace.name,
            profile="terminal",
            run_id=f"run-{task_id or 'fallback'}",
            task_id=task_id,
        )
        with patch.object(profile, "_load_tb2_tasks", return_value=self.TASKS), \
             patch("profiles.get_profile", return_value=profile), \
             patch.object(HarnessPhaseRunner, "_ensure_git"):
            harness, _profile, allocation = HarnessPhaseRunner()._prepare(state)
        return harness, allocation, state

    def test_scheduler_applies_900_second_builder_only_budget(self):
        harness, allocation, state = self._prepare("short-task", "unrelated prompt")

        self.assertFalse(allocation["planner_enabled"])
        self.assertFalse(allocation["evaluator_enabled"])
        self.assertAlmostEqual(harness.builder.time_budget, 900.0, delta=1.0)
        self.assertEqual(harness.planner.time_budget, 0.0)
        self.assertEqual(harness.evaluator.time_budget, 0.0)
        self.assertEqual(state["task_budget_seconds"], 900.0)

    def test_scheduler_applies_3600_second_dynamic_budget(self):
        harness, allocation, state = self._prepare("long-task", "unrelated prompt")

        self.assertFalse(allocation["planner_enabled"])
        self.assertTrue(allocation["evaluator_enabled"])
        self.assertAlmostEqual(harness.builder.time_budget, 3240.0, delta=1.0)
        self.assertAlmostEqual(harness.evaluator.time_budget, 360.0, delta=1.0)
        self.assertAlmostEqual(state["phase_budgets"]["builder"], 3240.0, delta=1.0)

    def test_scheduler_applies_12000_second_dynamic_budget(self):
        harness, _allocation, state = self._prepare("very-long-task", "unrelated prompt")

        self.assertAlmostEqual(harness.builder.time_budget, 10800.0, delta=1.0)
        self.assertAlmostEqual(harness.evaluator.time_budget, 1200.0, delta=1.0)
        self.assertEqual(state["task_budget_seconds"], 12000.0)

    def test_explicit_task_id_wins_over_prompt_heuristic(self):
        harness, _allocation, state = self._prepare(
            "short-task",
            "Please execute very-long-task exactly.",
        )

        self.assertAlmostEqual(harness.builder.time_budget, 900.0, delta=1.0)
        self.assertEqual(state["task_budget_seconds"], 900.0)

    def test_missing_task_id_uses_prompt_then_profile_default(self):
        prompt_harness, _allocation, prompt_state = self._prepare(
            None,
            "Please execute very long task exactly.",
        )
        default_harness, _allocation, default_state = self._prepare(
            None,
            "No known task name is present.",
        )

        self.assertAlmostEqual(prompt_harness.builder.time_budget, 10800.0, delta=1.0)
        self.assertEqual(prompt_state["task_budget_seconds"], 12000.0)
        self.assertAlmostEqual(default_harness.builder.time_budget, 1800.0, delta=1.0)
        self.assertEqual(default_state["task_budget_seconds"], 1800.0)

    def test_run_state_reads_task_id_from_environment_with_explicit_override(self):
        with tempfile.TemporaryDirectory() as workspace, \
             patch.dict("os.environ", {"HARNESS_TASK_ID": "from-harbor"}):
            inherited = create_run_state("task", workspace)
            explicit = create_run_state("task", workspace, task_id="explicit")

        self.assertEqual(inherited["task_id"], "from-harbor")
        self.assertEqual(explicit["task_id"], "explicit")

    def test_resumed_round_is_capped_by_remaining_global_budget(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        profile = TerminalProfile()
        state = create_run_state(
            "unrelated",
            workspace.name,
            profile="terminal",
            task_id="long-task",
        )
        from datetime import datetime

        started = datetime.fromisoformat(state["created_at"]).timestamp()
        with patch.object(profile, "_load_tb2_tasks", return_value=self.TASKS), \
             patch("profiles.get_profile", return_value=profile), \
             patch.object(HarnessPhaseRunner, "_ensure_git"), \
             patch("orchestrator.scheduler.time.time", return_value=started + 3500):
            harness, _profile, _allocation = HarnessPhaseRunner()._prepare(state)

        self.assertEqual(harness.builder.time_budget, 90.0)
        self.assertEqual(harness.evaluator.time_budget, 10.0)
        self.assertEqual(
            sum(state["phase_budgets"].values()),
            state["remaining_task_budget_seconds"],
        )
        self.assertEqual(state["remaining_task_budget_seconds"], 100.0)

    def test_evaluator_receives_the_full_remaining_reserve(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        profile = TerminalProfile()
        state = create_run_state(
            "unrelated",
            workspace.name,
            profile="terminal",
            task_id="long-task",
        )
        state["phase"] = "evaluate"
        from datetime import datetime

        started = datetime.fromisoformat(state["created_at"]).timestamp()
        with patch.object(profile, "_load_tb2_tasks", return_value=self.TASKS), \
             patch("profiles.get_profile", return_value=profile), \
             patch.object(HarnessPhaseRunner, "_ensure_git"), \
             patch("orchestrator.scheduler.time.time", return_value=started + 3500):
            harness, _profile, _allocation = HarnessPhaseRunner()._prepare(state)

        self.assertEqual(harness.builder.time_budget, 0.0)
        self.assertEqual(harness.evaluator.time_budget, 100.0)


if __name__ == "__main__":
    unittest.main()
