import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics
from orchestrator.canonical_trace import compare_replays, replay_trace, writer_for
from orchestrator.scheduler import Scheduler
from orchestrator.state import create_run_state, save_state, state_path_for_workspace


class CanonicalTraceTests(unittest.TestCase):
    def _valid_trace(self, root, run_id="run", task_id="task"):
        writer = writer_for(root, run_id)
        writer.emit("run_started", {"task_id": task_id, "model": "model", "feature_flags": {}})
        writer.emit("llm_request_started", {"llm_call_id": "llm-1", "input_tokens": 5}, role="builder", phase="build")
        writer.emit("llm_request_completed", {"llm_call_id": "llm-1", "output_tokens": 3, "latency_ms": 7}, role="builder", phase="build")
        writer.emit("tool_requested", {"tool_call_id": "tool-1"}, role="builder", phase="build")
        writer.emit("tool_completed", {"tool_call_id": "tool-1"}, role="builder", phase="build")
        writer.emit("state_changed", {"status": "completed"}, phase="complete")
        writer.emit("run_completed", {"status": "completed", "task_success": True})
        return writer.path

    def test_replay_rebuilds_timeline_and_aggregates_without_execution(self):
        with tempfile.TemporaryDirectory() as root:
            replay = replay_trace(self._valid_trace(root))
        self.assertTrue(replay["valid"])
        self.assertEqual(replay["by_role"]["builder"]["llm_calls"], 1)
        self.assertEqual(replay["by_phase"]["build"]["tool_calls"], 1)
        self.assertEqual(replay["task_success"], True)

    def test_missing_terminal_or_null_success_is_invalid(self):
        with tempfile.TemporaryDirectory() as root:
            writer = writer_for(root, "run")
            writer.emit("run_started", {"task_id": "task", "model": "m", "feature_flags": {}})
            replay = replay_trace(writer.path)
        self.assertFalse(replay["valid"])
        self.assertIn("missing or multiple terminal events", replay["invalid_reasons"])

    def test_replay_rejects_invalid_token_conservation(self):
        with tempfile.TemporaryDirectory() as root:
            writer = writer_for(root, "run")
            writer.emit("run_started", {"task_id": "task", "model": "m", "feature_flags": {}})
            writer.emit("llm_request_started", {"llm_call_id": "llm"})
            writer.emit("llm_request_completed", {"llm_call_id": "llm", "input_tokens": 1, "cached_tokens": 2, "output_tokens": 0})
            writer.emit("run_completed", {"task_success": True})
            replay = replay_trace(writer.path)
        self.assertFalse(replay["valid"])
        self.assertIn("LLM token conservation is invalid", replay["invalid_reasons"])

    def test_compare_rejects_different_tasks_and_accepts_same_task(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = replay_trace(self._valid_trace(Path(root) / "base", "base", "same"))
            candidate = replay_trace(self._valid_trace(Path(root) / "candidate", "candidate", "same"))
            mismatch = replay_trace(self._valid_trace(Path(root) / "other", "other", "other-task"))
        self.assertTrue(compare_replays(baseline, candidate)["comparable"])
        self.assertEqual(compare_replays(baseline, mismatch)["reason"], "mismatched task_id")

    def test_deterministic_fixture_replays_and_matches_metrics_totals(self):
        with tempfile.TemporaryDirectory() as root:
            writer = writer_for(root, "fixture")
            writer.emit("run_started", {"task_id": "fixture", "model": "m", "feature_flags": {}})
            writer.emit("state_changed", {"status": "running"}, phase="build")
            writer.emit("llm_request_started", {"llm_call_id": "llm-1"}, role="builder", phase="build")
            writer.emit("llm_request_completed", {"llm_call_id": "llm-1", "input_tokens": 11, "cached_tokens": 2, "output_tokens": 5, "latency_ms": 9}, role="builder", phase="build")
            writer.emit("tool_requested", {"tool_call_id": "tool-1", "tool": "write_file"}, role="builder", phase="build")
            writer.emit("safeguard_allowed", {"tool_call_id": "tool-1"}, role="builder", phase="build")
            writer.emit("tool_started", {"tool_call_id": "tool-1"}, role="builder", phase="build")
            writer.emit("workspace_mutated", {"tool": "write_file", "path": "out.txt"}, role="builder", phase="build")
            writer.emit("tool_completed", {"tool_call_id": "tool-1"}, role="builder", phase="build")
            writer.emit("managed_process_cleanup", {"run_id": "fixture", "stopped": 0})
            writer.emit("run_completed", {"status": "completed", "task_success": True})
            replay = replay_trace(writer.path)

            metrics.RECORDER.start_run("fixture", root)
            metrics.RECORDER.record_llm_call(role="builder", phase="build", model="m", latency_ms=9, usage={"prompt_tokens": 11, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 2}})
            metrics.RECORDER.record_tool_call(role="builder", phase="build", tool_name="write_file", arguments={}, latency_ms=0, result="ok")
            summary = metrics.summarize(metrics.RECORDER.data)

        self.assertTrue(replay["valid"])
        self.assertEqual(replay["totals"]["llm_calls"], summary["llm_call_count"])
        self.assertEqual(replay["totals"]["tool_calls"], summary["tool_call_count"])
        self.assertEqual(replay["totals"]["input_tokens"], summary["total_input_tokens"])
        self.assertEqual(replay["totals"]["cached_tokens"], summary["total_cached_tokens"])
        self.assertEqual(replay["totals"]["output_tokens"], summary["total_output_tokens"])

    def test_scheduler_deterministic_task_trace_replays_without_agents_or_tools(self):
        class Runner:
            def plan(self, state): return state
            def contract(self, state): return state
            def build(self, state): return state
            def evaluate(self, state):
                state["score_history"].append(10.0)
                return state

        with tempfile.TemporaryDirectory() as root:
            state = create_run_state("deterministic task", root, profile="terminal", run_id="scheduler-trace")
            path = state_path_for_workspace(root)
            save_state(path, state)
            # This fixture exercises canonical scheduler tracing without
            # emitting agent/tool verification evidence. Keep it on the
            # legacy policy; acceptance behavior has dedicated integration
            # coverage with real canonical verification events.
            with patch(
                "orchestrator.scheduler.config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER",
                False,
            ):
                completed = Scheduler(path, phase_runner=Runner()).run_until_idle(poll_interval=0)
            replay = replay_trace(Path(root) / ".harness" / "canonical_trace.jsonl")

        self.assertEqual(completed["status"], "completed")
        self.assertTrue(replay["valid"])
        self.assertTrue(replay["task_success"])
        self.assertGreaterEqual(len(replay["timeline"]), 4)
