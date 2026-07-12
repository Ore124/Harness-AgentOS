import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.analytics import list_artifacts
from orchestrator.scheduler import Scheduler, approve_human_action, confirm_profile, set_active
from orchestrator.state import create_run_state, load_state, now_iso, save_state, state_path_for_workspace


class FakeRunner:
    def __init__(self):
        self.calls = []

    def plan(self, state):
        self.calls.append("plan")
        return state

    def contract(self, state):
        self.calls.append("contract")
        return state

    def build(self, state):
        self.calls.append("build")
        return state

    def evaluate(self, state):
        self.calls.append("evaluate")
        state.setdefault("score_history", []).append(9.0)
        return state


class FileWritingRunner(FakeRunner):
    def build(self, state):
        self.calls.append("build")
        path = Path(state["workspace"]) / "created_by_builder.txt"
        path.write_text("content", encoding="utf-8")
        state["artifacts"] = {"files": list_artifacts(state["workspace"])}
        return state


class FailingRunner(FakeRunner):
    def build(self, state):
        self.calls.append("build")
        raise RuntimeError("build exploded")


class LowScoreRunner(FakeRunner):
    def evaluate(self, state):
        self.calls.append("evaluate")
        feedback = Path(state["workspace"]) / "feedback.md"
        feedback.write_text(
            "Average: 4/10\nFAILED tests/test_app.py::test_feature - AssertionError\napp.py:3",
            encoding="utf-8",
        )
        state.setdefault("score_history", []).append(4.0)
        return state


