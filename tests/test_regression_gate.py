import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.canonical_trace import writer_for
from orchestrator.regression_gate import evaluate_gate


class RegressionGateTests(unittest.TestCase):
    def _trace(self, root, run_id, task_id, *, success=True, input_tokens=10, output_tokens=5, rounds=1, workspace="fixture-workspace", command=None, terminal=True):
        writer = writer_for(root, run_id)
        writer.emit("run_started", {"task_id": task_id, "model": "fixture-model", "initial_workspace": workspace, "feature_flags": {}})
        for iteration in range(rounds):
            call_id = f"llm-{iteration}"
            writer.emit("agent_round", {"iteration": iteration + 1}, role="builder", phase="build")
            writer.emit("llm_request_started", {"llm_call_id": call_id}, role="builder", phase="build")
            writer.emit("llm_request_completed", {"llm_call_id": call_id, "input_tokens": input_tokens // rounds, "cached_tokens": 0, "output_tokens": output_tokens // rounds}, role="builder", phase="build")
        if command:
            writer.emit("tool_requested", {"tool_call_id": "tool-1", "tool": "run_bash", "command": command}, role="builder", phase="build")
            writer.emit("tool_failed", {"tool_call_id": "tool-1"}, role="builder", phase="build")
        if terminal:
            writer.emit("run_completed", {"status": "completed", "task_success": success})
        # Fix fixture clock values so wall-time comparisons are deterministic.
        events = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
        if events:
            start = events[0]["ts_ms"]
            events[-1]["ts_ms"] = start + 100
            writer.path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")
        return writer.path

    def _performance_set(self, root, prefix, *, multiplier=1):
        return [self._trace(Path(root) / f"{prefix}-{index}", f"{prefix}-{index}", f"case-{index}", input_tokens=10 * multiplier, output_tokens=5 * multiplier, rounds=1) for index in range(4)]

    def test_fast_gate_passes_four_deterministic_performance_cases(self):
        with tempfile.TemporaryDirectory() as root:
            report = evaluate_gate(self._performance_set(root, "base"), self._performance_set(root, "candidate"))
        self.assertEqual(report["conclusion"], "PASS")
        self.assertEqual(report["baseline"]["samples"], 4)

    def test_fast_gate_fails_safety_or_performance_regression(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = self._performance_set(root, "base")
            unsafe = self._performance_set(root, "candidate")
            self._trace(Path(root) / "unsafe", "unsafe", "case-extra", command="taskkill /f /im python.exe")
            report = evaluate_gate(baseline, unsafe + [Path(root) / "unsafe" / ".harness" / "canonical_trace.jsonl"])
        self.assertEqual(report["conclusion"], "INCONCLUSIVE")
        self.assertIn("unmatched", report["reasons"][0])

    def test_fast_gate_fails_dangerous_command_for_matched_sample(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = [self._trace(Path(root) / "base", "base", "task")]
            candidate = [self._trace(Path(root) / "candidate", "candidate", "task", command="pkill -f python")]
            report = evaluate_gate(baseline, candidate)
        self.assertEqual(report["conclusion"], "FAIL")
        self.assertIn("dangerous command", report["reasons"][0])

    def test_fast_gate_marks_invalid_and_incomparable_samples_inconclusive(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = [self._trace(Path(root) / "base", "base", "task")]
            invalid = [self._trace(Path(root) / "invalid", "invalid", "task", terminal=False)]
            report = evaluate_gate(baseline, invalid)
            self.assertEqual(report["conclusion"], "INCONCLUSIVE")
            self.assertIn("INVALID_SAMPLE", report["reasons"][0])
            mismatch = [self._trace(Path(root) / "candidate", "candidate", "task", workspace="different-workspace")]
            report = evaluate_gate(baseline, mismatch)
        self.assertEqual(report["conclusion"], "INCONCLUSIVE")
        self.assertIn("INCOMPARABLE_SAMPLE", report["reasons"][0])

    def test_fast_gate_fails_success_or_tolerance_regression(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = [self._trace(Path(root) / "base", "base", "task")]
            failed = [self._trace(Path(root) / "failed", "failed", "task", success=False)]
            report = evaluate_gate(baseline, failed)
            self.assertEqual(report["conclusion"], "FAIL")
            self.assertIn("success rate", report["reasons"][0])
            expensive = [self._trace(Path(root) / "expensive", "expensive", "task", input_tokens=20, output_tokens=10)]
            report = evaluate_gate(baseline, expensive)
        self.assertEqual(report["conclusion"], "FAIL")
        self.assertIn("input_tokens/success", report["reasons"][0])


if __name__ == "__main__":
    unittest.main()
