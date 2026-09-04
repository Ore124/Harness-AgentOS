import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import harness


class HarnessRunAndCliTests(unittest.TestCase):
    def test_run_returns_scheduler_final_state(self):
        final_state = {"status": "completed", "workspace": "workspace"}
        profile = MagicMock()
        profile.name.return_value = "terminal"
        profile.max_rounds.return_value = 1
        instance = harness.Harness.__new__(harness.Harness)
        instance.profile = profile

        with tempfile.TemporaryDirectory() as root, \
                patch.dict(os.environ, {"HARNESS_FLAT_WORKSPACE": "1"}), \
                patch.object(config, "WORKSPACE", root), \
                patch("orchestrator.state.save_state"), \
                patch("orchestrator.scheduler.Scheduler") as scheduler_cls:
            scheduler_cls.return_value.run_until_idle.return_value = final_state

            result = instance.run("do the task")

        self.assertIs(result, final_state)

    def test_exit_code_reflects_final_scheduler_state(self):
        cases = (
            ({"status": "completed"}, 0),
            ({"status": "error"}, 1),
            ({"status": "waiting_confirmation", "requires_confirmation": True}, 2),
            ({"status": "waiting_approval"}, 2),
        )
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertEqual(harness._exit_code_for_final_state(state), expected)

    def test_main_exits_nonzero_when_run_returns_error_state(self):
        final_state = {
            "status": "error",
            "last_error": {"type": "ValidationFailed"},
            "workspace": str(Path("workspace")),
        }
        with patch.dict(os.environ, {"HARNESS_FLAT_WORKSPACE": "1"}), \
                patch.object(config, "API_KEY", "test-key"), \
                patch("sys.argv", ["harness.py", "test task"]), \
                patch("harness.get_profile", return_value=MagicMock()), \
                patch.object(harness.Harness, "__init__", return_value=None), \
                patch.object(harness.Harness, "run", return_value=final_state):
            with self.assertRaises(SystemExit) as raised:
                harness.main()

        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