class SchedulerTests(unittest.TestCase):
    def test_harness_delegates_to_scheduler(self):
        import harness

        class FakeProfile:
            def name(self):
                return "terminal"

            def max_rounds(self):
                return 1

        instance = harness.Harness.__new__(harness.Harness)
        instance.profile = FakeProfile()
        state = {"run_id": "run-1", "workspace": "workspace"}

        with patch.dict("os.environ", {"HARNESS_FLAT_WORKSPACE": "1"}, clear=False), \
             patch("orchestrator.state.create_run_state", return_value=state) as create_state, \
             patch("orchestrator.state.save_state") as save_state, \
             patch("orchestrator.scheduler.Scheduler") as scheduler:
            instance.run("build x")

        create_state.assert_called_once()
        save_state.assert_called_once()
        scheduler.return_value.run_until_idle.assert_called_once()

    def test_inactive_state_does_not_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", next_action="plan", active=False)
            runner = FakeRunner()

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(runner.calls, [])
            self.assertFalse(state["active"])

    def test_resume_from_build_does_not_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="build", next_action="build")
            runner = FakeRunner()

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(runner.calls, ["build"])
            self.assertEqual(state["next_action"], "evaluate")

    def test_recovery_retries_failed_phase_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="build", next_action="build")
            state = load_state(path)
            state["hook_policy"] = {"max_recovery_attempts": 1}
            save_state(path, state)
            runner = FailingRunner()

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(runner.calls, ["build"])
            self.assertTrue(state["active"])
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["next_action"], "build")
            self.assertEqual(state["recovery"]["attempts"], 1)

    def test_recovery_fails_after_attempts_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="build", next_action="build")
            state = load_state(path)
            state["hook_policy"] = {"max_recovery_attempts": 0}
            save_state(path, state)

            state = Scheduler(path, phase_runner=FailingRunner()).step_once()

            self.assertFalse(state["active"])
            self.assertEqual(state["status"], "error")
            self.assertIsNone(state["next_action"])

    def test_artifact_change_hook_records_created_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="build", next_action="build")

            state = Scheduler(path, phase_runner=FileWritingRunner()).step_once()

            self.assertTrue(any(event["type"] == "artifact_changed" for event in state["events"]))
            changes = state.get("artifact_changes", {})
            self.assertIn("created_by_builder.txt", changes.get("created", []))

    def test_validation_hook_records_score_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="evaluate", next_action="evaluate")

            state = Scheduler(path, phase_runner=FakeRunner()).step_once()

            self.assertEqual(state["validation"]["status"], "verified")
            self.assertIn("score", state["validation"]["evidence"])

    def test_evaluate_failure_records_failure_evidence_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="evaluate", next_action="evaluate")
            state = load_state(path)
            state["max_rounds"] = 3
            save_state(path, state)

            state = Scheduler(path, phase_runner=LowScoreRunner()).step_once()

            evidence = state["current_failure_evidence"]
            self.assertEqual(state["next_action"], "build")
            self.assertEqual(state["round_num"], 2)
            self.assertEqual(evidence["failure_type"], "test_failure")
            self.assertEqual(evidence["recovery_strategy"], "targeted_fix")
            self.assertEqual(state["recovery"]["failed_attempt_count"], 1)
            self.assertEqual(state["recovery"]["recovery_attempt_count"], 1)

    def test_repeated_evaluate_failure_escalates_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="evaluate", next_action="evaluate")
            state = load_state(path)
            state["max_rounds"] = 4
            save_state(path, state)
            scheduler = Scheduler(path, phase_runner=LowScoreRunner())

            first = scheduler.step_once()
            first["phase"] = "evaluate"
            first["next_action"] = "evaluate"
            save_state(path, first)
            second = scheduler.step_once()
            second["phase"] = "evaluate"
            second["next_action"] = "evaluate"
            save_state(path, second)
            third = scheduler.step_once()

            self.assertEqual(third["current_failure_evidence"]["same_failure_count"], 3)
            self.assertEqual(third["current_failure_evidence"]["recovery_strategy"], "escalate_analysis")
            self.assertEqual(third["recovery"]["same_failure_escalation_count"], 1)

    def test_human_approval_blocks_and_approval_unblocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="build", next_action="build")
            state = load_state(path)
            state["requires_human_approval"] = True
            state["human_approval"] = {"reason": "risky action"}
            save_state(path, state)

            blocked = Scheduler(path, phase_runner=FakeRunner()).step_once()
            approved = approve_human_action(path)

            self.assertEqual(blocked["status"], "waiting_approval")
            self.assertFalse(blocked["active"])
            self.assertTrue(approved["active"])
            self.assertFalse(approved["requires_human_approval"])

    def test_tool_budget_hook_pauses_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="build", next_action="build")
            trace = Path(tmp) / "_trace_builder.jsonl"
            trace.write_text('{"event":"tool_call","tool":"run_bash"}\n', encoding="utf-8")
            state = load_state(path)
            state["hook_policy"] = {"max_tool_calls": 0}
            save_state(path, state)

            state = Scheduler(path, phase_runner=FakeRunner()).step_once()

            self.assertEqual(state["status"], "paused")
            self.assertFalse(state["active"])
            self.assertEqual(state["last_error"]["type"], "BudgetExceeded")

    def test_route_stores_task_type_on_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="auto", phase="route", next_action="route")
            state = load_state(path)
            state["prompt"] = "Build a browser web app with HTML and CSS"
            save_state(path, state)

            state = Scheduler(path, phase_runner=FakeRunner()).step_once()

            self.assertEqual(state["task_type"], state["route_decision"]["task_type"])
            self.assertEqual(state["next_action"], "plan")
            self.assertTrue(any(event["type"] == "before_route" for event in state["events"]))
            self.assertTrue(any(event["type"] == "after_route" for event in state["events"]))

    def test_confirm_profile_unblocks_waiting_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="auto", active=False)
            state = load_state(path)
            state["requires_confirmation"] = True
            state["status"] = "waiting_confirmation"
            state["next_action"] = None
            save_state(path, state)

            updated = confirm_profile(path, "app-builder")

            self.assertEqual(updated["profile"], "app-builder")
            self.assertTrue(updated["active"])
            self.assertEqual(updated["next_action"], "plan")

    def test_pause_and_resume_toggle_active_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", next_action="plan")

            paused = set_active(path, False)
            resumed = set_active(path, True)

            self.assertFalse(paused["active"])
            self.assertEqual(paused["status"], "paused")
            self.assertTrue(resumed["active"])
            self.assertEqual(resumed["status"], "running")

    def test_analyze_phase_runs_memory_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, profile="terminal", phase="analyze", next_action="analyze")

            state = Scheduler(path, phase_runner=FakeRunner()).step_once()

            self.assertEqual(state["status"], "completed")
            self.assertTrue(any(event["type"] == "before_memory_update" for event in state["events"]))
            self.assertTrue(any(event["type"] == "after_memory_update" for event in state["events"]))

    def _write_state(self, tmp, profile="terminal", phase="plan", next_action="plan", active=True):
        workspace = Path(tmp)
        state = create_run_state("Run tests", workspace, profile=profile, run_id="run")
        state["phase"] = phase
        state["next_action"] = next_action
        state["active"] = active
        path = state_path_for_workspace(workspace)
        save_state(path, state)
        return path


if __name__ == "__main__":
    unittest.main()
