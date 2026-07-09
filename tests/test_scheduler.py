import tempfile
import unittest
from pathlib import Path

from orchestrator.scheduler import Scheduler, confirm_profile, set_active
from orchestrator.state import create_run_state, load_state, save_state, state_path_for_workspace


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


class SchedulerTests(unittest.TestCase):
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
