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
        self.assertFalse(decision.requires_confirmation)

    def test_rule_routes_obvious_web_task(self):
        router = Router()
        decision = router.route("Build a browser web app with HTML CSS and buttons")

        self.assertEqual(decision.profile, "app-builder")
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
                "version": 1,
                "profiles": {
                    "terminal": {"attempts": 5, "successes": 5, "average_score": 9.5},
                },
                "runs": [],
            })
            router = Router(memory=memory, llm_router=lambda _p, _c: {
                "profile": "terminal",
                "confidence": 0.6,
                "reasoning": "LLM selected terminal.",
            })
            decision = router.route("Set up the server from command line instructions")

            self.assertTrue(decision.memory_refs)
            self.assertEqual(decision.profile, "terminal")


if __name__ == "__main__":
    unittest.main()
