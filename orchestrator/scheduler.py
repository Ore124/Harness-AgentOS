"""State-file-driven scheduler for Harness runs."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import inspect
from pathlib import Path
from typing import Any, Protocol

import config
import metrics
import tools
from orchestrator.analytics import analyze_workspace, list_artifacts
from orchestrator.canonical_trace import emit_event
from orchestrator.failure_evidence import (
    append_failure_evidence,
    build_failure_evidence,
    mark_recovery_attempt,
    render_retry_context,
)
from orchestrator.hooks import HookManager, HookResult
from orchestrator.memory import MemoryStore, VALID_PROFILES
from orchestrator.router import Router
from orchestrator.run_context import RunContext
from orchestrator.state import append_event, load_state, now_iso, save_state
from orchestrator.strategy import append_strategy_hints_to_prompt

log = logging.getLogger("harness")


class PhaseExecutionError(RuntimeError):
    """Raised when an agent terminates a phase without completing it normally."""


def _require_successful_agent_result(result: Any, phase: str) -> None:
    """Map native agent failures into Scheduler's existing recovery path.

    Older test doubles and third-party agents return plain strings, which retain
    the historic success behavior until they adopt ``AgentRunResult``.
    """
    # Import lazily: tools imports an orchestrator submodule while agents is
    # initializing, so importing AgentRunResult at module import time cycles.
    from agents import AgentRunResult

    if isinstance(result, AgentRunResult) and not result.succeeded:
        raise PhaseExecutionError(
            f"{phase} agent exited with {result.exit_reason} after "
            f"{result.iterations} iteration(s)"
        )


class PhaseRunner(Protocol):
    """Interface used by Scheduler to execute a single harness phase."""

    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        ...

    def contract(self, state: dict[str, Any]) -> dict[str, Any]:
        ...

    def build(self, state: dict[str, Any]) -> dict[str, Any]:
        ...

    def evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        ...


class HarnessPhaseRunner:
    """Runs one existing Harness phase at a time."""

    def _prepare(self, state: dict[str, Any]):
        from harness import Harness
        from profiles import get_profile

        profile_name = state["profile"]
        if profile_name not in VALID_PROFILES:
            raise ValueError(f"Cannot run phase without a concrete profile: {profile_name}")

        context = RunContext.from_state(state)
        context.workspace.mkdir(parents=True, exist_ok=True)
        self._ensure_git(context.workspace)

        profile = get_profile(profile_name)
        harness = Harness(profile)
        allocation = state.get("time_allocation") or profile.resolve_time_allocation(state["prompt"])
        return harness, profile, allocation

    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_in_context(state, self._plan)

    def _plan(self, state: dict[str, Any], context: RunContext) -> dict[str, Any]:
        harness, profile, allocation = self._prepare(state)
        state["time_allocation"] = allocation

        if harness.planner and allocation.get("planner_enabled", True):
            result = _run_agent(
                harness.planner,
                f"Create a plan for the following task:\n\n"
                f"{state['prompt']}\n\n"
                f"Save the plan to {config.SPEC_FILE}.",
                run_id=state.get("run_id"),
                phase="plan",
                run_context=context,
            )
            _require_successful_agent_result(result, "plan")
        else:
            spec_path = context.workspace / config.SPEC_FILE
            spec_path.write_text(f"# Task\n\n{state['prompt']}\n", encoding="utf-8")

        state["artifacts"] = {"files": list_artifacts(context.workspace)}
        return state

    def contract(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_in_context(state, self._contract)

    def _contract(self, state: dict[str, Any], context: RunContext) -> dict[str, Any]:
        harness, _profile, _allocation = self._prepare(state)
        if harness.contract_proposer and harness.contract_reviewer:
            harness._negotiate_contract(
                state.get("round_num", 1),
                run_id=state.get("run_id"),
                phase="contract",
                user_prompt=state.get("prompt"),
            )
        state["artifacts"] = {"files": list_artifacts(context.workspace)}
        return state

    def build(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_in_context(state, self._build)

    def _build(self, state: dict[str, Any], context: RunContext) -> dict[str, Any]:
        harness, profile, _allocation = self._prepare(state)
        total_start = _parse_start_time(state.get("created_at"))

        from middlewares import TimeBudgetMiddleware
        for middleware in harness.builder.middlewares:
            if isinstance(middleware, TimeBudgetMiddleware):
                middleware.sync_start_time(total_start)
                task_timeout = profile.resolve_task_timeout(state["prompt"])
                if task_timeout:
                    middleware.budget_seconds = task_timeout

        feedback_path = context.workspace / config.FEEDBACK_FILE
        prev_feedback = feedback_path.read_text(encoding="utf-8", errors="replace") if feedback_path.exists() else ""
        if config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
            prev_feedback = render_retry_context(state, prev_feedback)
        score_history = [float(s) for s in state.get("score_history", [])]
        build_task = profile.format_build_task(
            state["prompt"],
            int(state.get("round_num", 1)),
            prev_feedback,
            score_history,
        )
        build_task = append_strategy_hints_to_prompt(build_task, state, "builder")
        result = _run_agent(
            harness.builder,
            build_task,
            run_id=state.get("run_id"),
            phase="build",
            run_context=context,
        )
        _require_successful_agent_result(result, "build")
        state["artifacts"] = {"files": list_artifacts(context.workspace)}
        return state

    def evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_in_context(state, self._evaluate)

    def _evaluate(self, state: dict[str, Any], context: RunContext) -> dict[str, Any]:
        harness, profile, allocation = self._prepare(state)
        if not harness.evaluator or not allocation.get("evaluator_enabled", True):
            state["evaluation_skipped"] = True
            return state

        eval_task = (
            f"This is evaluation round {state.get('round_num', 1)}.\n"
            f"Read {config.SPEC_FILE} to understand the task.\n"
            f"Examine the work done and test it thoroughly.\n"
            f"Score each criterion honestly. Write your evaluation to {config.FEEDBACK_FILE}."
        )
        eval_task = append_strategy_hints_to_prompt(eval_task, state, "evaluator")
        result = _run_agent(
            harness.evaluator,
            eval_task,
            run_id=state.get("run_id"),
            phase="evaluate",
            run_context=context,
        )
        _require_successful_agent_result(result, "evaluate")
        tools.stop_dev_server()

        feedback_path = context.workspace / config.FEEDBACK_FILE
        feedback_text = feedback_path.read_text(encoding="utf-8", errors="replace") if feedback_path.exists() else ""
        score = profile.extract_score(feedback_text)
        state.setdefault("score_history", []).append(score)
        state["artifacts"] = {"files": list_artifacts(context.workspace)}
        return state

    @staticmethod
    def _run_in_context(state: dict[str, Any], operation) -> dict[str, Any]:
        context = RunContext.from_state(state)
        with context.activate():
            return operation(state, context)

    @staticmethod
    def _ensure_git(workspace: Path) -> None:
        if (workspace / ".git").exists():
            return
        try:
            subprocess.run(["git", "init"], cwd=str(workspace), capture_output=True, text=True, timeout=10)
            subprocess.run(["git", "add", "-A"], cwd=str(workspace), capture_output=True, text=True, timeout=10)
            subprocess.run(
                ["git", "commit", "-m", "init", "--allow-empty"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass


class Scheduler:
    """Advances a run by reading and writing its state file."""

    def __init__(
        self,
        state_path: str | Path,
        router: Router | None = None,
        memory: MemoryStore | None = None,
        phase_runner: PhaseRunner | None = None,
        hooks: HookManager | None = None,
    ):
        self.state_path = Path(state_path)
        self.memory = memory or MemoryStore()
        self.router = router or Router(memory=self.memory)
        self.phase_runner = phase_runner or HarnessPhaseRunner()
        self.hooks = hooks or HookManager(self.state_path)

    def step_once(self) -> dict[str, Any]:
        state = load_state(self.state_path)
        if not state.get("canonical_trace_started"):
            self._trace(state, "run_started", {"task_id": state.get("prompt"), "model": config.MODEL, "initial_workspace": state.get("workspace"), "feature_flags": _trace_flags()})
            state["canonical_trace_started"] = True
            save_state(self.state_path, state)

        if state.get("requires_human_approval") and not state.get("human_approval", {}).get("approved"):
            state = self._apply_hook_result(state, self.hooks.on_human_approval_required(state))
            save_state(self.state_path, state)
            return load_state(self.state_path)

        if not state.get("active"):
            self.hooks.on_stall(state, "active flag is false")
            return state
        if state.get("requires_confirmation"):
            self.hooks.on_stall(state, "profile confirmation required")
            return state
        if not state.get("next_action"):
            self.hooks.on_stall(state, "no next action")
            return state

        state = self._apply_hook_result(state, self.hooks.before_step(state))
        if state.get("status") in {"paused", "error", "waiting_confirmation", "waiting_approval"}:
            save_state(self.state_path, state)
            return load_state(self.state_path)

        action = state["next_action"]
        try:
            if action == "route":
                state = self._route(state)
            elif action == "plan":
                state = self._run_phase(state, "plan", self.phase_runner.plan, "planner")
                state = self._set_next_after_plan(state)
            elif action == "contract":
                state = self._run_phase(state, "contract", self.phase_runner.contract, "contract")
                state["phase"] = "build"
                state["next_action"] = "build"
            elif action == "build":
                state = self._run_phase(state, "build", self.phase_runner.build, "builder")
                state = self._set_next_after_build(state)
            elif action == "evaluate":
                state = self._run_phase(state, "evaluate", self.phase_runner.evaluate, "evaluator")
                state = self._set_next_after_evaluate(state)
            elif action == "analyze":
                state = self._analyze(state)
            else:
                raise ValueError(f"Unknown scheduler action: {action}")

            save_state(self.state_path, state)
            return load_state(self.state_path)
        except Exception as exc:
            self._cleanup_run_processes(state)
            if config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
                evidence = build_failure_evidence(
                    state,
                    phase=action,
                    error=exc,
                    retry_goal=f"Recover from {action} failure and continue the original task.",
                )
                state = append_failure_evidence(state, evidence)
                metrics.RECORDER.record_failure_evidence(evidence, recovery_attempt_planned=False)
            result = self.hooks.on_error(state, exc)
            state = self._apply_hook_result(state, result, failed_action=action)
            if result.action == "retry" and config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
                state = mark_recovery_attempt(state)
                metrics.RECORDER.record_recovery_attempt()
            if result.action == "continue":
                state["status"] = "error"
                state["active"] = False
                state["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
            if state.get("status") == "error":
                self._trace(state, "run_failed", {"status": "error", "task_success": False, "error": str(exc)})
            append_event(state, "scheduler_error", state["last_error"] or {"message": str(exc)})
            save_state(self.state_path, state)
            return load_state(self.state_path)

    def run_until_idle(self, poll_interval: float = 0.2, max_steps: int | None = None) -> dict[str, Any]:
        steps = 0
        while True:
            state = self.step_once()
            steps += 1
            if max_steps is not None and steps >= max_steps:
                return state
            if not state.get("active") or not state.get("next_action") or state.get("requires_confirmation"):
                return state
            sleep_started = time.perf_counter()
            time.sleep(poll_interval)
            metrics.RECORDER.add_latency("explicit_sleep_polling_ms", int((time.perf_counter() - sleep_started) * 1000))

    def _route(self, state: dict[str, Any]) -> dict[str, Any]:
        from_phase = state.get("phase")
        append_event(state, "before_route", {"prompt_preview": str(state.get("prompt", ""))[:200]})
        state = self._apply_hook_result(state, self.hooks.before_route(state))
        append_event(state, "before_transition", {"from": from_phase, "to": "route"})
        self.hooks.before_transition(from_phase, "route", state)
        self._trace(state, "state_changed", {"from": from_phase, "to": "route", "status": state.get("status")}, phase="route")
        decision = self.router.route(state["prompt"], state.get("profile"))
        state["route_decision"] = decision.to_dict()
        state["task_type"] = decision.task_type
        state["memory_refs"] = decision.memory_refs or []
        state["strategy_hints"] = decision.strategy_hints or []
        append_event(state, "route_decision", state["route_decision"])
        state = self._apply_hook_result(state, self.hooks.after_route(state))
        append_event(state, "after_route", state["route_decision"])

        if decision.requires_confirmation:
            state["requires_confirmation"] = True
            state["active"] = False
            state["status"] = "waiting_confirmation"
            state["next_action"] = None
            append_event(state, "after_transition", {"from": from_phase, "to": "waiting_confirmation"})
            self.hooks.after_transition(from_phase, "waiting_confirmation", state)
            return state

        state["profile"] = decision.profile
        state["requires_confirmation"] = False
        state["phase"] = "plan"
        state["next_action"] = "plan"
        self._trace(state, "state_changed", {"from": from_phase, "to": "plan", "status": state.get("status")}, phase="plan")
        append_event(state, "after_transition", {"from": from_phase, "to": "plan"})
        self.hooks.after_transition(from_phase, "plan", state)
        return state

    def _run_phase(
        self,
        state: dict[str, Any],
        phase: str,
        callback,
        agent_name: str,
    ) -> dict[str, Any]:
        from_phase = state.get("phase")
        transition_started = time.perf_counter()
        before_artifacts = list((state.get("artifacts") or {}).get("files", []))
        metrics.RECORDER.start_run(state["run_id"], state["workspace"], state.get("profile"))
        metrics.RECORDER.start_phase(phase)
        state["phase"] = phase
        state["status"] = "running"
        self._trace(state, "state_changed", {"from": from_phase, "to": phase, "status": "running"}, phase=phase)
        state["phase_started_at"] = now_iso()
        append_event(state, "phase_started", {"phase": phase, "round": state.get("round_num")})
        self._trace(state, "phase_started", {"status": "running"}, phase=phase)
        append_event(state, "before_transition", {"from": from_phase, "to": phase})
        save_state(self.state_path, state)

        state = self._apply_hook_result(state, self.hooks.before_transition(from_phase, phase, state))
        append_event(state, "before_agent_run", {"agent": agent_name, "phase": phase})
        save_state(self.state_path, state)
        state = self._apply_hook_result(state, self.hooks.before_agent_run(agent_name, state))
        metrics.RECORDER.add_latency("scheduler_state_transitions_ms", int((time.perf_counter() - transition_started) * 1000))
        callback_result = callback(load_state(self.state_path))
        _require_successful_agent_result(callback_result, phase)
        state = self._enforce_artifact_scope(callback_result, phase)
        transition_started = time.perf_counter()
        append_event(state, "after_agent_run", {"agent": agent_name, "phase": phase})
        state = self._apply_hook_result(state, self.hooks.after_agent_run(agent_name, state))
        append_event(state, "phase_completed", {"phase": phase, "round": state.get("round_num")})
        self._trace(state, "phase_completed", {"status": "running"}, phase=phase)
        append_event(state, "after_transition", {"from": from_phase, "to": phase})
        after_artifacts = list((state.get("artifacts") or {}).get("files", []))
        state = self._apply_hook_result(state, self.hooks.on_artifact_changed(before_artifacts, after_artifacts, state))
        if state.get("artifact_changes"):
            append_event(state, "artifact_changed", state["artifact_changes"])
        if phase == "evaluate" or (phase == "build" and state.get("evaluation_skipped")):
            analysis = analyze_workspace(state["workspace"])
            state["analysis"] = analysis
            state = self._apply_hook_result(state, self.hooks.after_validation(state, analysis))
            if state.get("validation"):
                append_event(state, "validation_checked", state["validation"])
        state = self._apply_hook_result(state, self.hooks.after_transition(from_phase, phase, state))
        state.pop("phase_started_at", None)
        metrics.RECORDER.record_phase_progress(phase, True)
        metrics.RECORDER.add_latency("scheduler_state_transitions_ms", int((time.perf_counter() - transition_started) * 1000))
        metrics.RECORDER.end_phase(phase)
        return state

    def _set_next_after_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        if self._contract_enabled(state):
            state["phase"] = "contract"
            state["next_action"] = "contract"
        else:
            state["phase"] = "build"
            state["next_action"] = "build"
        return state

    def _set_next_after_build(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("evaluation_skipped"):
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
        else:
            state["phase"] = "evaluate"
            state["next_action"] = "evaluate"
        return state

    def _set_next_after_evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("evaluation_skipped"):
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
            return state

        max_rounds = self._max_rounds(state)
        threshold = self._pass_threshold(state)
        score_history = state.get("score_history", [])
        score = float(score_history[-1]) if score_history else 0.0

        if score >= threshold or int(state.get("round_num", 1)) >= max_rounds:
            if score < threshold:
                if config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
                    state = self._record_round_failure_evidence(
                        state,
                        retry_planned=False,
                        retry_goal="Analyze final failure after exhausting available rounds.",
                    )
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
        else:
            if config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
                state = self._record_round_failure_evidence(
                    state,
                    retry_planned=True,
                    retry_goal="Repair the current failed checks, then rerun focused verification.",
                )
            state = self._apply_hook_result(state, self.hooks.before_new_round(state))
            if state.get("status") in {"paused", "error", "waiting_confirmation", "waiting_approval"}:
                return state
            if config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
                state = mark_recovery_attempt(state)
                metrics.RECORDER.record_recovery_attempt()
            state["round_num"] = int(state.get("round_num", 1)) + 1
            if self._contract_enabled(state):
                state["phase"] = "contract"
                state["next_action"] = "contract"
            else:
                state["phase"] = "build"
                state["next_action"] = "build"
        return state

    def _analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        self._cleanup_run_processes(state)
        append_event(state, "phase_started", {"phase": "analyze"})
        analysis = analyze_workspace(state["workspace"])
        state["analysis"] = analysis
        state["artifacts"] = {"files": list_artifacts(state["workspace"])}
        task_success = self._task_success(state)
        final_status = "completed" if task_success else "error"
        terminal_event = "run_completed" if task_success else "run_failed"
        state["phase"] = "complete"
        state["next_action"] = None
        state["status"] = final_status
        state["validated"] = task_success
        state["active"] = False
        if not task_success and not state.get("last_error"):
            state["last_error"] = {
                "type": "ValidationFailed",
                "message": "Run did not meet score and validation requirements.",
            }
        metrics.RECORDER.record_status_change(True)
        append_event(state, "phase_completed", {"phase": "analyze"})
        append_event(state, terminal_event, {"status": final_status, "task_success": task_success})
        append_event(state, "before_memory_update", {
            "profile": state.get("profile"),
            "status": state.get("status"),
            "score_history": state.get("score_history", []),
        })
        state = self._apply_hook_result(state, self.hooks.before_memory_update(state, analysis))
        if not state.get("skip_memory_update") and not state.get("memory_update_skipped") and state.get("status") != "paused":
            self.memory.record_run(state, analysis)
            state = self._apply_hook_result(state, self.hooks.after_memory_update(state, analysis))
            append_event(state, "after_memory_update", {
                "profile": state.get("profile"),
                "tool_calls": (analysis.get("tool_calls") or {}).get("total", 0),
            })
        verification_success = (state.get("validation") or {}).get("status") == "verified"
        metrics.RECORDER.record_recovery_result(task_success=task_success)
        self._trace(state, terminal_event, {"status": final_status, "task_success": task_success}, phase="complete")
        metrics.RECORDER.finish_run(
            task_success=task_success,
            verification_success=verification_success,
        )
        return state

    def _task_success(self, state: dict[str, Any]) -> bool:
        scores = state.get("score_history") or []
        score_ok = bool(scores and float(scores[-1]) >= self._pass_threshold(state))
        validation = state.get("validation") or {}
        verification_ok = validation.get("status") == "verified"
        if scores:
            return score_ok and verification_ok
        return verification_ok

    def _enforce_artifact_scope(self, state: dict[str, Any], phase: str) -> dict[str, Any]:
        if phase not in {"build", "evaluate"} or not _requires_single_html_file(state):
            return state

        workspace = Path(state["workspace"])
        removed = _remove_single_html_extras(workspace)
        if not removed:
            return state

        state["artifacts"] = {"files": list_artifacts(workspace)}
        append_event(state, "artifact_scope_enforced", {
            "phase": phase,
            "policy": "single_html_file",
            "removed": removed,
        })
        return state

    @staticmethod
    def _cleanup_run_processes(state: dict[str, Any]) -> None:
        run_id = state.get("run_id")
        if run_id:
            tools.cleanup_run_processes(str(run_id))

    @staticmethod
    def _trace(state: dict[str, Any], event_type: str, payload: dict[str, Any], *, phase: str | None = None) -> None:
        emit_event(state["workspace"], state["run_id"], event_type, payload, phase=phase or state.get("phase"))

    def _record_round_failure_evidence(
        self,
        state: dict[str, Any],
        *,
        retry_planned: bool,
        retry_goal: str,
    ) -> dict[str, Any]:
        feedback_path = Path(state["workspace"]) / config.FEEDBACK_FILE
        feedback_text = feedback_path.read_text(encoding="utf-8", errors="replace") if feedback_path.exists() else ""
        evidence = build_failure_evidence(
            state,
            phase="evaluate",
            feedback_text=feedback_text,
            retry_goal=retry_goal,
        )
        state = append_failure_evidence(state, evidence)
        metrics.RECORDER.record_failure_evidence(evidence, recovery_attempt_planned=retry_planned)
        append_event(state, "failure_evidence_recorded", {
            "failure_type": evidence.get("failure_type"),
            "failure_signature": evidence.get("failure_signature"),
            "same_failure_count": evidence.get("same_failure_count"),
            "recovery_strategy": evidence.get("recovery_strategy"),
        })
        return state

    def _apply_hook_result(
        self,
        state: dict[str, Any],
        result: HookResult | None,
        failed_action: str | None = None,
    ) -> dict[str, Any]:
        if result is None or result.action == "continue":
            if result and result.patch:
                _merge_patch(state, result.patch)
            return state

        if result.patch:
            _merge_patch(state, result.patch)
        append_event(state, "hook_action", {
            "action": result.action,
            "reason": result.reason,
        })

        if result.action == "pause":
            state["active"] = False
            state["status"] = "paused"
        elif result.action == "retry":
            state["active"] = True
            state["status"] = "running"
            state["next_action"] = failed_action or state.get("next_action") or state.get("phase")
            state["phase_started_at"] = now_iso()
        elif result.action == "fail":
            state["active"] = False
            state["status"] = "error"
            state["next_action"] = None
        elif result.action == "require_confirmation":
            state["active"] = False
            if state.get("requires_human_approval"):
                state["status"] = "waiting_approval"
            else:
                state["requires_confirmation"] = True
                state["status"] = "waiting_confirmation"
            state["next_action"] = None
        return state

    def _contract_enabled(self, state: dict[str, Any]) -> bool:
        return state.get("profile") == "app-builder"

    def _max_rounds(self, state: dict[str, Any]) -> int:
        if state.get("max_rounds"):
            return int(state["max_rounds"])
        try:
            from profiles import get_profile
            profile = get_profile(state["profile"])
            return profile.max_rounds() or config.MAX_HARNESS_ROUNDS
        except Exception:
            return config.MAX_HARNESS_ROUNDS

    def _pass_threshold(self, state: dict[str, Any]) -> float:
        try:
            from profiles import get_profile
            return float(get_profile(state["profile"]).pass_threshold())
        except Exception:
            return config.PASS_THRESHOLD


def confirm_profile(state_path: str | Path, profile: str) -> dict[str, Any]:
    if profile not in VALID_PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    state = load_state(state_path)
    state["profile"] = profile
    state["requires_confirmation"] = False
    state["active"] = True
    state["status"] = "running"
    state["phase"] = "plan"
    state["next_action"] = "plan"
    HookManager(state_path).on_profile_confirmed(state, profile)
    append_event(state, "profile_confirmed", {"profile": profile})
    save_state(state_path, state)
    return load_state(state_path)


def approve_human_action(state_path: str | Path, approved_by: str = "user") -> dict[str, Any]:
    state = load_state(state_path)
    approval = dict(state.get("human_approval") or {})
    approval["approved"] = True
    approval["approved_by"] = approved_by
    approval["approved_at"] = now_iso()
    state["human_approval"] = approval
    state["requires_human_approval"] = False
    if state.get("status") == "waiting_approval":
        state["status"] = "running"
        state["active"] = True
    append_event(state, "human_approved", approval)
    save_state(state_path, state)
    return load_state(state_path)


def set_active(state_path: str | Path, active: bool) -> dict[str, Any]:
    state = load_state(state_path)
    state["active"] = active
    if active and state.get("status") in {"paused", "waiting_confirmation"} and not state.get("requires_confirmation"):
        state["status"] = "running"
    elif not active and state.get("status") == "running":
        state["status"] = "paused"
    append_event(state, "active_changed", {"active": active})
    save_state(state_path, state)
    return load_state(state_path)


def _parse_start_time(value: str | None) -> float:
    if not value:
        return time.time()
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return time.time()


def _run_agent(
    agent,
    task: str,
    *,
    run_id: str | None,
    phase: str | None,
    run_context: RunContext | None = None,
) -> Any:
    run_method = agent.run
    signature = inspect.signature(run_method)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return run_method(task, run_id=run_id, phase=phase, run_context=run_context)
    if "run_id" in signature.parameters or "phase" in signature.parameters:
        kwargs = {}
        if "run_id" in signature.parameters:
            kwargs["run_id"] = run_id
        if "phase" in signature.parameters:
            kwargs["phase"] = phase
        if "run_context" in signature.parameters:
            kwargs["run_context"] = run_context
        return run_method(task, **kwargs)
    return run_method(task)


def _merge_patch(state: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        else:
            state[key] = value


def _trace_flags() -> dict[str, bool]:
    return {name: bool(getattr(config, name)) for name in dir(config) if name.startswith("HARNESS_") and isinstance(getattr(config, name), bool)}


def _requires_single_html_file(state: dict[str, Any]) -> bool:
    text = str(state.get("prompt") or "")
    spec = Path(str(state.get("workspace") or "")) / config.SPEC_FILE
    if spec.exists():
        try:
            text += "\n" + spec.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            pass
    return bool(re.search(r"\b(?:single|one)\s+(?:html|\.html)\s+file\b", text, re.IGNORECASE))


def _remove_single_html_extras(workspace: Path) -> list[str]:
    removed: list[str] = []

    html_files = sorted(path for path in workspace.glob("*.html") if path.is_file())
    keep_html = max(html_files, key=lambda path: path.stat().st_size, default=None)
    for path in html_files:
        if path != keep_html:
            _remove_file(path, workspace, removed)

    for path in (workspace.iterdir() if workspace.exists() else []):
        if not path.is_file():
            continue
        name = path.name.lower()
        if (
            name == "server.log"
            or name.startswith("test_") and path.suffix.lower() in {".js", ".mjs", ".cjs", ".ts"}
            or name.startswith("validation") and path.suffix.lower() in {".js", ".mjs", ".cjs", ".ts"}
            or name.endswith("-complete.md")
            or name.endswith("_complete.md")
        ):
            _remove_file(path, workspace, removed)

    return removed


def _remove_file(path: Path, workspace: Path, removed: list[str]) -> None:
    try:
        rel = path.relative_to(workspace).as_posix()
        path.unlink()
        removed.append(rel)
    except OSError:
        return
