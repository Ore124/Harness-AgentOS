import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.artifacts import LocalArtifactStore
from orchestrator.git_workspace import GitWorktreeManager
from orchestrator.run_context import RunContext
from orchestrator.runtime import (
    AgentExecutionOutcome,
    GitWorktreeAgentAdapter,
    ProfileAgentAdapter,
)
from orchestrator.sandbox import DockerExecutor, EffectRequest, SandboxPolicy


class _Secrets:
    def resolve(self, reference):
        self.reference = reference
        return "super-secret-value"


class RuntimePortTests(unittest.TestCase):
    def test_profile_adapter_tells_builder_to_chunk_initial_output(self):
        class RecordingAgent:
            time_budget = None
            task = ""

            def run(self, task, **_kwargs):
                self.task = task
                return SimpleNamespace(succeeded=True, text="done", exit_reason="no_tool_calls")

        agent = RecordingAgent()
        harness = SimpleNamespace(planner=agent, builder=agent, evaluator=agent)
        profile = SimpleNamespace(
            format_build_task=lambda *_args: "build the app",
            extract_score=lambda _text: 10,
            pass_threshold=lambda: 7,
        )
        work_item = {
            "id": "build",
            "kind": "execute",
            "agent_role": "executor",
            "budget": {"max_seconds": 300},
            "inputs": {"goal": "build the app", "profile": "app-builder"},
        }
        with tempfile.TemporaryDirectory() as root, patch(
            "harness.Harness", return_value=harness
        ), patch("profiles.get_profile", return_value=profile):
            ProfileAgentAdapter().run(
                work_item,
                None,
                RunContext("run-1", Path(root), Path(root) / "traces"),
            )

        self.assertIn("minimal index.html skeleton under 60 lines", agent.task)
        self.assertIn("separate short tool calls", agent.task)

    def test_profile_adapter_does_not_retry_exhausted_api_quota(self):
        class QuotaAgent:
            time_budget = None

            def run(self, _task, **_kwargs):
                return SimpleNamespace(succeeded=False, text="", exit_reason="api_quota")

        agent = QuotaAgent()
        harness = SimpleNamespace(planner=agent, builder=agent, evaluator=agent)
        profile = SimpleNamespace(
            format_build_task=lambda *_args: "build",
            extract_score=lambda _text: 0,
            pass_threshold=lambda: 7,
        )
        work_item = {
            "id": "build",
            "kind": "execute",
            "agent_role": "executor",
            "inputs": {"goal": "build", "profile": "app-builder"},
        }
        with tempfile.TemporaryDirectory() as root, patch(
            "harness.Harness", return_value=harness
        ), patch("profiles.get_profile", return_value=profile):
            outcome = ProfileAgentAdapter().run(
                work_item,
                None,
                RunContext("run-1", Path(root), Path(root) / "traces"),
            )

        self.assertEqual(outcome.failure_kind, "api_quota")
        self.assertFalse(outcome.retryable)

    def test_profile_adapter_uses_work_item_budget_and_scopes_diagnosis(self):
        class RecordingAgent:
            def __init__(self):
                self.time_budget = 90
                self.task = ""

            def run(self, task, **_kwargs):
                self.task = task
                return SimpleNamespace(
                    succeeded=True,
                    text="diagnosed",
                    exit_reason="completed",
                )

        agent = RecordingAgent()
        harness = SimpleNamespace(
            planner=agent,
            builder=agent,
            evaluator=agent,
        )
        profile = SimpleNamespace(
            format_build_task=lambda *_args: "build",
            extract_score=lambda _text: 10,
            pass_threshold=lambda: 7,
        )
        work_item = {
            "id": "diagnose-r2",
            "kind": "diagnose",
            "agent_role": "diagnostician",
            "budget": {"max_seconds": 360},
            "inputs": {
                "goal": "build the app",
                "profile": "app-builder",
                "failure_work_items": ["plan"],
                "failure_evidence": [
                    {
                        "work_item_id": "plan",
                        "outcome": {"failure_kind": "time_budget"},
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as root, patch(
            "harness.Harness", return_value=harness
        ), patch("profiles.get_profile", return_value=profile):
            outcome = ProfileAgentAdapter().run(
                work_item,
                {"exit_reason": "time_budget", "output_size": 104},
                RunContext("run-1", Path(root), Path(root) / "traces"),
            )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(agent.time_budget, 360)
        self.assertIn('"failure_kind": "time_budget"', agent.task)
        self.assertIn("failed work items: plan", agent.task)
        self.assertIn("Do not evaluate downstream deliverables", agent.task)
        self.assertIn("checkpoint metadata", agent.task)

    def test_local_artifacts_are_content_addressed_and_verified(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalArtifactStore(root)
            first = store.put(b"payload", {"name": "report.txt", "media_type": "text/plain"})
            second = store.put(io.BytesIO(b"payload"))
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.uri, second.uri)
            with store.open(first) as stream:
                self.assertEqual(stream.read(), b"payload")

            path = Path(first.uri.removeprefix("file:///")) if os.name == "nt" else Path(first.uri.removeprefix("file://"))
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                store.open(first)

    def test_docker_executor_defaults_to_no_network_and_hides_secret_values(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            env_index = command.index("--env-file") + 1
            captured["env_file_contents"] = Path(command[env_index]).read_text(encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="super-secret-value", stderr="")

        with tempfile.TemporaryDirectory() as root:
            executor = DockerExecutor(secret_provider=_Secrets(), runner=runner)
            result = executor.execute(
                EffectRequest(
                    "effect-1",
                    ("python", "-V"),
                    Path(root),
                    secret_refs={"API_TOKEN": "MY_SECRET_REF"},
                ),
                SandboxPolicy(),
            )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.stdout, "[redacted]")
        self.assertIn("none", captured["command"])
        self.assertNotIn("super-secret-value", " ".join(captured["command"]))
        self.assertIn("API_TOKEN=super-secret-value", captured["env_file_contents"])
        self.assertFalse(Path(captured["command"][captured["command"].index("--env-file") + 1]).exists())

    def test_docker_executor_rejects_uncontrolled_network(self):
        with tempfile.TemporaryDirectory() as root:
            executor = DockerExecutor(runner=lambda *_args, **_kwargs: None)
            effect = EffectRequest(
                "effect-1",
                ("curl", "https://example.com"),
                Path(root),
                network_hosts=("example.com",),
            )
            with self.assertRaises(PermissionError):
                executor.execute(effect, SandboxPolicy())

    def test_docker_executor_rejects_workspace_outside_approved_roots(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            executor = DockerExecutor(runner=lambda *_args, **_kwargs: None)
            with self.assertRaisesRegex(PermissionError, "writable roots"):
                executor.execute(
                    EffectRequest("effect-1", ("true",), Path(outside)),
                    SandboxPolicy(allowed_workspace_roots=(Path(root),)),
                )

    def test_docker_executor_removes_container_when_cancelled(self):
        commands = []

        class Process:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 130

            def communicate(self, timeout):
                return "", ""

        def runner(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as root:
            executor = DockerExecutor(
                runner=runner,
                process_factory=lambda *_args, **_kwargs: Process(),
            )
            result = executor.execute(
                EffectRequest(
                    "cancel-effect",
                    ("python", "-V"),
                    Path(root),
                    cancel_check=lambda: True,
                ),
                SandboxPolicy(),
            )
        self.assertEqual(result.exit_code, 130)
        self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in commands))

    def test_docker_executor_forwards_checkpoint_journal_entries(self):
        class Process:
            returncode = 0

            def poll(self):
                return 0

            def communicate(self, timeout):
                return "", ""

        checkpoints = []
        with tempfile.TemporaryDirectory() as root:
            journal = Path(root) / "checkpoints.jsonl"
            journal.write_text('{"kind":"tool_result","tool":"test"}\n', encoding="utf-8")
            executor = DockerExecutor(
                process_factory=lambda *_args, **_kwargs: Process(),
            )
            result = executor.execute(
                EffectRequest(
                    "checkpoint-effect",
                    ("python", "-V"),
                    Path(root),
                    checkpoint_path=journal,
                    checkpoint_callback=checkpoints.append,
                ),
                SandboxPolicy(),
            )
        self.assertTrue(result.succeeded)
        self.assertEqual(checkpoints, [{"kind": "tool_result", "tool": "test"}])

    def test_git_worktree_isolated_commit_integrates_serially(self):
        with tempfile.TemporaryDirectory() as root:
            repository = Path(root) / "repo"
            worktrees = Path(root) / "worktrees"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            (repository / "base.txt").write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)

            manager = GitWorktreeManager(repository, worktrees)
            workspace = manager.prepare({"run_id": "r1", "id": "build"}, "main")
            (workspace.path / "feature.txt").write_text("feature", encoding="utf-8")
            feature_commit = manager.commit(workspace, "feature")
            self.assertTrue(feature_commit)
            integrated = manager.integrate(workspace, "main")
            self.assertTrue(integrated.succeeded)
            self.assertEqual((repository / "feature.txt").read_text(encoding="utf-8"), "feature")
            manager.remove(workspace)

    def test_git_agent_adapter_executes_verifies_and_integrates_fixed_revision(self):
        class Inner:
            def run(self, work_item, checkpoint, run_context):
                if work_item["kind"] == "execute":
                    (run_context.workspace / "feature.txt").write_text("feature", encoding="utf-8")
                else:
                    (run_context.workspace / "feedback.md").write_text("verified", encoding="utf-8")
                return AgentExecutionOutcome(True, output=work_item["kind"])

        with tempfile.TemporaryDirectory() as root:
            repository = Path(root) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            (repository / "base.txt").write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)

            manager = GitWorktreeManager(repository, Path(root) / "worktrees")
            adapter = GitWorktreeAgentAdapter(Inner(), manager)
            context = RunContext("run-1", Path(root), Path(root) / "traces")
            common = {"base_revision": "main", "target_branch": "main"}
            built = adapter.run(
                {"id": "build", "kind": "execute", "dependencies": [], "inputs": common},
                None,
                context,
            )
            self.assertTrue(built.succeeded)
            commit = built.checkpoint["commit"]
            verified = adapter.run(
                {
                    "id": "verify",
                    "kind": "verify",
                    "dependencies": ["build"],
                    "inputs": {**common, "workspace_item_id": "build"},
                },
                None,
                context,
            )
            self.assertTrue(verified.succeeded)
            self.assertEqual(verified.evidence[-1]["commit"], commit)
            integrated = adapter.run(
                {
                    "id": "integrate",
                    "kind": "integrate",
                    "dependencies": ["verify"],
                    "inputs": {**common, "workspace_item_id": "build"},
                },
                None,
                context,
            )
            self.assertTrue(integrated.succeeded)
            self.assertEqual((repository / "feature.txt").read_text(encoding="utf-8"), "feature")


if __name__ == "__main__":
    unittest.main()
