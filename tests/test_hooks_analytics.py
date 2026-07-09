import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.analytics import analyze_workspace
from orchestrator.hooks import HookManager
from orchestrator.state import create_run_state, load_state, save_state, state_path_for_workspace


class HookAnalyticsTests(unittest.TestCase):
    def test_hook_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = state_path_for_workspace(tmp)
            save_state(path, create_run_state("task", tmp, profile="terminal", run_id="run"))

            HookManager(path).before_agent_run("builder", load_state(path))
            state = load_state(path)

            self.assertTrue(any(event["type"] == "before_agent_run" for event in state["events"]))

    def test_analysis_parses_trace_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "_trace_builder.jsonl"
            trace.write_text(
                json.dumps({"agent": "builder", "event": "tool_call", "tool": "run_bash", "result": "ok"}) + "\n"
                + json.dumps({"agent": "builder", "event": "error", "type": "api_error", "message": "boom"}) + "\n"
                + json.dumps({"agent": "builder", "event": "finish", "reason": "no_tool_calls"}) + "\n",
                encoding="utf-8",
            )

            analysis = analyze_workspace(tmp)

            self.assertEqual(analysis["tool_calls"]["total"], 1)
            self.assertEqual(analysis["tool_calls"]["by_tool"]["run_bash"], 1)
            self.assertEqual(analysis["finish_reasons"]["no_tool_calls"], 1)
            self.assertEqual(len(analysis["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
