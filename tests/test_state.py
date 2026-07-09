import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.state import StateError, create_run_state, load_state, save_state, state_path_for_workspace


class StateTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = create_run_state("Build a timer app", tmp, run_id="run-1")
            path = state_path_for_workspace(tmp)
            save_state(path, state)
            loaded = load_state(path)

            self.assertEqual(loaded["run_id"], "run-1")
            self.assertEqual(loaded["prompt"], "Build a timer app")
            self.assertEqual(loaded["next_action"], "route")

    def test_invalid_json_raises_state_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness_state.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(StateError):
                load_state(path)

    def test_missing_required_field_raises_state_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness_state.json"
            path.write_text(json.dumps({"version": 1}), encoding="utf-8")

            with self.assertRaises(StateError):
                load_state(path)


if __name__ == "__main__":
    unittest.main()
