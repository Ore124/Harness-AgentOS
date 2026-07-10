import tempfile
import unittest
from pathlib import Path

from orchestrator.scheduler import HarnessPhaseRunner
from orchestrator.strategy import (
    append_strategy_hints_to_prompt,
    format_strategy_hints_for_prompt,
    select_strategy_hints_for_phase,
)


class StrategyHintTests(unittest.TestCase):
    def test_select_strategy_hints_filters_by_phase_profile_task_and_confidence(self):
        state = {
            "profile": "app-builder",
            "task_type": "web_app",
            "strategy_hints": [
                {
                    "task_type": "web_app",
                    "profile": "app-builder",
                    "failure_reason": "browser_unavailable",
                    "hint": "Verify browser availability first.",
                    "confidence": 0.82,
                },
                {
                    "task_type": "web_app",
                    "profile": "app-builder",
                    "failure_reason": "api_error",
                    "hint": "Check API limits.",
                    "confidence": 0.9,
                },
                {
                    "task_type": "web_app",
                    "profile": "terminal",
                    "failure_reason": "tool_missing",
                    "hint": "Wrong profile.",
                    "confidence": 0.95,
                },
                {
                    "task_type": "web_app",
                    "profile": "app-builder",
                    "failure_reason": "tests_failed",
                    "hint": "Low confidence hint.",
                    "confidence": 0.5,
                },
            ],
        }

        selected = select_strategy_hints_for_phase(state, "builder")

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["failure_reason"], "browser_unavailable")

    def test_evaluator_only_receives_evaluator_safe_reasons(self):
        state = {
            "profile": "app-builder",
            "task_type": "web_app",
            "strategy_hints": [
                {
                    "task_type": "web_app",
                    "profile": "app-builder",
                    "failure_reason": "tool_missing",
                    "hint": "Probe tools.",
                    "confidence": 0.9,
                },
                {
                    "task_type": "web_app",
                    "profile": "app-builder",
                    "failure_reason": "tests_failed",
                    "hint": "Run focused verification.",
                    "confidence": 0.86,
                },
            ],
        }

        selected = select_strategy_hints_for_phase(state, "evaluator")

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["failure_reason"], "tests_failed")

    def test_format_strategy_hints_marks_them_advisory(self):
        prompt = format_strategy_hints_for_prompt([
            {
                "failure_reason": "timeout",
                "hint": "Avoid long exploratory commands.",
                "confidence": 0.8,
            }
        ])

        self.assertIn("advisory only", prompt)
        self.assertIn("Current task requirements", prompt)
        self.assertIn("Avoid long exploratory commands.", prompt)

    def test_append_strategy_hints_to_prompt_is_noop_without_selected_hints(self):
        prompt = append_strategy_hints_to_prompt("Base task", {"strategy_hints": []}, "builder")

        self.assertEqual(prompt, "Base task")

    def test_build_phase_injects_selected_strategy_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_agent = FakeAgent()
            fake_harness = FakeHarness(builder=fake_agent)
            fake_profile = FakeProfile()
            runner = HarnessPhaseRunner()
            runner._prepare = lambda _state: (fake_harness, fake_profile, {"evaluator_enabled": True})
            state = self._state(tmp)

            runner.build(state)

            self.assertIn("Historical strategy hints", fake_agent.prompt)
            self.assertIn("Verify browser availability first.", fake_agent.prompt)

    def test_evaluate_phase_injects_selected_strategy_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_agent = FakeAgent()
            fake_harness = FakeHarness(evaluator=fake_agent)
            fake_profile = FakeProfile()
            runner = HarnessPhaseRunner()
            runner._prepare = lambda _state: (fake_harness, fake_profile, {"evaluator_enabled": True})
            state = self._state(tmp)

            runner.evaluate(state)

            self.assertIn("Historical strategy hints", fake_agent.prompt)
            self.assertIn("Verify browser availability first.", fake_agent.prompt)

    def _state(self, tmp):
        return {
            "workspace": tmp,
            "prompt": "Build a browser web app",
            "profile": "app-builder",
            "task_type": "web_app",
            "round_num": 1,
            "score_history": [],
            "created_at": "2026-07-10T00:00:00+00:00",
            "strategy_hints": [
                {
                    "task_type": "web_app",
                    "profile": "app-builder",
                    "failure_reason": "browser_unavailable",
                    "hint": "Verify browser availability first.",
                    "confidence": 0.82,
                }
            ],
        }


class FakeAgent:
    def __init__(self):
        self.prompt = ""
        self.middlewares = []

    def run(self, prompt):
        self.prompt = prompt
        return "ok"


class FakeHarness:
    def __init__(self, builder=None, evaluator=None):
        self.builder = builder or FakeAgent()
        self.evaluator = evaluator or FakeAgent()


class FakeProfile:
    def resolve_task_timeout(self, _prompt):
        return None

    def format_build_task(self, user_prompt, round_num, prev_feedback, score_history):
        return f"Task: {user_prompt}\nRound: {round_num}\nFeedback: {prev_feedback}\nScores: {score_history}"

    def extract_score(self, _feedback_text):
        return 0.0


if __name__ == "__main__":
    unittest.main()
