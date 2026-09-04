import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.run_acceptance_longtask_benchmark import (
    DEFAULT_MANIFEST,
    Scenario,
    aggregate_runs,
    build_parser,
    build_report,
    fixture_fingerprint,
    load_scenarios,
    redact_tail,
    run_once,
    run_pair,
    run_independent_acceptance,
    select_scenarios,
    state_event_statistics,
    trace_statistics,
    write_fixture,
)


class AcceptanceLongTaskBenchmarkTests(unittest.TestCase):
    def test_default_is_small_smoke_and_full_is_explicit(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.suite, "smoke")
        self.assertEqual(args.repeat, 1)

        scenarios = load_scenarios(DEFAULT_MANIFEST)
        smoke = select_scenarios(scenarios, suite="smoke", ids=[])
        full = select_scenarios(scenarios, suite="full", ids=[])
        self.assertEqual(len(smoke), 5)
        self.assertEqual(len(full), 8)
        self.assertTrue({scenario.scenario_id for scenario in smoke}.issubset(
            {scenario.scenario_id for scenario in full}
        ))

    def test_manifest_covers_long_task_categories_and_real_fixtures(self):
        scenarios = load_scenarios(DEFAULT_MANIFEST)
        categories = {category for scenario in scenarios for category in scenario.categories}
        self.assertTrue({"multi_file", "delegation", "resume", "budget", "repair_candidate"}.issubset(categories))
        self.assertTrue(any(s.target_repair_rounds == (2, 5) for s in scenarios))
        for scenario in scenarios:
            self.assertGreaterEqual(len(scenario.setup_files), 2)
            self.assertTrue(scenario.acceptance_command)

    def test_scenario_selection_rejects_unknown_id(self):
        scenarios = load_scenarios(DEFAULT_MANIFEST)
        with self.assertRaisesRegex(ValueError, "unknown scenario"):
            select_scenarios(scenarios, suite="smoke", ids=["missing"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            select_scenarios(scenarios, suite="smoke", ids=[scenarios[0].scenario_id] * 2)

    def test_loader_rejects_credential_environment_keys(self):
        payload = {
            "scenarios": [{
                "id": "unsafe",
                "suites": ["smoke"],
                "categories": ["test"],
                "profile": "terminal",
                "prompt": "x",
                "setup_files": {"test_x.py": "pass"},
                "acceptance_command": ["{python}", "-m", "pytest"],
                "environment": {"HARNESS_API_KEY": "do-not-store"},
            }]
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe benchmark environment key"):
                load_scenarios(path)

    def test_fixture_copies_have_identical_fingerprints(self):
        scenario = Scenario(
            scenario_id="fixture",
            suites=("smoke",),
            categories=("test",),
            profile="terminal",
            prompt="test",
            setup_files={"pkg/a.py": "A\n", "tests/test_a.py": "B\n"},
            acceptance_command=("{python}", "-m", "pytest", "-q"),
            timeout_seconds=10,
            acceptance_timeout_seconds=10,
            environment={},
        )
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            write_fixture(scenario, first)
            write_fixture(scenario, second)
            self.assertEqual(fixture_fingerprint(first), fixture_fingerprint(second))
            (second / "pkg/a.py").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(fixture_fingerprint(first), fixture_fingerprint(second))

    def test_pair_execution_order_alternates_between_repeats(self):
        scenario = Scenario(
            scenario_id="order",
            suites=("smoke",),
            categories=("test",),
            profile="terminal",
            prompt="test",
            setup_files={"test_order.py": "assert False\n"},
            acceptance_command=("{python}", "-m", "pytest", "-q"),
            timeout_seconds=10,
            acceptance_timeout_seconds=10,
            environment={},
        )
        fake_result = {
            "valid": True,
            "invalid_reason": None,
            "variant": "",
        }
        with tempfile.TemporaryDirectory() as temp, patch(
            "benchmarks.run_acceptance_longtask_benchmark.run_once",
            side_effect=lambda _scenario, **kwargs: {
                **fake_result,
                "variant": kwargs["variant"],
            },
        ):
            odd = run_pair(scenario, repeat_index=1, root=Path(temp) / "odd")
            even = run_pair(scenario, repeat_index=2, root=Path(temp) / "even")

        self.assertEqual(
            {run["variant"]: run["execution_order"] for run in odd},
            {"off": 1, "on": 2},
        )
        self.assertEqual(
            {run["variant"]: run["execution_order"] for run in even},
            {"off": 2, "on": 1},
        )

    def test_independent_acceptance_uses_argument_list_without_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_independent_acceptance(
                ("{python}", "-c", "from pathlib import Path; assert Path('ok.txt').read_text() == 'ok'"),
                workspace=Path(temp),
                timeout=10,
            )
            self.assertNotEqual(result["returncode"], 0)
            (Path(temp) / "ok.txt").write_text("ok", encoding="utf-8")
            result = run_independent_acceptance(
                ("{python}", "-c", "from pathlib import Path; assert Path('ok.txt').read_text() == 'ok'"),
                workspace=Path(temp),
                timeout=10,
            )
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["command"][0], "{python}")

    def test_all_initial_fixtures_fail_their_acceptance_contract(self):
        scenarios = load_scenarios(DEFAULT_MANIFEST)
        with tempfile.TemporaryDirectory() as temp:
            for scenario in scenarios:
                workspace = Path(temp) / scenario.scenario_id
                write_fixture(scenario, workspace)
                result = run_independent_acceptance(
                    scenario.acceptance_command,
                    workspace=workspace,
                    timeout=30,
                )
                self.assertNotEqual(result["returncode"], 0, scenario.scenario_id)

    def test_trace_statistics_count_duplicate_verification_repair_and_delegation(self):
        events = [
            {"event_type": "tool_requested", "payload": {"tool": "run_bash", "command": "py -m pytest -q", "tool_call_id": "1"}},
            {"event_type": "tool_completed", "payload": {"tool_call_id": "1", "success": False}},
            {"event_type": "tool_requested", "payload": {"tool": "run_bash", "command": "  PY   -m pytest -q ", "tool_call_id": "2"}},
            {"event_type": "tool_completed", "payload": {"tool_call_id": "2", "success": True}},
            {"event_type": "tool_requested", "payload": {"tool": "delegate_task", "tool_call_id": "3"}},
            {"event_type": "acceptance_retry_scheduled", "payload": {"action": "repair"}},
        ]
        stats = trace_statistics(events)
        self.assertEqual(stats["verification_attempt_count"], 2)
        self.assertEqual(stats["repeated_verification_count"], 1)
        self.assertEqual(stats["repair_count"], 1)
        self.assertEqual(stats["delegation_count"], 1)

    def test_state_events_are_used_for_repair_count(self):
        stats = state_event_statistics([
            {"type": "acceptance_retry_scheduled", "data": {"decision": "repair"}},
            {"type": "acceptance_retry_scheduled", "data": {"decision": "verify"}},
        ])
        self.assertEqual(stats["repair_count"], 1)

    def test_legacy_recovery_attempt_is_counted_as_repair(self):
        scenario = Scenario(
            scenario_id="legacy-repair",
            suites=("smoke",),
            categories=("test",),
            profile="terminal",
            prompt="test",
            setup_files={"test_x.py": "assert False\n"},
            acceptance_command=("{python}", "-c", "raise SystemExit(0)"),
            timeout_seconds=10,
            acceptance_timeout_seconds=10,
            environment={},
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            state = {
                "status": "completed",
                "events": [],
                "recovery": {"recovery_attempt_count": 1},
            }
            (workspace / "harness_state.json").write_text(json.dumps(state), encoding="utf-8")
            metrics_dir = workspace / ".harness"
            metrics_dir.mkdir()
            (metrics_dir / "metrics.json").write_text(
                json.dumps({"task_success": True, "summary": {}}), encoding="utf-8"
            )
            with patch(
                "benchmarks.run_acceptance_longtask_benchmark._run_command",
                return_value={
                    "returncode": 0,
                    "timed_out": False,
                    "stdout": "",
                    "stderr": "",
                    "wall_time_ms": 1,
                },
            ):
                result = run_once(
                    scenario,
                    variant="off",
                    controller_enabled=False,
                    repeat_index=1,
                    workspace=workspace,
                    initial_fixture_hash="fixture",
                    timeout_seconds=10,
                )

        self.assertEqual(result["repair_count"], 1)

    def test_aggregate_excludes_entire_invalid_pair(self):
        base = {
            "pair_id": "a:r1",
            "pair_valid": True,
            "reported_completed": True,
            "correct_completion": True,
            "erroneous_completion": False,
            "timed_out": False,
            "llm_call_count": 3,
            "total_tokens": 100,
            "wall_time_ms": 1000,
            "verification_attempt_count": 1,
            "repeated_verification_count": 0,
            "repair_count": 1,
            "recovery_attempt_count": 1,
            "recovery_success_count": 1,
            "resume_attempted": False,
            "resume_succeeded": False,
        }
        runs = [
            dict(base, variant="off"),
            dict(base, variant="on", llm_call_count=2),
            dict(base, pair_id="b:r1", pair_valid=False, variant="off", timed_out=True),
            dict(base, pair_id="b:r1", pair_valid=False, variant="on"),
        ]
        off = aggregate_runs(runs, "off")
        on = aggregate_runs(runs, "on")
        self.assertEqual(off["run_count"], 2)
        self.assertEqual(off["comparable_count"], 1)
        self.assertEqual(off["timeout_count"], 1)
        self.assertEqual(off["avg_llm_calls"], 3)
        self.assertEqual(on["avg_llm_calls"], 2)

    def test_report_contains_completion_and_recovery_comparison(self):
        scenario = load_scenarios(DEFAULT_MANIFEST)[0]
        template = {
            "pair_id": "x:r1", "pair_valid": True, "reported_completed": True,
            "correct_completion": True, "erroneous_completion": False, "timed_out": False,
            "llm_call_count": 1, "total_tokens": 2, "wall_time_ms": 3,
            "verification_attempt_count": 1, "repeated_verification_count": 0,
            "repair_count": 0, "recovery_attempt_count": 0, "recovery_success_count": 0,
            "resume_attempted": False, "resume_succeeded": False,
        }
        report = build_report(
            [dict(template, variant="off"), dict(template, variant="on")],
            suite="smoke", repeat=1, selected_scenarios=[scenario], manifest=DEFAULT_MANIFEST,
        )
        self.assertEqual(report["comparable_pair_count"], 1)
        self.assertIn("completion_rate", report["comparison"])
        self.assertIn("resume_success_rate", report["comparison"])

    def test_redaction_never_emits_environment_secret(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-value-123456"}):
            result = redact_tail(
                "api_key=secret-value-123456 Authorization: Bearer hidden-bearer-value "
                "sk-abcdefghijklmnop"
            )
        self.assertNotIn("secret-value-123456", result)
        self.assertNotIn("hidden-bearer-value", result)
        self.assertNotIn("sk-abcdefghijklmnop", result)
        self.assertIn("<redacted>", result)


if __name__ == "__main__":
    unittest.main()
