"""State-file-driven scheduler for Harness runs."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

import config
import tools
from orchestrator.analytics import analyze_workspace, list_artifacts
from orchestrator.hooks import HookManager, HookResult
from orchestrator.memory import MemoryStore, VALID_PROFILES
from orchestrator.router import Router
from orchestrator.run_context import RunContext
from orchestrator.state import append_event, load_state, now_iso, save_state
from orchestrator.strategy import append_strategy_hints_to_prompt

log = logging.getLogger("harness")


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
        config.WORKSPACE = str(context.workspace)

        profile = get_profile(profile_name)
        harness = Harness(profile)
        allocation = state.get("time_allocation") or profile.resolve_time_allocation(state["prompt"])
        return harness, profile, allocation

    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        harness, profile, allocation = self._prepare(state)
        state["time_allocation"] = allocation

        if harness.planner and allocation.get("planner_enabled", True):
            harness.planner.run(
                f"Create a plan for the following task:\n\n"
                f"{state['prompt']}\n\n"
                f"Save the plan to {config.SPEC_FILE}."
            )
        else:
            spec_path = Path(config.WORKSPACE) / config.SPEC_FILE
            spec_path.write_text(f"# Task\n\n{state['prompt']}\n", encoding="utf-8")

        state["artifacts"] = {"files": list_artifacts(config.WORKSPACE)}
        return state

    def contract(self, state: dict[str, Any]) -> dict[str, Any]:
        harness, _profile, _allocation = self._prepare(state)
        if harness.contract_proposer and harness.contract_reviewer:
            harness._negotiate_contract(state.get("round_num", 1))
        state["artifacts"] = {"files": list_artifacts(config.WORKSPACE)}
        return state

    def build(self, state: dict[str, Any]) -> dict[str, Any]:
        harness, profile, _allocation = self._prepare(state)
        total_start = _parse_start_time(state.get("created_at"))

        from middlewares import TimeBudgetMiddleware
        for middleware in harness.builder.middlewares:
            if isinstance(middleware, TimeBudgetMiddleware):
                middleware.sync_start_time(total_start)
                task_timeout = profile.resolve_task_timeout(state["prompt"])
                if task_timeout:
                    middleware.budget_seconds = task_timeout

        feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
        prev_feedback = feedback_path.read_text(encoding="utf-8", errors="replace") if feedback_path.exists() else ""
        score_history = [float(s) for s in state.get("score_history", [])]
        build_task = profile.format_build_task(
            state["prompt"],
            int(state.get("round_num", 1)),
            prev_feedback,
            score_history,
        )
        build_task = append_strategy_hints_to_prompt(build_task, state, "builder")
        harness.builder.run(build_task)
        state["artifacts"] = {"files": list_artifacts(config.WORKSPACE)}
        return state

    def evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
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
        harness.evaluator.run(eval_task)
        tools.stop_dev_server()

        feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
        feedback_text = feedback_path.read_text(encoding="utf-8", errors="replace") if feedback_path.exists() else ""
        score = profile.extract_score(feedback_text)
        state.setdefault("score_history", []).append(score)
        state["artifacts"] = {"files": list_artifacts(config.WORKSPACE)}
        return state

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
            result = self.hooks.on_error(state, exc)
            state = self._apply_hook_result(state, result, failed_action=action)
            if result.action == "continue":
                state["status"] = "error"
                state["active"] = False
                state["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
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
            time.sleep(poll_interval)

    def _route(self, state: dict[str, Any]) -> dict[str, Any]:
        from_phase = state.get("phase")
        append_event(state, "before_route", {"prompt_preview": str(state.get("prompt", ""))[:200]})
        state = self._apply_hook_result(state, self.hooks.before_route(state))
        append_event(state, "before_transition", {"from": from_phase, "to": "route"})
        self.hooks.before_transition(from_phase, "route", state)
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
        before_artifacts = list((state.get("artifacts") or {}).get("files", []))
        state["phase"] = phase
        state["status"] = "running"
        state["phase_started_at"] = now_iso()
        append_event(state, "phase_started", {"phase": phase, "round": state.get("round_num")})
        append_event(state, "before_transition", {"from": from_phase, "to": phase})
        save_state(self.state_path, state)

        state = self._apply_hook_result(state, self.hooks.before_transition(from_phase, phase, state))
        append_event(state, "before_agent_run", {"agent": agent_name, "phase": phase})
        save_state(self.state_path, state)
        state = self._apply_hook_result(state, self.hooks.before_agent_run(agent_name, state))
        state = callback(load_state(self.state_path))
        append_event(state, "after_agent_run", {"agent": agent_name, "phase": phase})
        state = self._apply_hook_result(state, self.hooks.after_agent_run(agent_name, state))
        append_event(state, "phase_completed", {"phase": phase, "round": state.get("round_num")})
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
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
        else:
            state = self._apply_hook_result(state, self.hooks.before_new_round(state))
            if state.get("status") in {"paused", "error", "waiting_confirmation", "waiting_approval"}:
                return state
            state["round_num"] = int(state.get("round_num", 1)) + 1
            if self._contract_enabled(state):
                state["phase"] = "contract"
                state["next_action"] = "contract"
            else:
                state["phase"] = "build"
                state["next_action"] = "build"
        return state

    def _analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        append_event(state, "phase_started", {"phase": "analyze"})
        analysis = analyze_workspace(state["workspace"])
        state["analysis"] = analysis
        state["artifacts"] = {"files": list_artifacts(state["workspace"])}
        state["phase"] = "complete"
        state["next_action"] = None
        state["status"] = "completed"
        state["active"] = False
        append_event(state, "phase_completed", {"phase": "analyze"})
        append_event(state, "run_completed", {"status": "completed"})
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


def _merge_patch(state: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        else:
            state[key] = value
