import tempfile
import unittest
from pathlib import Path

from orchestrator.failure_evidence import build_failure_evidence, render_retry_context
from orchestrator.state import create_run_state


class FailureEvidenceTests(unittest.TestCase):
    def test_extracts_test_failure_signature_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "_trace_builder.jsonl").write_text(
                '{"event":"tool_call","tool":"run_bash","result":"[exit code: 1]\\nFAILED tests/test_math.py::test_add - AssertionError: expected 4\\napp/math.py:12"}\n',
                encoding="utf-8",
            )
            state = create_run_state("Fix math", workspace, profile="terminal", run_id="run")

            evidence = build_failure_evidence(state, phase="evaluate")

        self.assertEqual(evidence["failure_type"], "test_failure")
        self.assertIn("tests/test_math.py::test_add", evidence["failed_checks"])
        self.assertIn("app/math.py", evidence["suspected_files"])
        self.assertTrue(evidence["failure_signature"].startswith("test_failure:"))
        self.assertEqual(evidence["recovery_strategy"], "targeted_fix")

    def test_same_signature_escalates_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            state = create_run_state("Fix math", workspace, profile="terminal", run_id="run")
            first = build_failure_evidence(
                state,
                feedback_text="FAILED tests/test_math.py::test_add - AssertionError: expected 4",
            )
            state["failure_evidence_history"] = [first]

            second = build_failure_evidence(
                state,
                feedback_text="FAILED tests/test_math.py::test_add - AssertionError: expected 5",
            )
            state["failure_evidence_history"].append(second)
            third = build_failure_evidence(
                state,
                feedback_text="FAILED tests/test_math.py::test_add - AssertionError: expected 6",
            )

        self.assertEqual(second["same_failure_count"], 2)
        self.assertEqual(second["recovery_strategy"], "reinspect_assumptions")
        self.assertEqual(third["same_failure_count"], 3)
        self.assertEqual(third["recovery_strategy"], "escalate_analysis")

    def test_render_retry_context_is_structured_and_targeted(self):
        state = {
            "current_failure_evidence": {
                "failure_type": "syntax_error",
                "failure_signature": "syntax_error:abc",
                "failed_checks": [],
                "evidence": ["SyntaxError: invalid syntax"],
                "suspected_files": ["main.py"],
                "recent_changed_files": ["main.py"],
                "retry_goal": "Fix syntax",
                "same_failure_count": 1,
                "recovery_strategy": "targeted_fix",
            }
        }

        context = render_retry_context(state, "long previous feedback")

        self.assertIn("Evidence-Guided Recovery", context)
        self.assertIn('"failure_signature": "syntax_error:abc"', context)
        self.assertIn("Fix syntax", context)


if __name__ == "__main__":
    unittest.main()
