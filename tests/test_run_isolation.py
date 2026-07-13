import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import TraceWriter
import context
from orchestrator.run_context import RunContext
from profiles.terminal import TerminalProfile
import tools


class RunIsolationTests(unittest.TestCase):
    def test_nested_run_contexts_keep_files_and_traces_in_their_workspaces(self):
        import config

        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "first"
            second = Path(root) / "second"
            first_context = RunContext(run_id="one", workspace=first, trace_dir=first / ".harness" / "traces")
            second_context = RunContext(run_id="two", workspace=second, trace_dir=second / ".harness" / "traces")

            with first_context.activate():
                self.assertEqual(tools.current_workspace(), first.resolve())
                tools.write_file("marker.txt", "one")
                TraceWriter("first").finish("done", 1)
                with second_context.activate():
                    self.assertEqual(tools.current_workspace(), second.resolve())
                    tools.write_file("marker.txt", "two")
                    TraceWriter("second").finish("done", 1)
                tools.write_file("first-after-second.txt", "one")

            self.assertEqual(tools.current_workspace(), Path(config.WORKSPACE).resolve())
            self.assertEqual((first / "marker.txt").read_text(encoding="utf-8"), "one")
            self.assertEqual((second / "marker.txt").read_text(encoding="utf-8"), "two")
            self.assertEqual((first / "first-after-second.txt").read_text(encoding="utf-8"), "one")
            self.assertTrue((first / "_trace_first.jsonl").exists())
            self.assertFalse((first / "_trace_second.jsonl").exists())
            self.assertTrue((second / "_trace_second.jsonl").exists())
            self.assertFalse((second / "_trace_first.jsonl").exists())

    def test_context_and_terminal_profile_use_the_active_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "target-task"
            run_context = RunContext(
                run_id="one",
                workspace=workspace,
                trace_dir=workspace / ".harness" / "traces",
            )
            workspace.mkdir()
            profile = TerminalProfile()

            with run_context.activate(), \
                 patch.object(profile, "_load_tb2_tasks", return_value={"target-task": {"agent_timeout_sec": 42}}), \
                 patch("context.subprocess.run") as run:
                run.return_value.stdout = "recent changes"
                run.return_value.stderr = ""
                context.create_checkpoint([], lambda _messages: "checkpoint")
                context.restore_from_checkpoint("checkpoint", "system")

                self.assertEqual(profile.resolve_task_timeout("unrelated"), 42)
                self.assertEqual(run.call_args.kwargs["cwd"], workspace.resolve())

            self.assertEqual((workspace / "progress.md").read_text(encoding="utf-8"), "checkpoint")
