import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_cli import _run_inline_until_terminal
from orchestrator.controller import DurableController, EvidenceRecoveryPlanner
from orchestrator.artifacts import LocalArtifactStore
from orchestrator.domain import PlanDocumentV1
from orchestrator.runtime import AgentExecutionOutcome, DurableWorker, standard_profile_plan
from orchestrator.workflow_repository import SqlWorkflowRepository


class _Adapter:
    def __init__(self):
        self.calls = []

    def run(self, work_item, checkpoint, run_context):
        self.calls.append((work_item["id"], checkpoint, run_context.run_id))
        return AgentExecutionOutcome(
            True,
            output=f"finished {work_item['id']}",
            checkpoint={"summary": f"finished {work_item['id']}"},
        )


class _FailOnceAdapter:
    def __init__(self):
        self.calls = 0

    def run(self, work_item, checkpoint, run_context):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("worker activity crashed")
        return AgentExecutionOutcome(True, output="recovered")


class _FailThreeAdapter:
    def __init__(self):
        self.calls = []

    def run(self, work_item, checkpoint, run_context):
        self.calls.append(work_item["id"])
        if len(self.calls) <= 3:
            return AgentExecutionOutcome(
                False,
                failure_kind="same_test_failure",
                retryable=True,
            )
        return AgentExecutionOutcome(True, output="recovered")


class _CancellationAwareAdapter:
    def __init__(self):
        self.started = threading.Event()

    def run(self, work_item, checkpoint, run_context):
        self.started.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if run_context.cancel_check and run_context.cancel_check():
                return AgentExecutionOutcome(
                    False,
                    output="cancelled",
                    retryable=False,
                    failure_kind="cancelled",
                )
            time.sleep(0.01)
        raise AssertionError("worker did not observe cancellation")


class _CheckpointingAdapter:
    def run(self, work_item, checkpoint, run_context):
        run_context.checkpoint_callback({"kind": "tool_result", "tool": "one"})
        run_context.checkpoint_callback({"kind": "tool_result", "tool": "two"})
        return AgentExecutionOutcome(True, checkpoint={"kind": "agent_complete"})


class _ArtifactAdapter:
    def run(self, work_item, checkpoint, run_context):
        (run_context.workspace / "report.txt").write_text("evidence", encoding="utf-8")
        return AgentExecutionOutcome(
            True,
            artifacts=({"path": "report.txt", "name": "report.txt"},),
        )


class _CaptureRecoveryEvidenceAdapter:
    def __init__(self):
        self.calls = []
        self.diagnosis_inputs = None

    def run(self, work_item, checkpoint, run_context):
        self.calls.append(work_item["id"])
        if work_item["id"] == "plan":
            return AgentExecutionOutcome(
                False,
                failure_kind="time_budget",
                retryable=True,
            )
        if work_item["id"].startswith("diagnose-"):
            self.diagnosis_inputs = dict(work_item.get("inputs") or {})
        return AgentExecutionOutcome(True, output="recovered")


class _InterruptAfterCancellationAdapter:
    def __init__(self, controller):
        self.controller = controller

    def run(self, work_item, checkpoint, run_context):
        self.controller.change_status(run_context.run_id, "cancelled", actor="test")
        raise KeyboardInterrupt


