import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import metrics
import tools
from agents import AgentRunResult


class ToolResultClassificationTests(unittest.TestCase):
    def test_classifies_protocol_markers_and_invalid_results(self):
        self.assertEqual(
            tools.classify_tool_result("[exit code: 1]\nFAILED", "run_bash"),
            tools.ToolResultOutcome(False, "nonzero_exit", 1),
        )
        self.assertEqual(
            tools.classify_tool_result("[exit code: 0]\npassed", "run_bash"),
            tools.ToolResultOutcome(True, None, 0),
        )
        self.assertEqual(
            tools.classify_tool_result("[error] timed out", "run_bash"),
            tools.ToolResultOutcome(False, "error_marker", None),
        )
        self.assertTrue(tools.classify_tool_result("normal output").success)
        self.assertEqual(
            tools.classify_tool_result(RuntimeError("boom")).failure_kind,
            "exception",
        )
        self.assertEqual(tools.classify_tool_result(None).failure_kind, "invalid_result")

    def test_metrics_records_nonzero_bash_as_failed_and_zero_as_success(self):
        recorder = metrics.MetricsRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            recorder.start_run("tool-status", tmp)
            recorder.record_tool_call(
                role="builder",
                phase="build",
                tool_name="run_bash",
                arguments={"command": "false"},
                latency_ms=1,
                result="[exit code: 2]\nfailed",
            )
            recorder.record_tool_call(
                role="builder",
                phase="build",
                tool_name="run_bash",
                arguments={"command": "true"},
                latency_ms=1,
                result="ok",
            )

        failed, succeeded = recorder.data["tool_calls"]
        self.assertFalse(failed["success"])
        self.assertEqual(failed["failure_kind"], "nonzero_exit")
        self.assertEqual(failed["exit_code"], 2)
        self.assertTrue(succeeded["success"])
        self.assertIsNone(succeeded["failure_kind"])

    def test_legacy_trace_includes_shared_outcome_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = tools.activate_workspace(Path(tmp))
            try:
                writer = agents.TraceWriter("builder")
                writer.tool_call("run_bash", {"command": "false"}, "[exit code: 1]\nno")
                event = json.loads(writer._path.read_text(encoding="utf-8").splitlines()[-1])
            finally:
                tools.reset_workspace(token)

        self.assertFalse(event["success"])
        self.assertEqual(event["failure_kind"], "nonzero_exit")
        self.assertEqual(event["exit_code"], 1)


class DelegateTaskResultTests(unittest.TestCase):
    def test_structured_agent_result_returns_text_without_type_error(self):
        with patch("agents.Agent") as agent_cls:
            agent_cls.return_value.run.return_value = AgentRunResult(
                "useful summary",
                "no_tool_calls",
                2,
            )
            result = tools.delegate_task("inspect code")

        self.assertEqual(result, "useful summary")

    def test_incomplete_agent_result_reports_reason_and_preserves_text(self):
        with patch("agents.Agent") as agent_cls:
            agent_cls.return_value.run.return_value = AgentRunResult(
                "partial findings",
                "time_budget",
                3,
            )
            result = tools.delegate_task("inspect code")

        self.assertIn("[sub-agent incomplete: time_budget]", result)
        self.assertIn("partial findings", result)

    def test_legacy_string_agent_result_remains_supported(self):
        with patch("agents.Agent") as agent_cls:
            agent_cls.return_value.run.return_value = "legacy summary"
            result = tools.delegate_task("inspect code")

        self.assertEqual(result, "legacy summary")


if __name__ == "__main__":
    unittest.main()
