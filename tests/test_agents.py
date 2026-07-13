import unittest
from unittest.mock import patch

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
