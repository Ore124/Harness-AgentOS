import tempfile
import unittest

from orchestrator.memory import MemoryStore, _run_passed, _safe_preview, normalize_failure_reason


class MemoryStoreTests(unittest.TestCase):
    def test_completed_without_validation_is_not_success(self):
        self.assertFalse(_run_passed({"status": "completed"}))

    def test_completed_with_validation_is_success(self):
        self.assertTrue(_run_passed({"status": "completed", "validated": True}))

    def test_sensitive_preview_is_redacted(self):
        self.assertEqual(_safe_preview("Authorization: Bearer abc"), "[redacted]")

    def test_load_migrates_v1_schema_to_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            memory.save({
                "version": 1,
                "profiles": {
                    "terminal": {"attempts": 4, "successes": 3, "average_score": 8.0},
                },
                "runs": [
                    {"run_id": "old", "profile": "terminal", "prompt": "Fix files", "score": 8.0}
                ],
            })

            data = memory.load()

            self.assertEqual(data["version"], 2)
            self.assertIn("short_term", data)
            self.assertIn("long_term", data)
            self.assertEqual(
                data["long_term"]["global_profiles"]["terminal"]["attempts"],
                4,
            )
            self.assertEqual(data["short_term"]["runs"][0]["task_type"], "unclear")

    def test_record_run_keeps_short_term_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            for i in range(55):
                memory.record_run(
                    {
                        "run_id": f"run-{i}",
                        "prompt": "Build a web app",
                        "profile": "app-builder",
                        "status": "completed",
                        "score_history": [8.0],
                        "route_decision": {"task_type": "web_app"},
                    },
                    {"tool_calls": {"total": i}},
                )

            data = memory.load()

            self.assertEqual(len(data["short_term"]["runs"]), 50)
            self.assertEqual(data["short_term"]["runs"][0]["run_id"], "run-5")
            self.assertEqual(
                data["long_term"]["task_types"]["web_app"]["profiles"]["app-builder"]["attempts"],
                55,
            )

    def test_adjust_candidates_uses_task_type_short_and_long_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            memory.save({
                "version": 2,
                "short_term": {
                    "max_runs": 50,
                    "runs": [
                        {
                            "run_id": "recent-success",
                            "task_type": "web_app",
                            "profile": "app-builder",
                            "status": "completed",
                            "final_score": 9.0,
                            "passed": True,
                        },
                        {
                            "run_id": "recent-failure",
                            "task_type": "web_app",
                            "profile": "terminal",
                            "status": "error",
                            "final_score": 2.0,
                            "passed": False,
                        },
                    ],
                },
                "long_term": {
                    "global_profiles": {},
                    "task_types": {
                        "web_app": {
                            "profiles": {
                                "app-builder": {
                                    "attempts": 5,
                                    "successes": 5,
                                    "average_score": 8.8,
                                    "average_tool_calls": 12,
                                    "failure_reasons": {},
                                },
                                "terminal": {
                                    "attempts": 5,
                                    "successes": 0,
                                    "average_score": 2.0,
                                    "average_tool_calls": 20,
                                    "failure_reasons": {"low_score": 5},
                                },
                            }
                        }
                    },
                },
            })
            candidates = [
                {"profile": "app-builder", "confidence": 0.55, "reason": "rule"},
                {"profile": "terminal", "confidence": 0.55, "reason": "rule"},
            ]

            adjusted, info = memory.adjust_candidates(candidates, "Build a web app", "web_app")

            self.assertEqual(adjusted[0]["profile"], "app-builder")
            self.assertGreater(adjusted[0]["confidence"], 0.55)
            self.assertLess(adjusted[1]["confidence"], 0.55)
            self.assertTrue(info["short_term_refs"])
            self.assertTrue(info["long_term_refs"])
            self.assertTrue(info["memory_adjustments"])

    def test_normalize_failure_reason_uses_known_categories(self):
        cases = [
            ({"type": "RateLimitError", "message": "429 too many requests"}, 0.0, False, {}, "api_error"),
            ({"type": "TimeoutError", "message": "task timed out"}, 0.0, False, {}, "timeout"),
            ({"type": "Error", "message": "Playwright executable doesn't exist"}, 0.0, False, {}, "browser_unavailable"),
            (None, 4.0, False, {"errors": [{"message": "pytest failed"}]}, "tests_failed"),
            (None, 0.0, False, {"tool_calls": {"by_tool": {"run_bash": 3}}}, "low_score"),
        ]

        for last_error, score, has_score, analysis, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    normalize_failure_reason(last_error, score, passed=False, has_score=has_score, analysis=analysis),
                    expected,
                )

    def test_record_run_generates_strategy_hint_for_repeated_browser_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            for i in range(3):
                memory.record_run(
                    {
                        "run_id": f"run-{i}",
                        "prompt": "Build a web app",
                        "profile": "app-builder",
                        "status": "error",
                        "score_history": [],
                        "route_decision": {"task_type": "web_app"},
                        "last_error": {
                            "type": "Error",
                            "message": "Playwright executable doesn't exist",
                        },
                    },
                    {"tool_calls": {"total": 4, "by_tool": {"browser_test": 1}}},
                )

            data = memory.load()
            hints = data["strategy_hints"]["web_app"]["app-builder"]

            self.assertTrue(hints)
            self.assertEqual(hints[0]["failure_reason"], "browser_unavailable")
            self.assertIn("Playwright Chromium is unavailable", hints[0]["hint"])

    def test_adjust_candidates_returns_strategy_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(path=f"{tmp}/memory.json")
            memory.save({
                "version": 2,
                "short_term": {"max_runs": 50, "runs": []},
                "long_term": {
                    "global_profiles": {},
                    "task_types": {
                        "web_app": {
                            "profiles": {
                                "app-builder": {
                                    "attempts": 3,
                                    "successes": 0,
                                    "average_score": 0.0,
                                    "average_tool_calls": 4,
                                    "failure_reasons": {"browser_unavailable": 3},
                                }
                            }
                        }
                    },
                },
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

            _adjusted, info = memory.adjust_candidates(
                [{"profile": "app-builder", "confidence": 0.7, "reason": "rule"}],
                "Build a web app",
                "web_app",
            )

            self.assertTrue(info["strategy_hints"])
            self.assertEqual(info["strategy_hints"][0]["profile"], "app-builder")


if __name__ == "__main__":
    unittest.main()
