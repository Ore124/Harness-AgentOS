"""State-file-driven scheduler for Harness runs."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import inspect
import math
from pathlib import Path
from typing import Any, Protocol

import config
import metrics
import tools
from orchestrator.acceptance_progress import (
    AcceptanceObservation,
    AcceptanceProgressController,
    build_acceptance_summary,
    create_acceptance_progress_state,
)
from orchestrator.analytics import (
    analyze_workspace,
    list_artifacts,
    persist_analysis,
    workspace_artifact_fingerprint,
)
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
        task_id = state.get("task_id")
        allocation = state.get("time_allocation") or _resolve_profile_value(
            profile,
            "resolve_time_allocation",
            state["prompt"],
            task_id,
        )
        task_budget = _resolve_profile_value(
            profile,
            "resolve_task_budget",
            state["prompt"],
            task_id,
        )
        state["time_allocation"] = allocation
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            state["evaluation_skipped"] = bool(
                not harness.evaluator or not allocation.get("evaluator_enabled", True)
            )
        if task_budget is not None:
            task_budget = float(task_budget)
            if not math.isfinite(task_budget) or task_budget <= 0:
                raise ValueError(f"Invalid task budget: {task_budget!r}")
            state["task_budget_seconds"] = task_budget
            elapsed = max(0.0, time.time() - _parse_start_time(state.get("created_at")))
            remaining_budget = max(0.0, task_budget - elapsed)
            state["remaining_task_budget_seconds"] = remaining_budget
            state["phase_budgets"] = _apply_phase_budgets(
                harness,
                allocation,
                task_budget,
                remaining_budget=remaining_budget,
                current_phase=str(state.get("phase") or state.get("next_action") or ""),
            )
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
                task_budget = state.get("task_budget_seconds")
                if task_budget:
                    middleware.budget_seconds = float(task_budget)

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
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            acceptance_action = (state.get("acceptance_decision") or {}).get("action")
            if acceptance_action == "verify":
                build_task += (
                    "\n\n[ACCEPTANCE] The current artifacts have no fresh passing verification. "
                    "Run the focused project tests now. Avoid modifying accepted behavior unless a test fails."
                )
            elif acceptance_action == "repair":
                build_task += (
                    "\n\n[ACCEPTANCE] The latest verification failed. Repair that failure, then rerun "
                    "the focused verification before finishing."
                )
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
            self._trace(state, "run_started", {"task_id": state.get("task_id") or state.get("prompt"), "model": config.MODEL, "initial_workspace": state.get("workspace"), "feature_flags": _trace_flags()})
            state["canonical_trace_started"] = True
            save_state(self.state_path, state)

        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            state = self._sync_acceptance_baseline(state)
            state = self._reconcile_durable_acceptance_transition(state)

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
            if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
                try:
                    state = self._refresh_acceptance(
                        state,
                        context=f"{action}_error",
                        recent_failure=f"{type(exc).__name__}: {exc}",
                    )
                    if (state.get("acceptance_decision") or {}).get("action") == "complete":
                        state["phase"] = "analyze"
                        state["next_action"] = "analyze"
                        state["status"] = "running"
                        state["active"] = True
                        append_event(state, "acceptance_salvaged", {
                            "failed_action": action,
                            "error_type": type(exc).__name__,
                            "verification_token": _latest_verification_token(state.get("analysis") or {}),
                        })
                        save_state(self.state_path, state)
                        return load_state(self.state_path)
                except Exception as acceptance_error:
                    append_event(state, "acceptance_refresh_failed", {
                        "failed_action": action,
                        "message": str(acceptance_error),
                    })
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
            if (
                config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER
                and result.action == "retry"
                and (state.get("acceptance_decision") or {}).get("action") == "repair"
            ):
                state = self._record_acceptance_repair_round(state)
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
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            state = self._sync_acceptance_baseline(state)
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
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER and phase in {"build", "evaluate"}:
            state = self._refresh_acceptance(state, context=f"{phase}_complete")
        elif phase == "evaluate" or (phase == "build" and state.get("evaluation_skipped")):
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
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            if not state.get("evaluation_skipped"):
                action = (state.get("acceptance_decision") or {}).get("action")
                if action == "repair":
                    return self._schedule_acceptance_retry(state, "repair")
                if action == "blocked":
                    state["phase"] = "analyze"
                    state["next_action"] = "analyze"
                    return state
                state["phase"] = "evaluate"
                state["next_action"] = "evaluate"
                return state
            return self._set_next_without_evaluator(state)

        if state.get("evaluation_skipped"):
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
        else:
            state["phase"] = "evaluate"
            state["next_action"] = "evaluate"
        return state

    def _set_next_after_evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            return self._set_next_after_acceptance_evaluate(state)

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

    def _set_next_without_evaluator(self, state: dict[str, Any]) -> dict[str, Any]:
        """Drive terminal/single-agent tasks directly from acceptance evidence."""
        action = (state.get("acceptance_decision") or {}).get("action")
        if action == "complete" or action == "blocked":
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
            return state
        return self._schedule_acceptance_retry(state, action or "verify")

    def _set_next_after_acceptance_evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        """Keep evaluator score policy while requiring fresh verification."""
        scores = state.get("score_history") or []
        score = float(scores[-1]) if scores else 0.0
        score_ok = bool(scores and score >= self._pass_threshold(state))
        if not score_ok:
            state = self._record_score_acceptance_failure(state, score)
        action = (state.get("acceptance_decision") or {}).get("action")
        if action == "complete" and score_ok:
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
            return state
        if action == "blocked" or int(state.get("round_num", 1)) >= self._max_rounds(state):
            if not score_ok and config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
                state = self._record_round_failure_evidence(
                    state,
                    retry_planned=False,
                    retry_goal="Analyze final failure after exhausting available rounds.",
                )
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
            return state
        retry_action = "repair" if not score_ok else (action or "repair")
        return self._schedule_acceptance_retry(state, retry_action)

    def _record_score_acceptance_failure(
        self,
        state: dict[str, Any],
        score: float,
    ) -> dict[str, Any]:
        """Make a low evaluator score durable current acceptance evidence."""
        threshold = self._pass_threshold(state)
        sequence = int(state.get("acceptance_observation_seq", 0)) + 1
        scores = state.get("score_history") or []
        score_token = (
            f"score:round-{int(state.get('round_num', 1))}:"
            f"sample-{len(scores)}:{score:.6g}:threshold-{threshold:.6g}"
        )
        observation = AcceptanceObservation(
            observation_id=f"{state.get('run_id', 'run')}:acceptance:{sequence}",
            artifacts_changed=False,
            verification_status="failed",
            verification_token=score_token,
            recent_failure=f"evaluator score {score:g} is below threshold {threshold:g}",
            remaining_budget_seconds=(
                max(0.0, float(state["remaining_task_budget_seconds"]))
                if state.get("remaining_task_budget_seconds") is not None
                else None
            ),
        )
        policy = state.get("hook_policy") or {}
        progress, decision = AcceptanceProgressController(
            no_progress_limit=max(1, int(policy.get("acceptance_no_progress_limit", 3))),
        ).decide(state.get("acceptance_progress"), observation)
        state["acceptance_progress"] = progress
        state["acceptance_decision"] = decision.to_dict()
        state["acceptance_observation_seq"] = sequence
        append_event(state, "acceptance_score_failed", {
            **decision.to_dict(),
            "score": score,
            "threshold": threshold,
            "verification_token": score_token,
        })
        return state

    def _schedule_acceptance_retry(self, state: dict[str, Any], action: str) -> dict[str, Any]:
        if int(state.get("round_num", 1)) >= self._max_rounds(state):
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
            return state

        if action == "repair" and config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
            state = self._record_round_failure_evidence(
                state,
                retry_planned=True,
                retry_goal="Repair the failed acceptance check, then rerun focused verification.",
            )
        state = self._apply_hook_result(state, self.hooks.before_new_round(state))
        if state.get("status") in {"paused", "error", "waiting_confirmation", "waiting_approval"}:
            return state
        if action == "repair" and config.HARNESS_EVIDENCE_GUIDED_RECOVERY:
            state = mark_recovery_attempt(state)
            metrics.RECORDER.record_recovery_attempt()
        state["round_num"] = int(state.get("round_num", 1)) + 1
        if action == "repair":
            state = self._record_acceptance_repair_round(state)
        if self._contract_enabled(state):
            state["phase"] = "contract"
            state["next_action"] = "contract"
        else:
            state["phase"] = "build"
            state["next_action"] = "build"
        append_event(state, "acceptance_retry_scheduled", {
            "decision": action,
            "round": state["round_num"],
        })
        return state

    def _analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            # A restarted process can resume directly at analyze without an
            # in-memory recorder initialized by a preceding build phase.
            metrics.RECORDER.start_run(state["run_id"], state["workspace"], state.get("profile"))
        self._cleanup_run_processes(state)
        append_event(state, "phase_started", {"phase": "analyze"})
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            state = self._refresh_acceptance(state, context="analyze")
            analysis = state["analysis"]
        else:
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
        if task_success:
            state["last_error"] = None
        if not task_success and not state.get("last_error"):
            state["last_error"] = {
                "type": "ValidationFailed",
                "message": "Run did not meet score and validation requirements.",
            }
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            acceptance_summary = build_acceptance_summary(
                state.get("acceptance_progress"),
                analysis,
                state.get("acceptance_decision"),
                repair_rounds=int(state.get("acceptance_repair_rounds", 0)),
            )
            state["acceptance_summary"] = acceptance_summary
            analysis["acceptance"] = acceptance_summary
            persist_analysis(state["workspace"], analysis)
            if metrics.RECORDER.data:
                metrics.RECORDER.data["acceptance"] = dict(acceptance_summary)
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
        if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER:
            acceptance_ok = (state.get("acceptance_decision") or {}).get("action") == "complete"
            if not acceptance_ok:
                return False
            if self._acceptance_evaluator_required(state):
                scores = state.get("score_history") or []
                return bool(scores and float(scores[-1]) >= self._pass_threshold(state))
            return True

        scores = state.get("score_history") or []
        score_ok = bool(scores and float(scores[-1]) >= self._pass_threshold(state))
        validation = state.get("validation") or {}
        verification_ok = validation.get("status") == "verified"
        if scores:
            return score_ok and verification_ok
        return verification_ok

    def _sync_acceptance_baseline(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("acceptance_progress") is None:
            state["acceptance_progress"] = create_acceptance_progress_state()
        if state.get("acceptance_fingerprint") is None:
            state["acceptance_fingerprint"] = workspace_artifact_fingerprint(state["workspace"])
        state.setdefault("acceptance_observation_seq", 0)
        state.setdefault("acceptance_repair_rounds", 0)
        state.setdefault("acceptance_repair_tokens", [])
        state.setdefault("acceptance_summary", None)
        return state

    def _record_acceptance_repair_round(self, state: dict[str, Any]) -> dict[str, Any]:
        """Count a scheduled repair once across save/retry replay boundaries."""
        progress = state.get("acceptance_progress") or {}
        token = str(
            progress.get("last_observation_id")
            or f"round:{state.get('round_num', 1)}:{(state.get('acceptance_decision') or {}).get('reason', '')}"
        )
        tokens = list(state.get("acceptance_repair_tokens") or [])
        if token not in tokens:
            tokens.append(token)
            state["acceptance_repair_tokens"] = tokens[-500:]
            state["acceptance_repair_rounds"] = int(
                state.get("acceptance_repair_rounds", 0)
            ) + 1
        return state

    def _reconcile_durable_acceptance_transition(self, state: dict[str, Any]) -> dict[str, Any]:
        """Resume a transition whose accepted state was saved before its phase change.

        Only evaluator-disabled runs are advanced here.  A fingerprint mismatch
        deliberately leaves the build pending so a later refresh invalidates
        the old verification instead of accepting changed artifacts.
        """
        decision = state.get("acceptance_decision") or {}
        progress = state.get("acceptance_progress") or {}
        if (
            not state.get("active")
            or state.get("next_action") != "build"
            or not state.get("evaluation_skipped")
            or decision.get("action") != "complete"
            or progress.get("verified_revision") != progress.get("artifact_revision")
        ):
            return state
        if workspace_artifact_fingerprint(state["workspace"]) != state.get("acceptance_fingerprint"):
            return state
        state["phase"] = "analyze"
        state["next_action"] = "analyze"
        append_event(state, "acceptance_transition_recovered", {
            "artifact_revision": progress.get("artifact_revision"),
            "verification_token": (progress.get("last_verification") or {}).get("token"),
        })
        return state

    def _refresh_acceptance(
        self,
        state: dict[str, Any],
        *,
        context: str,
        recent_failure: str | None = None,
    ) -> dict[str, Any]:
        """Refresh validation and advance the durable acceptance state once."""
        state = self._sync_acceptance_baseline(state)
        analysis = analyze_workspace(state["workspace"])
        state["analysis"] = analysis
        state = self._apply_hook_result(state, self.hooks.after_validation(state, analysis))
        if state.get("validation"):
            append_event(state, "validation_checked", state["validation"])

        previous_fingerprint = state.get("acceptance_fingerprint")
        current_fingerprint = workspace_artifact_fingerprint(state["workspace"])
        artifacts_changed = bool(
            previous_fingerprint is not None
            and previous_fingerprint != current_fingerprint
        )
        if artifacts_changed:
            append_event(state, "acceptance_artifacts_changed", {
                "before": previous_fingerprint,
                "after": current_fingerprint,
                "context": context,
            })

        latest = (analysis.get("verification") or {}).get("latest")
        if latest:
            verification_status = "passed" if latest.get("success") else "failed"
            verification_token = str(latest.get("token") or "") or None
        else:
            verification_status = "missing"
            verification_token = None

        remaining_budget = state.get("remaining_task_budget_seconds")
        if remaining_budget is not None:
            remaining_budget = max(0.0, float(remaining_budget))
        sequence = int(state.get("acceptance_observation_seq", 0)) + 1
        observation = AcceptanceObservation(
            observation_id=f"{state.get('run_id', 'run')}:acceptance:{sequence}",
            artifacts_changed=artifacts_changed,
            verification_status=verification_status,
            verification_token=verification_token,
            recent_failure=recent_failure,
            remaining_budget_seconds=remaining_budget,
        )
        policy = state.get("hook_policy") or {}
        controller = AcceptanceProgressController(
            no_progress_limit=max(1, int(policy.get("acceptance_no_progress_limit", 3))),
        )
        progress, decision = controller.decide(state.get("acceptance_progress"), observation)
        state["acceptance_progress"] = progress
        state["acceptance_decision"] = decision.to_dict()
        state["acceptance_fingerprint"] = current_fingerprint
        state["acceptance_observation_seq"] = sequence
        append_event(state, "acceptance_decision", {
            **decision.to_dict(),
            "context": context,
            "verification_token": verification_token,
            "artifacts_changed": artifacts_changed,
        })
        return state

    def _acceptance_evaluator_required(self, state: dict[str, Any]) -> bool:
        if isinstance(state.get("evaluation_skipped"), bool):
            return not state["evaluation_skipped"]
        allocation = state.get("time_allocation") or {}
        if "evaluator_enabled" in allocation:
            return bool(allocation["evaluator_enabled"])
        try:
            from profiles import get_profile

            profile = get_profile(state["profile"])
            resolved = _resolve_profile_value(
                profile,
                "resolve_time_allocation",
                state["prompt"],
                state.get("task_id"),
            )
            return bool(profile.evaluator().enabled and resolved.get("evaluator_enabled", True))
        except Exception:
            return bool(state.get("score_history"))

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


def _resolve_profile_value(
    profile,
    method_name: str,
    prompt: str,
    task_id: str | None,
):
    """Call new task-aware profile APIs without breaking older overrides."""
    method = getattr(profile, method_name)
    signature = inspect.signature(method)
    supports_task_id = (
        "task_id" in signature.parameters
        or any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
    )
    if supports_task_id:
        return method(prompt, task_id=task_id)
    return method(prompt)


def _apply_phase_budgets(
    harness,
    allocation: dict[str, Any],
    task_budget: float,
    *,
    remaining_budget: float | None = None,
    current_phase: str = "",
) -> dict[str, float]:
    """Apply profile fractions to the freshly-created phase agents.

    Positive fractions are percentages of the total task budget. Fractions
    above a combined 100% are normalized to keep the configured phases within
    the same total. A positive enabled phase always receives at least one
    second so tiny synthetic test budgets remain executable. On resumed or
    multi-round runs, the remaining wall-clock budget is divided among the
    current and later phases. This preserves the configured reserve without
    allowing the sum of independently capped phase budgets to exceed the
    global remainder.
    """
    phases = ("planner", "builder", "evaluator")
    fractions: dict[str, float] = {}
    for phase in phases:
        enabled = bool(allocation.get(f"{phase}_enabled", True))
        try:
            fraction = float(allocation.get(phase, 0.0))
        except (TypeError, ValueError):
            fraction = 0.0
        fractions[phase] = max(0.0, fraction) if enabled else 0.0

    fraction_total = sum(fractions.values())
    scale = 1.0 / fraction_total if fraction_total > 1.0 else 1.0
    agent_phase = {
        "plan": "planner",
        "build": "builder",
        "evaluate": "evaluator",
    }.get(current_phase, current_phase)
    current_index = phases.index(agent_phase) if agent_phase in phases else 0
    eligible = {
        phase
        for index, phase in enumerate(phases)
        if index >= current_index and fractions[phase] > 0
    }
    eligible_total = sum(fractions[phase] for phase in eligible)
    budgets: dict[str, float] = {}
    for phase in phases:
        agent = getattr(harness, phase, None)
        if agent is None:
            continue
        fraction = fractions[phase]
        if remaining_budget is not None:
            if phase in eligible and eligible_total > 0:
                budget = max(0.0, remaining_budget) * fraction / eligible_total
            else:
                budget = 0.0
        else:
            budget = max(1.0, task_budget * fraction * scale) if fraction > 0 else 0.0
        agent.time_budget = budget
        budgets[phase] = budget
    return budgets


def _merge_patch(state: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        else:
            state[key] = value


def _trace_flags() -> dict[str, bool]:
    return {name: bool(getattr(config, name)) for name in dir(config) if name.startswith("HARNESS_") and isinstance(getattr(config, name), bool)}


def _latest_verification_token(analysis: dict[str, Any]) -> str | None:
    latest = (analysis.get("verification") or {}).get("latest")
    if not isinstance(latest, dict):
        return None
    token = latest.get("token")
    return str(token) if token else None


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
