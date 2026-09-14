import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tools
from agents import (
    Agent,
    AgentRunResult,
    EVALUATOR_FINALIZATION_TOOLS,
    _filter_tool_schemas,
)


class AgentRunResultTests(unittest.TestCase):
    def test_time_budget_exit_is_structured_without_an_api_call(self):
        agent = Agent("builder", "system", time_budget=0)

        with patch("agents.get_client") as get_client:
            result = agent.run("task")

        self.assertEqual(result, AgentRunResult("", "time_budget", 1))
        self.assertFalse(result.succeeded)
        get_client.assert_called_once()

    def test_no_tool_calls_is_the_successful_exit(self):
        result = AgentRunResult("done", "no_tool_calls", 2)

        self.assertTrue(result.succeeded)

    def test_length_response_without_tool_calls_is_retried(self):
        truncated = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="partial implementation", tool_calls=None),
            )],
            usage=None,
        )
        finished = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="done", tool_calls=None),
            )],
            usage=None,
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(side_effect=[truncated, finished]),
                ),
            ),
        )

        with patch("agents.get_client", return_value=client):
            result = Agent("builder", "system").run("task")

        self.assertEqual(result, AgentRunResult("done", "no_tool_calls", 2))
        self.assertEqual(client.chat.completions.create.call_count, 2)
        retry_messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertTrue(any(
            "minimal index.html skeleton under 60 lines" in message.get("content", "")
            for message in retry_messages
        ))

    def test_repeated_unparsed_length_responses_fail_the_attempt(self):
        def truncated():
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content="", tool_calls=None),
                )],
                usage=None,
            )

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(side_effect=[truncated(), truncated(), truncated()]),
                ),
            ),
        )

        with patch("agents.get_client", return_value=client):
            result = Agent("builder", "system").run("task")

        self.assertEqual(result.exit_reason, "length_truncated")
        self.assertEqual(result.iterations, 3)
        self.assertFalse(result.succeeded)

    def test_billing_quota_error_is_not_retried_as_rate_limit(self):
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(side_effect=Exception(
                        "Error code: 429 - 余额不足 或无可用资源包"
                    )),
                ),
            ),
        )

        with patch("agents.get_client", return_value=client), patch("agents.time.sleep"):
            result = Agent("builder", "system").run("task")

        self.assertEqual(result.exit_reason, "api_quota")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertFalse(result.succeeded)

    def test_evaluator_finalization_filters_expensive_tools(self):
        agent = Agent(
            "evaluator",
            "system",
            extra_tool_schemas=tools.BROWSER_TOOL_SCHEMAS,
            time_budget=180,
        )

        self.assertFalse(agent._should_finalize_for_time(151))
        self.assertTrue(agent._should_finalize_for_time(166))
        schemas = _filter_tool_schemas(
            tools.TOOL_SCHEMAS + tools.BROWSER_TOOL_SCHEMAS,
            EVALUATOR_FINALIZATION_TOOLS,
        )
        names = {schema["function"]["name"] for schema in schemas}

        self.assertIn("write_file", names)
        self.assertIn("stop_dev_server", names)
        self.assertNotIn("browser_test", names)
        self.assertNotIn("run_bash", names)


if __name__ == "__main__":
    unittest.main()
