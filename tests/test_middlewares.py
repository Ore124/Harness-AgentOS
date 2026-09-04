import unittest
from unittest.mock import patch

from middlewares import BrowserTestBudgetMiddleware, PreExitVerificationMiddleware
from tools import ToolResultOutcome


class BrowserTestBudgetMiddlewareTests(unittest.TestCase):
    def test_warns_at_soft_limit_once(self):
        middleware = BrowserTestBudgetMiddleware(soft_limit=2, hard_limit=4)

        self.assertIsNone(middleware.post_tool("browser_test", {}, "ok", []))
        warning = middleware.post_tool("browser_test", {}, "ok", [])
        repeated = middleware.post_tool("browser_test", {}, "ok", [])

        self.assertIn("enough browser evidence", warning)
        self.assertIsNone(repeated)

    def test_blocks_at_hard_limit(self):
        middleware = BrowserTestBudgetMiddleware(soft_limit=2, hard_limit=3)

        middleware.post_tool("browser_test", {}, "ok", [])
        middleware.post_tool("browser_test", {}, "ok", [])
        warning = middleware.post_tool("browser_test", {}, "ok", [])

        self.assertIn("Browser test budget exhausted", warning)
        self.assertIn("write feedback.md", warning)

    def test_ignores_other_tools(self):
        middleware = BrowserTestBudgetMiddleware(soft_limit=1, hard_limit=2)

        self.assertIsNone(middleware.post_tool("read_file", {}, "ok", []))
        self.assertEqual(middleware.browser_test_count, 0)


class PreExitVerificationMiddlewareTests(unittest.TestCase):
    @staticmethod
    def _worked_messages():
        return [{
            "role": "assistant",
            "tool_calls": [{
                "id": "work",
                "function": {"name": "write_file", "arguments": "{}"},
            }],
        }]

    @patch("tools.classify_tool_result")
    def test_requires_successful_concrete_verification(self, classify):
        classify.return_value = ToolResultOutcome(success=True)
        middleware = PreExitVerificationMiddleware(include_task_requirements=False)
        messages = self._worked_messages()

        first = middleware.pre_exit(messages)
        middleware.post_tool("run_bash", {"command": "ls -la"}, "files", messages)
        second = middleware.pre_exit(messages)
        middleware.post_tool("run_bash", {"command": "python -m pytest -q"}, "2 passed", messages)

        self.assertIn("MANDATORY VERIFICATION", first)
        self.assertIn("VERIFICATION IS STILL MISSING", second)
        self.assertIsNone(middleware.pre_exit(messages))

    @patch("tools.classify_tool_result")
    def test_reuses_proactive_test_but_invalidates_it_after_edit(self, classify):
        classify.return_value = ToolResultOutcome(success=True)
        middleware = PreExitVerificationMiddleware(include_task_requirements=False)
        messages = self._worked_messages()

        middleware.post_tool("run_bash", {"command": "pytest -q"}, "2 passed", messages)
        self.assertIsNone(middleware.pre_exit(messages))

        middleware.post_tool("edit_file", {"path": "app.py"}, "Edited app.py", messages)
        self.assertIn("MANDATORY VERIFICATION", middleware.pre_exit(messages))

    @patch("tools.classify_tool_result")
    def test_windows_py_verification_pass_is_not_nudged_again(self, classify):
        classify.return_value = ToolResultOutcome(success=True, exit_code=0)
        middleware = PreExitVerificationMiddleware(include_task_requirements=False)
        messages = self._worked_messages()

        middleware.post_tool(
            "run_bash",
            {"command": "py -m pytest -q"},
            "1 passed\n[exit code: 0]",
            messages,
        )

        self.assertIsNone(middleware.pre_exit(messages))
        self.assertIsNone(middleware.pre_exit(messages))

    @patch("tools.classify_tool_result")
    def test_failed_verification_requires_fix_and_rerun(self, classify):
        classify.side_effect = [
            ToolResultOutcome(success=False, failure_kind="nonzero_exit", exit_code=1),
            ToolResultOutcome(success=True, exit_code=0),
        ]
        middleware = PreExitVerificationMiddleware(include_task_requirements=False)
        messages = self._worked_messages()

        middleware.pre_exit(messages)
        middleware.post_tool("run_bash", {"command": "./test.sh"}, "[exit code: 1]", messages)
        retry = middleware.pre_exit(messages)
        middleware.post_tool("run_bash", {"command": "./test.sh"}, "ok", messages)

        self.assertIn("VERIFICATION FAILED", retry)
        self.assertIsNone(middleware.pre_exit(messages))

    def test_verification_nudges_are_bounded(self):
        middleware = PreExitVerificationMiddleware(
            include_task_requirements=False,
            max_verification_nudges=2,
        )
        messages = self._worked_messages()

        self.assertIsNotNone(middleware.pre_exit(messages))
        self.assertIsNotNone(middleware.pre_exit(messages))
        self.assertIsNone(middleware.pre_exit(messages))


if __name__ == "__main__":
    unittest.main()
