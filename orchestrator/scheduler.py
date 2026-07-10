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
from orchestrator.hooks import HookManager
from orchestrator.memory import MemoryStore, VALID_PROFILES
from orchestrator.router import Router
from orchestrator.run_context import RunContext
from orchestrator.state import append_event, load_state, save_state

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
        harness.builder.run(build_task)
        state["artifacts"] = {"files": list_artifacts(config.WORKSPACE)}
        return state

    def evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        harness, profile, allocation = self._prepare(state)
        if not harness.evaluator or not allocation.get("evaluator_enabled", True):
            state["evaluation_skipped"] = True
            return state

        harness.evaluator.run(
            f"This is evaluation round {state.get('round_num', 1)}.\n"
            f"Read {config.SPEC_FILE} to understand the task.\n"
            f"Examine the work done and test it thoroughly.\n"
            f"Score each criterion honestly. Write your evaluation to {config.FEEDBACK_FILE}."
        )
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

        if not state.get("active"):
            self.hooks.on_stall(state, "active flag is false")
            return state
        if state.get("requires_confirmation"):
            self.hooks.on_stall(state, "profile confirmation required")
            return state
        if not state.get("next_action"):
            self.hooks.on_stall(state, "no next action")
            return state

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
            self.hooks.on_error(state, exc)
            state["status"] = "error"
            state["active"] = False
            state["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
            append_event(state, "scheduler_error", state["last_error"])
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
        append_event(state, "before_transition", {"from": from_phase, "to": "route"})
        self.hooks.before_transition(from_phase, "route", state)
        decision = self.router.route(state["prompt"], state.get("profile"))
        state["route_decision"] = decision.to_dict()
        state["memory_refs"] = decision.memory_refs or []
        append_event(state, "route_decision", state["route_decision"])

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
        state["phase"] = phase
        state["status"] = "running"
        append_event(state, "phase_started", {"phase": phase, "round": state.get("round_num")})
        append_event(state, "before_transition", {"from": from_phase, "to": phase})
        save_state(self.state_path, state)

        self.hooks.before_transition(from_phase, phase, state)
        append_event(state, "before_agent_run", {"agent": agent_name, "phase": phase})
        save_state(self.state_path, state)
        self.hooks.before_agent_run(agent_name, state)
        state = callback(load_state(self.state_path))
        append_event(state, "after_agent_run", {"agent": agent_name, "phase": phase})
        self.hooks.after_agent_run(agent_name, state)
        append_event(state, "phase_completed", {"phase": phase, "round": state.get("round_num")})
        append_event(state, "after_transition", {"from": from_phase, "to": phase})
        self.hooks.after_transition(from_phase, phase, state)
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
        max_rounds = self._max_rounds(state)
        threshold = self._pass_threshold(state)
        score_history = state.get("score_history", [])
        score = float(score_history[-1]) if score_history else 0.0

        if score >= threshold or int(state.get("round_num", 1)) >= max_rounds:
            state["phase"] = "analyze"
            state["next_action"] = "analyze"
        else:
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
        self.memory.record_run(state, analysis)
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
    append_event(state, "profile_confirmed", {"profile": profile})
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