class DurableWorkerTests(unittest.TestCase):
    def setUp(self):
        self.repository = SqlWorkflowRepository("sqlite://")
        self.controller = DurableController(self.repository)

    def tearDown(self):
        self.repository.close()

    def _create(self, run_id="r1"):
        state = self.controller.create_run(
            "p1",
            "ship",
            run_id=run_id,
            policy={"workspace": tempfile.gettempdir()},
        )
        self.controller.commit_plan(
            run_id,
            standard_profile_plan("ship", "terminal"),
            expected_version=state["version"],
        )
        self.controller.reconcile(run_id)

    def test_worker_runs_dag_to_verified_completion(self):
        self._create()
        adapter = _Adapter()
        worker = DurableWorker(
            "w1",
            self.repository,
            adapter,
            capabilities={"local"},
            controller=self.controller,
            heartbeat_seconds=0.01,
        )
        for _ in range(3):
            self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())

        snapshot = self.repository.get_snapshot("r1")
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual([call[0] for call in adapter.calls], ["plan", "build", "verify"])
        self.assertEqual(snapshot["verified_revision"], 1)

    def test_inline_worker_claims_only_its_target_run(self):
        self._create("r1")
        self._create("r2")
        adapter = _Adapter()
        worker = DurableWorker(
            "inline",
            self.repository,
            adapter,
            capabilities={"local"},
            controller=self.controller,
            target_run_id="r2",
        )

        self.assertTrue(worker.run_once())
        self.assertEqual(adapter.calls[0][2], "r2")
        self.assertEqual(
            self.repository.get_snapshot("r1")["work_item_statuses"]["plan"],
            "ready",
        )

    def test_worker_exception_is_retried_and_recovered(self):
        self._create()
        adapter = _FailOnceAdapter()
        worker = DurableWorker(
            "w1",
            self.repository,
            adapter,
            capabilities={"local"},
            controller=self.controller,
        )
        self.assertTrue(worker.run_once())
        failed = self.repository.get_snapshot("r1")
        self.assertEqual(failed["work_item_statuses"]["plan"], "ready")
        self.assertTrue(worker.run_once())
        recovered = self.repository.get_snapshot("r1")
        self.assertEqual(recovered["work_item_statuses"]["plan"], "succeeded")

    def test_repeated_failure_creates_diagnose_repair_verify_revision(self):
        self.controller = DurableController(
            self.repository, planner=EvidenceRecoveryPlanner()
        )
        self._create()
        adapter = _FailThreeAdapter()
        worker = DurableWorker(
            "w1",
            self.repository,
            adapter,
            capabilities={"local"},
            controller=self.controller,
        )
        for _ in range(6):
            self.assertTrue(worker.run_once())
        snapshot = self.repository.get_snapshot("r1")
        self.assertEqual(snapshot["current_plan_revision"], 2)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(
            adapter.calls,
            [
                "plan",
                "plan",
                "plan",
                "diagnose-r2",
                "repair-r2",
                "verify-r2",
            ],
        )
        self.assertEqual(
            snapshot["plan"]["work_items"][0]["inputs"]["recovery_strategy"],
            "escalate_analysis",
        )

    def test_diagnosis_receives_evidence_from_the_failed_work_items(self):
        self.controller = DurableController(
            self.repository, planner=EvidenceRecoveryPlanner()
        )
        self._create()
        adapter = _CaptureRecoveryEvidenceAdapter()
        worker = DurableWorker(
            "w1",
            self.repository,
            adapter,
            capabilities={"local"},
            controller=self.controller,
        )
        for _ in range(6):
            self.assertTrue(worker.run_once())

        evidence = adapter.diagnosis_inputs["failure_evidence"]
        self.assertEqual(len(evidence), 3)
        self.assertEqual({item["work_item_id"] for item in evidence}, {"plan"})
        self.assertTrue(
            all(item["outcome"]["failure_kind"] == "time_budget" for item in evidence)
        )

    def test_inline_runner_waits_when_retry_backoff_temporarily_has_no_work(self):
        class Repository:
            status = "running"

            def get_snapshot(self, _run_id):
                return {"status": self.status}

        class Worker:
            calls = 0

            def run_once(self):
                self.calls += 1
                if self.calls == 2:
                    repository.status = "completed"
                    return True
                return False

        repository = Repository()
        worker = Worker()
        with patch("durable_cli.time.sleep") as sleep:
            state = _run_inline_until_terminal(repository, worker, "r1", 0.01)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(worker.calls, 2)
        sleep.assert_called_once_with(0.01)

    def test_recovery_plan_preserves_execution_capability(self):
        repository = SqlWorkflowRepository("sqlite+pysqlite:///:memory:")
        controller = DurableController(repository, planner=EvidenceRecoveryPlanner())
        state = controller.create_run(
            "project",
            "recover in docker",
            policy={
                "profile": "app-builder",
                "max_total_seconds": 100,
                "execution_capability": "docker",
            },
        )
        controller.commit_plan(
            state["run_id"],
            standard_profile_plan(
                "recover in docker",
                "app-builder",
                max_seconds=100,
                execution_capability="docker",
            ),
        )
        snapshot = repository.get_snapshot(state["run_id"])
        patch = EvidenceRecoveryPlanner().replan(
            PlanDocumentV1.from_dict(snapshot["plan"]),
            {**snapshot, "work_item_statuses": {"build": "failed"}},
            ({"kind": "execution_failure"},),
        )
        capabilities = {
            capability
            for item in patch.add
            for capability in item.required_capabilities
        }
        self.assertEqual(capabilities, {"docker"})

    def test_cancelled_run_stops_active_attempt_and_preserves_cancelled_work(self):
        self._create()
        adapter = _CancellationAwareAdapter()
        worker = DurableWorker(
            "w1",
            self.repository,
            adapter,
            capabilities={"local"},
            controller=self.controller,
            heartbeat_seconds=0.01,
        )
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        self.assertTrue(adapter.started.wait(1))
        self.controller.change_status("r1", "cancelled", actor="test")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        snapshot = self.repository.get_snapshot("r1")
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(set(snapshot["work_item_statuses"].values()), {"cancelled"})
        self.assertEqual(snapshot["current_attempts"], {})

    def test_keyboard_interrupt_finishes_cancelled_attempt_before_exiting(self):
        self._create()
        worker = DurableWorker(
            "w1",
            self.repository,
            _InterruptAfterCancellationAdapter(self.controller),
            capabilities={"local"},
            controller=self.controller,
        )
        with self.assertRaises(KeyboardInterrupt):
            worker.run_once()
        snapshot = self.repository.get_snapshot("r1")
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(snapshot["current_attempts"], {})

    def test_tool_and_agent_checkpoints_are_monotonic_within_attempt(self):
        self._create()
        worker = DurableWorker(
            "w1",
            self.repository,
            _CheckpointingAdapter(),
            capabilities={"local"},
            controller=self.controller,
        )
        self.assertTrue(worker.run_once())
        checkpoint = self.repository.get_snapshot("r1")["latest_checkpoints"]["plan"]
        self.assertEqual(checkpoint["sequence"], 3)
        self.assertEqual(checkpoint["checkpoint"]["kind"], "agent_complete")

    def test_declared_artifact_is_uploaded_and_registered(self):
        with tempfile.TemporaryDirectory() as root:
            state = self.controller.create_run(
                "p1",
                "ship",
                run_id="artifact-run",
                policy={"workspace": str(Path(root) / "workspace")},
            )
            self.controller.commit_plan(
                "artifact-run",
                standard_profile_plan("ship", "app-builder"),
                expected_version=state["version"],
            )
            self.controller.reconcile("artifact-run")
            worker = DurableWorker(
                "w1",
                self.repository,
                _ArtifactAdapter(),
                capabilities={"local"},
                controller=self.controller,
                artifact_store=LocalArtifactStore(Path(root) / "artifacts"),
            )
            self.assertTrue(worker.run_once())
            snapshot = self.repository.get_snapshot("artifact-run")
        self.assertEqual(len(snapshot["artifacts"]), 1)
        self.assertEqual(snapshot["artifacts"][0]["name"], "report.txt")


if __name__ == "__main__":
    unittest.main()
