import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from orchestrator.analytics import list_artifacts
from orchestrator.canonical_trace import writer_for
from orchestrator.scheduler import Scheduler
from orchestrator.state import create_run_state, load_state, save_state, state_path_for_workspace


class AcceptanceRunner:
    def __init__(
        self,
        outcome="passed",
        *,
        edit_after_verification=False,
        fail_after_verification=False,
        evaluator_score=None,
        fail_after_write_once=False,
        delegate_result_lost=False,
        replay_delegate_after_verification=False,
    ):
        self.outcome = outcome
        self.edit_after_verification = edit_after_verification
        self.fail_after_verification = fail_after_verification
        self.evaluator_score = evaluator_score
        self.fail_after_write_once = fail_after_write_once
        self.delegate_result_lost = delegate_result_lost
        self.replay_delegate_after_verification = replay_delegate_after_verification
        self.calls = []
        self.tool_sequence = 0

    def plan(self, state):
        self.calls.append("plan")
        return state

    def contract(self, state):
        self.calls.append("contract")
        return state

    def build(self, state):
        self.calls.append("build")
        workspace = Path(state["workspace"])
        writer = writer_for(workspace, state["run_id"])
        self._tool(writer, "write_file", success=True, path="app.py")
        (workspace / "app.py").write_text("alpha", encoding="utf-8")
        if self.fail_after_write_once:
            self.fail_after_write_once = False
            state["artifacts"] = {"files": list_artifacts(workspace)}
            raise RuntimeError("worker crashed after writing artifacts")
        delegate_call_id = None
        if self.delegate_result_lost:
            delegate_call_id = self._tool_requested(writer, "delegate_task")
        self._tool(
            writer,
            "run_bash",
            success=self.outcome == "passed",
            command="python -m pytest -q",
            exit_code=0 if self.outcome == "passed" else 1,
        )
        if self.delegate_result_lost:
            if self.replay_delegate_after_verification:
                self._tool_requested(writer, "delegate_task")
            else:
                self._tool_completed(
                    writer,
                    "delegate_task",
                    success=True,
                    tool_call_id=delegate_call_id,
                )
        if self.edit_after_verification:
            # Same byte length proves acceptance uses contents, not sizes.
            self._tool(writer, "edit_file", success=True, path="app.py")
            (workspace / "app.py").write_text("bravo", encoding="utf-8")
        state["evaluation_skipped"] = self.evaluator_score is None
        state["artifacts"] = {"files": list_artifacts(workspace)}
        if self.fail_after_verification:
            raise RuntimeError("API disconnected after verification")
        if self.delegate_result_lost:
            raise RuntimeError("delegate completed but result delivery was lost")
        return state

    def evaluate(self, state):
        self.calls.append("evaluate")
        if self.evaluator_score is None:
            raise AssertionError("disabled evaluator must not run")
        state.setdefault("score_history", []).append(float(self.evaluator_score))
        return state

    def _tool(self, writer, tool, *, success, command=None, path=None, exit_code=None):
        tool_call_id = self._tool_requested(writer, tool, command=command, path=path)
        self._tool_completed(
            writer,
            tool,
            success=success,
            tool_call_id=tool_call_id,
            exit_code=exit_code,
        )

    def _tool_requested(self, writer, tool, *, command=None, path=None):
        self.tool_sequence += 1
        tool_call_id = f"fake-{self.tool_sequence}"
        writer.emit(
            "tool_requested",
            {"tool_call_id": tool_call_id, "tool": tool, "command": command, "path": path},
            role="builder",
            phase="build",
        )
        return tool_call_id

    def _tool_completed(self, writer, tool, *, success, tool_call_id=None, exit_code=None):
        if tool_call_id is None:
            tool_call_id = f"fake-{self.tool_sequence}"
        writer.emit(
            "tool_completed" if success else "tool_failed",
            {
                "tool_call_id": tool_call_id,
                "tool": tool,
                "success": success,
                "exit_code": exit_code,
            },
            role="builder",
            phase="build",
        )


