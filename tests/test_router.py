import tempfile
import unittest

from orchestrator.memory import MemoryStore
from orchestrator.router import Router


class RouterTests(unittest.TestCase):
    def test_manual_override_wins(self):
        router = Router()
        decision = router.route("Build a web app", override_profile="terminal")

        self.assertEqual(decision.profile, "terminal")
        self.assertEqual(decision.source, "manual")
        self.assertEqual(decision.task_type, "web_app")
        self.assertFalse(decision.requires_confirmation)

    def test_rule_routes_obvious_web_task(self):
        router = Router()
        decision = router.route("Build a browser web app with HTML CSS and buttons")

        self.assertEqual(decision.profile, "app-builder")
        self.assertEqual(decision.task_type, "web_app")
        self.assertGreaterEqual(decision.task_type_confidence, 0.55)
        self.assertGreaterEqual(decision.confidence, 0.55)

    def test_llm_fallback_handles_ambiguous_task(self):
        def fake_llm(prompt, candidates):
            return {
                "profile": "reasoning",
                "confidence": 0.82,
                "reasoning": "The prompt asks for analysis rather than implementation.",
            }

        router = Router(llm_router=fake_llm)
        decision = router.route("I need help thinking through this")

        self.assertEqual(decision.profile, "reasoning")
        self.assertEqual(decision.source, "llm")
        self.assertFalse(decision.requires_confirmation)

    def test_low_confidence_requires_confirmation(self):
        router = Router(llm_router=lambda _p, _c: None, confirmation_threshold=0.9)
        decision = router.route("Handle this")

        self.assertIsNone(decision.profile)
        self.assertTrue(decision.requires_confirmation)
        self.assertGreater(len(decision.alternatives), 0)

    def test_memory_adjusts_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            memory.save({
                "version": 2,
                "short_term": {"max_runs": 50, "runs": []},
                "long_term": {
                    "global_profiles": {},
                    "task_types": {
                        "terminal_ops": {
                            "profiles": {
                                "terminal": {
                                    "attempts": 5,
                                    "successes": 5,
                                    "average_score": 9.5,
                                    "average_tool_calls": 10,
                                    "failure_reasons": {},
                                }
                            }
                        }
                    },
                },
            })
            router = Router(memory=memory, llm_router=lambda _p, _c: {
                "profile": "terminal",
                "confidence": 0.6,
                "reasoning": "LLM selected terminal.",
            })
            decision = router.route("Set up the server from command line instructions")

            self.assertTrue(decision.memory_refs)
            self.assertTrue(decision.long_term_refs)
            self.assertTrue(decision.memory_adjustments)
            self.assertEqual(decision.task_type, "terminal_ops")
            self.assertEqual(decision.profile, "terminal")

    def test_route_decision_includes_strategy_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            memory.save({
                "version": 2,
                "short_term": {"max_runs": 50, "runs": []},
                "long_term": {"global_profiles": {}, "task_types": {}},
                "strategy_hints": {
                    "web_app": {
                        "app-builder": [
                            {
                                "task_type": "web_app",
                                "profile": "app-builder",
                                "failure_reason": "browser_unavailable",
                                "hint": "Playwright Chromium is unavailable; prefer static checks until installed.",
                                "source": "failure_pattern",
                                "confidence": 0.8,
                            }
                        ]
                    }
                },
            })
            router = Router(memory=memory, llm_router=lambda _p, _c: None)

            decision = router.route("Build a browser web app")

            self.assertTrue(decision.strategy_hints)
            self.assertIn("strategy_hints", decision.to_dict())

    def test_route_decision_serializes_task_type_and_memory_fields(self):
        router = Router(llm_router=lambda _p, _c: None)
        decision = router.route("Explain why this equation works")
        data = decision.to_dict()

        self.assertIn("task_type", data)
        self.assertIn("task_type_confidence", data)
        self.assertIn("short_term_refs", data)
        self.assertIn("long_term_refs", data)
        self.assertIn("memory_adjustments", data)
        self.assertIn("strategy_hints", data)


if __name__ == "__main__":
    unittest.main()
