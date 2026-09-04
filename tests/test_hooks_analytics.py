import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.analytics import (
    _LARGE_FILE_SAMPLE_BYTES,
    _update_fingerprint_with_file,
    analyze_workspace,
    workspace_artifact_fingerprint,
)
from orchestrator.canonical_trace import CanonicalTraceWriter
from orchestrator.hooks import HookManager, _validation_summary
from orchestrator.state import create_run_state, load_state, save_state, state_path_for_workspace


class HookAnalyticsTests(unittest.TestCase):
    def test_hook_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = state_path_for_workspace(tmp)
            save_state(path, create_run_state("task", tmp, profile="terminal", run_id="run"))

            HookManager(path).before_agent_run("builder", load_state(path))
            state = load_state(path)

            self.assertTrue(any(event["type"] == "before_agent_run" for event in state["events"]))

    def test_analysis_parses_trace_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "_trace_builder.jsonl"
            trace.write_text(
                json.dumps({"agent": "builder", "event": "tool_call", "tool": "run_bash", "result": "ok"}) + "\n"
                + json.dumps({"agent": "builder", "event": "error", "type": "api_error", "message": "boom"}) + "\n"
                + json.dumps({"agent": "builder", "event": "finish", "reason": "no_tool_calls"}) + "\n",
                encoding="utf-8",
            )

            analysis = analyze_workspace(tmp)

            self.assertEqual(analysis["tool_calls"]["total"], 1)
            self.assertEqual(analysis["tool_calls"]["by_tool"]["run_bash"], 1)
            self.assertEqual(analysis["finish_reasons"]["no_tool_calls"], 1)
            self.assertEqual(len(analysis["errors"]), 1)

    def test_analysis_distinguishes_commands_from_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "_trace_builder.jsonl"
            events = [
                {
                    "agent": "builder",
                    "event": "tool_call",
                    "tool": "run_bash",
                    "args": json.dumps({"command": "ls -la"}),
                    "result": "files",
                },
                {
                    "agent": "builder",
                    "event": "tool_call",
                    "tool": "run_bash",
                    "args": json.dumps({"command": "python -m pytest -q"}),
                    "result": "FAILED\n[exit code: 1]",
                },
                {
                    "agent": "builder",
                    "event": "tool_call",
                    "tool": "run_bash",
                    "args": json.dumps({"command": "python -m pytest -q"}),
                    "result": "2 passed",
                },
            ]
            trace.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            analysis = analyze_workspace(tmp)

            self.assertEqual(analysis["tool_calls"]["successful"], 2)
            self.assertEqual(analysis["tool_calls"]["failed"], 1)
            self.assertEqual(len(analysis["verification"]["attempts"]), 2)
            self.assertTrue(analysis["verification"]["latest"]["success"])

    def test_validation_does_not_keyword_scan_positive_feedback(self):
        state = {"profile": "terminal", "score_history": [8.75]}
        analysis = {
            "scores": [8.75],
            "feedback_preview": "Strong error handling; exception paths are tested.",
            "verification": {"attempts": [], "latest": None},
        }

        validation = _validation_summary(state, analysis)

        self.assertEqual(validation["status"], "verified")
        self.assertEqual(validation["evidence"], ["score"])

    def test_validation_requires_concrete_success_without_score(self):
        state = {"profile": "terminal"}
        generic_command = {
            "tool_calls": {"by_tool": {"run_bash": 1}},
            "verification": {"attempts": [], "latest": None},
        }
        failed_test = {
            "verification": {
                "attempts": [{"tool": "run_bash", "success": False}],
                "latest": {"tool": "run_bash", "success": False},
            },
        }

        self.assertEqual(
            _validation_summary(state, generic_command)["status"],
            "missing",
        )
        self.assertEqual(
            _validation_summary(state, failed_test)["status"],
            "failed",
        )

    def test_artifact_fingerprint_detects_same_size_edit_and_ignores_control_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            source.write_text("alpha", encoding="utf-8")
            first = workspace_artifact_fingerprint(root)

            source.write_text("bravo", encoding="utf-8")
            second = workspace_artifact_fingerprint(root)
            (root / "analysis.json").write_text("control", encoding="utf-8")
            (root / "_trace_builder.jsonl").write_text("trace", encoding="utf-8")
            (root / ".harness").mkdir()
            (root / ".harness" / "cache").write_text("cache", encoding="utf-8")
            third = workspace_artifact_fingerprint(root)

            self.assertNotEqual(first, second)
            self.assertEqual(second, third)

    def test_large_file_fingerprint_reads_only_bounded_head_and_tail(self):
        class TrackedFile:
            def __init__(self, size):
                self.size = size
                self.position = 0
                self.bytes_read = 0

            def read(self, size=-1):
                count = self.size - self.position if size < 0 else min(size, self.size - self.position)
                self.position += count
                self.bytes_read += count
                return b"x" * count

            def seek(self, offset):
                self.position = offset

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakePath:
            def __init__(self, handle):
                self.handle = handle

            def open(self, _mode):
                return self.handle

        size = 64 * 1024 * 1024
        handle = TrackedFile(size)

        _update_fingerprint_with_file(hashlib.sha256(), FakePath(handle), size, 123)

        self.assertEqual(handle.bytes_read, 2 * _LARGE_FILE_SAMPLE_BYTES)

    def test_canonical_verification_has_stable_token_and_becomes_stale_after_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = CanonicalTraceWriter("run", Path(tmp))
            requested = writer.emit(
                "tool_requested",
                {"tool_call_id": "verify-1", "tool": "run_bash", "command": "python -m pytest"},
                role="builder",
            )
            writer.emit(
                "tool_completed",
                {"tool_call_id": "verify-1", "tool": "run_bash", "success": True, "exit_code": 0},
                role="builder",
            )
            writer.emit(
                "tool_requested",
                {"tool_call_id": "edit-1", "tool": "edit_file", "path": "app.py"},
                role="builder",
            )
            writer.emit(
                "tool_completed",
                {"tool_call_id": "edit-1", "tool": "edit_file", "success": True},
                role="builder",
            )

            with patch("orchestrator.analytics.config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER", True):
                analysis = analyze_workspace(tmp)
            attempt = analysis["verification"]["attempts"][0]

            self.assertEqual(attempt["token"], requested["event_id"])
            self.assertEqual(attempt["ordinal"], requested["seq"])
            self.assertTrue(attempt["stale"])
            self.assertIsNone(analysis["verification"]["latest"])

    def test_writing_feedback_does_not_stale_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = CanonicalTraceWriter("run", Path(tmp))
            writer.emit(
                "tool_requested",
                {"tool_call_id": "verify-1", "tool": "run_bash", "command": "python -m pytest"},
            )
            writer.emit(
                "tool_completed",
                {"tool_call_id": "verify-1", "tool": "run_bash", "success": True},
            )
            writer.emit(
                "tool_requested",
                {"tool_call_id": "feedback", "tool": "write_file", "path": "feedback.md"},
            )
            writer.emit(
                "tool_completed",
                {"tool_call_id": "feedback", "tool": "write_file", "success": True},
            )

            analysis = analyze_workspace(tmp)

            self.assertFalse(analysis["verification"]["latest"]["stale"])


if __name__ == "__main__":
    unittest.main()