class AcceptanceSchedulerIntegrationTests(unittest.TestCase):
    def test_fresh_pass_skips_disabled_evaluator_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed")

            state = Scheduler(path, phase_runner=runner).run_until_idle(poll_interval=0)

            self.assertEqual(state["status"], "completed")
            self.assertEqual(runner.calls, ["build"])
            self.assertEqual(state["acceptance_decision"]["action"], "complete")
            self.assertTrue(state["validated"])

    def test_pass_followed_by_same_size_edit_is_stale_and_requests_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed", edit_after_verification=True)

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(state["acceptance_decision"]["action"], "verify")
            self.assertEqual(state["next_action"], "build")
            self.assertEqual(state["round_num"], 2)
            self.assertTrue(state["analysis"]["verification"]["latest_attempt"]["stale"])

    def test_legacy_policy_can_false_complete_after_post_verification_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed", edit_after_verification=True)

            with patch("orchestrator.scheduler.config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER", False):
                state = Scheduler(path, phase_runner=runner).run_until_idle(poll_interval=0)

            self.assertEqual(state["status"], "completed")
            self.assertEqual(
                state["analysis"]["verification"]["latest_attempt"]["stale"],
                True,
            )

    def test_failed_verification_requests_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("failed")

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(state["acceptance_decision"]["action"], "repair")
            self.assertEqual(state["next_action"], "build")
            self.assertEqual(state["round_num"], 2)
            self.assertIn("current_failure_evidence", state)

    def test_enabled_evaluator_repairs_failed_build_verification_before_evaluating(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp, evaluator_enabled=True)
            runner = AcceptanceRunner("failed", evaluator_score=9.0)

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(runner.calls, ["build"])
            self.assertEqual(state["acceptance_decision"]["action"], "repair")
            self.assertEqual(state["next_action"], "build")

    def test_low_evaluator_score_becomes_current_acceptance_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp, evaluator_enabled=True)
            runner = AcceptanceRunner("passed", evaluator_score=4.0)
            scheduler = Scheduler(path, phase_runner=runner)

            after_build = scheduler.step_once()
            after_evaluate = scheduler.step_once()

            self.assertEqual(after_build["next_action"], "evaluate")
            self.assertEqual(after_evaluate["acceptance_decision"]["action"], "repair")
            self.assertIn("score:round-1", after_evaluate["acceptance_progress"]["last_verification"]["token"])
            self.assertEqual(after_evaluate["next_action"], "build")

    def test_exception_after_fresh_pass_is_salvaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed", fail_after_verification=True)

            salvaged = Scheduler(path, phase_runner=runner).step_once()
            completed = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(salvaged["next_action"], "analyze")
            self.assertEqual(salvaged["acceptance_decision"]["action"], "complete")
            self.assertTrue(any(event["type"] == "acceptance_salvaged" for event in salvaged["events"]))
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["acceptance_summary"]["verification_attempts"], 1)
            self.assertEqual(completed["acceptance_summary"]["final_accepted_revision"], 1)
            analysis = json.loads((Path(tmp) / "analysis.json").read_text(encoding="utf-8"))
            metrics_payload = json.loads(
                (Path(tmp) / ".harness" / "metrics.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis["acceptance"], completed["acceptance_summary"])
            self.assertEqual(metrics_payload["acceptance"], completed["acceptance_summary"])

    def test_crash_after_write_resumes_without_double_counting_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed", fail_after_write_once=True)
            scheduler = Scheduler(path, phase_runner=runner)

            crashed = scheduler.step_once()
            replayed_repair = scheduler._record_acceptance_repair_round(load_state(path))
            completed = scheduler.run_until_idle(poll_interval=0)

            self.assertEqual(crashed["acceptance_decision"]["action"], "repair")
            self.assertEqual(replayed_repair["acceptance_repair_rounds"], 1)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["acceptance_progress"]["artifact_revision"], 1)
            self.assertEqual(completed["acceptance_progress"]["verified_revision"], 1)
            self.assertEqual(completed["acceptance_summary"]["repair_rounds"], 1)

    def test_saved_acceptance_before_phase_transition_resumes_without_rerunning_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed")
            scheduler = Scheduler(path, phase_runner=runner)
            accepted = scheduler._refresh_acceptance(load_state(path), context="injected_gap")
            # Inject fresh evidence, then persist the accepted controller state
            # while deliberately retaining the old build transition.
            writer = writer_for(tmp, accepted["run_id"])
            runner._tool(writer, "run_bash", success=True, command="python -m pytest -q", exit_code=0)
            accepted = scheduler._refresh_acceptance(accepted, context="before_transition")
            accepted["phase"] = "build"
            accepted["next_action"] = "build"
            save_state(path, accepted)

            completed = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(runner.calls, [])
            self.assertTrue(any(
                event["type"] == "acceptance_transition_recovered"
                for event in completed["events"]
            ))

    def test_delegate_completion_with_lost_result_salvages_fresh_nested_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed", delegate_result_lost=True)

            salvaged = Scheduler(path, phase_runner=runner).step_once()
            completed = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(salvaged["acceptance_decision"]["action"], "complete")
            self.assertEqual(salvaged["next_action"], "analyze")
            self.assertEqual(completed["status"], "completed")

    def test_replayed_delegate_dispatch_invalidates_old_nested_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner(
                "passed",
                delegate_result_lost=True,
                replay_delegate_after_verification=True,
            )

            state = Scheduler(path, phase_runner=runner).step_once()

            self.assertNotEqual(state["acceptance_decision"]["action"], "complete")
            self.assertTrue(state["analysis"]["verification"]["latest_attempt"]["stale"])

    def test_legacy_policy_cannot_salvage_exception_after_fresh_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            state = load_state(path)
            state["hook_policy"] = {"max_recovery_attempts": 0}
            save_state(path, state)
            runner = AcceptanceRunner("passed", fail_after_verification=True)

            with patch("orchestrator.scheduler.config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER", False):
                failed = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(failed["status"], "error")
            self.assertFalse(failed["active"])

    def test_acceptance_state_survives_scheduler_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed")

            first = Scheduler(path, phase_runner=runner).step_once()
            persisted = load_state(path)
            second = Scheduler(path, phase_runner=runner).step_once()

            self.assertEqual(first["acceptance_progress"], persisted["acceptance_progress"])
            self.assertEqual(first["acceptance_fingerprint"], persisted["acceptance_fingerprint"])
            self.assertGreaterEqual(second["acceptance_progress"]["observation_count"], 2)
            self.assertEqual(second["status"], "completed")

    def test_feature_flag_off_keeps_legacy_scheduler_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed")

            with patch("orchestrator.scheduler.config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER", False):
                state = Scheduler(path, phase_runner=runner).step_once()

            self.assertIsNone(state["acceptance_decision"])
            self.assertEqual(state["next_action"], "analyze")

    def test_feature_flag_off_does_not_emit_acceptance_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._state(tmp)
            runner = AcceptanceRunner("passed")

            with patch("orchestrator.scheduler.config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER", False):
                state = Scheduler(path, phase_runner=runner).run_until_idle(poll_interval=0)

            self.assertEqual(state["status"], "completed")
            self.assertNotIn("acceptance_summary", state)
            self.assertNotIn("acceptance", state["analysis"])

    @staticmethod
    def _state(tmp, evaluator_enabled=False):
        state = create_run_state("Implement and test app.py", tmp, profile="terminal", run_id="run")
        state["phase"] = "build"
        state["next_action"] = "build"
        state["max_rounds"] = 2
        state["evaluation_skipped"] = not evaluator_enabled
        state["time_allocation"] = {"evaluator_enabled": evaluator_enabled}
        path = state_path_for_workspace(tmp)
        save_state(path, state)
        return path


if __name__ == "__main__":
    unittest.main()
