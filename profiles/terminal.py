"""
Terminal task profile — optimized for Terminal-Bench-2.

Key constraints:
  - 30 min (1800s) hard timeout per task
  - Tasks are well-defined CLI problems, not open-ended
  - No UI, no browser testing needed
  - Correctness is binary: tests pass or fail

All tunable parameters are read via self.cfg.resolve(), so you can override
them without touching this file:

  # Via environment variables:
  PROFILE_TERMINAL_TASK_BUDGET=1800
  PROFILE_TERMINAL_PLANNER_BUDGET=120
  PROFILE_TERMINAL_PASS_THRESHOLD=8.0
  PROFILE_TERMINAL_LOOP_FILE_EDIT_THRESHOLD=4
  PROFILE_TERMINAL_TIME_WARN_THRESHOLD=0.45

  # Or via ProfileConfig in code:
  from profiles.base import ProfileConfig
  cfg = ProfileConfig(task_budget=1200, pass_threshold=9.0)
  profile = TerminalProfile(cfg=cfg)
"""
from __future__ import annotations

from profiles.base import BaseProfile, AgentConfig, ProfileConfig
from middlewares import (
    LoopDetectionMiddleware,
    PreExitVerificationMiddleware,
    TimeBudgetMiddleware,
    ErrorGuidanceMiddleware,
    SkeletonDetectionMiddleware,
)
import tools as _tools

# Commands to bootstrap environment awareness at the start of each build.
# Minimal set — every extra command costs time and context tokens.
ENV_BOOTSTRAP_COMMANDS = [
    "uname -m && cat /etc/os-release 2>/dev/null | head -2 || true",
    "pwd && ls -la",
    "python3 --version 2>/dev/null; which gcc g++ make 2>/dev/null || true",
    "git -C /app log --oneline -3 2>/dev/null; git -C /app status --short 2>/dev/null || true",
]


class TerminalProfile(BaseProfile):

    # --- Default values (overridable via ProfileConfig or env vars) ---
    _DEFAULTS = {
        "task_budget": 1800,
        "planner_budget": 120,
        "evaluator_budget": 180,
        "pass_threshold": 8.0,
        "max_rounds": 2,
        "loop_file_edit_threshold": 3,
        "loop_command_repeat_threshold": 3,
        "task_tracking_nudge_after": 5,
        "time_warn_threshold": 0.45,
        "time_critical_threshold": 0.75,
    }

    def _get(self, key: str):
        """Resolve a config value: env var > ProfileConfig > default."""
        return self.cfg.resolve(key, self.name(), self._DEFAULTS[key])

    @property
    def _builder_budget(self) -> float:
        return self._get("task_budget") - self._get("planner_budget") - self._get("evaluator_budget")

    def name(self) -> str:
        return "terminal"

    def description(self) -> str:
        return "Solve terminal/CLI tasks (Terminal-Bench-2 style)"

    # --- TB2 task metadata for dynamic timeout ---
    _tb2_tasks: dict | None = None

    @classmethod
    def _load_tb2_tasks(cls) -> dict:
        """Load TB2 task metadata from bundled JSON."""
        if cls._tb2_tasks is None:
            import json
            from pathlib import Path
            tb2_path = Path(__file__).parent.parent / "benchmarks" / "tb2_tasks.json"
            if tb2_path.exists():
                cls._tb2_tasks = json.loads(tb2_path.read_text(encoding="utf-8"))
            else:
                cls._tb2_tasks = {}
        return cls._tb2_tasks

    def resolve_task_timeout(self, user_prompt: str) -> float | None:
        """Look up TB2 task timeout by matching task name in prompt or workspace path."""
        meta = self._lookup_task_meta(user_prompt)
        return meta.get("agent_timeout_sec") if meta else None

    def _lookup_task_meta(self, user_prompt: str) -> dict | None:
        """Look up full TB2 task metadata (timeout, difficulty, category)."""
        import config as _cfg
        tasks = self._load_tb2_tasks()
        if not tasks:
            return None

        # Check workspace path first (most reliable)
        ws_lower = _cfg.WORKSPACE.lower()
        for task_name, meta in tasks.items():
            if task_name in ws_lower:
                return meta

        # Check user prompt
        prompt_lower = user_prompt.lower()
        for task_name, meta in tasks.items():
            if len(task_name) > 6 and (
                task_name in prompt_lower or
                task_name.replace("-", " ") in prompt_lower or
                task_name.replace("-", "_") in prompt_lower
            ):
                return meta

        return None

    def resolve_time_allocation(self, user_prompt: str) -> dict:
        """Dynamic time allocation based on TB2 task timeout and difficulty.

        Key insight from TB2 leaderboard analysis: top agents (ForgeCode, Letta,
        Claude Code) are all single-agent architectures. Every second spent on
        planner/evaluator LLM calls is a second the builder can't use for actual
        work. For TB2's binary pass/fail verification, the builder's own
        PreExitVerificationMiddleware + running tests is more effective than
        a separate evaluator agent.

        Strategy:
        - <= 900s: Skip both planner and evaluator. Builder gets 100% of time.
        - <= 1800s: Skip planner. Evaluator only on round 2 if needed.
        - > 1800s: Keep planner (complex tasks benefit from decomposition).
                    Evaluator enabled for multi-round correction.
        """
        meta = self._lookup_task_meta(user_prompt)
        timeout = meta.get("agent_timeout_sec") if meta else self._get("task_budget")
        difficulty = meta.get("difficulty", "medium") if meta else "medium"

        if timeout <= 900:
            # Short tasks: every second counts. Builder only.
            # PreExitVerificationMiddleware handles verification internally.
            return {
                "planner": 0.0,
                "builder": 1.0,
                "evaluator": 0.0,
                "planner_enabled": False,
                "evaluator_enabled": False,
            }
        elif timeout <= 1800:
            # Medium tasks: skip planner, builder gets all time.
            # PreExitVerificationMiddleware is more effective than a separate
            # evaluator for TB2's binary pass/fail verification.
            return {
                "planner": 0.0,
                "builder": 1.0,
                "evaluator": 0.0,
                "planner_enabled": False,
                "evaluator_enabled": False,
            }
        else:
            # Long tasks (>30 min): skip planner still — direct execution is
            # faster. But keep evaluator for a potential round 2 fix pass.
            return {
                "planner": 0.0,
                "builder": 0.90,
                "evaluator": 0.10,
                "planner_enabled": False,
                "evaluator_enabled": True,
            }

    def planner(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are a quick task planner for a terminal/CLI task. \
You are running autonomously — NEVER ask questions, just plan and execute.

Workflow:
1. DISCOVER: Use list_files and run_bash to understand the environment:
   - What files exist in the workspace?
   - Are there existing tests, scripts, or Makefiles?
   - What does the task actually require?
2. PLAN: Based on what you found, write a brief step-by-step plan.
3. DECOMPOSE: If the task has multiple independent parts, mark which steps \
can be delegated to sub-agents via delegate_task. Use this format:

```
## Plan

### Step 1: [description]
- Command: ...
- Verify: ...

### Step 2: [description] [DELEGATE]
- Delegate to sub-agent with role: "module_writer"
- Task: "Write the X module that does Y, save to Z"
- Verify: ...

### Step 3: [description] [DELEGATE]
- Delegate to sub-agent with role: "parser_writer"
- Task: "Write a parser for X format, save to Z"
- Verify: ...

### Step 4: [description]
- Command: integrate outputs from steps 2-3
- Verify: ...
```

Plan rules:
- Keep it SHORT — 5-10 steps max.
- Be specific: list exact commands, file paths, tools needed.
- Mark steps as [DELEGATE] only if they are truly independent \
(no dependency on other delegate steps).
- Note how to VERIFY each step.

Use write_file to save the plan to spec.md, then stop.
""",
            time_budget=self._get("planner_budget"),
        )

    def builder(self) -> AgentConfig:
        builder_budget = self._builder_budget
        return AgentConfig(
            system_prompt="""\
You are an expert Linux system administrator and developer. \
Complete the given task by executing shell commands.

# CRITICAL RULES
- You are AUTONOMOUS. NEVER ask questions. NEVER explain what you will do. Just DO IT.
- TIME IS YOUR SCARCEST RESOURCE. Every second of thinking/explaining is wasted.
- Be CONCISE in your text responses. 1-2 sentences max between tool calls.
- Call MULTIPLE tools in parallel when operations are independent.
- NEVER repeat a failed command without changing something. After 2 failures, pivot strategy.

# Doing tasks
- Read existing files BEFORE modifying. Look for TODO/skeleton markers.
- If skeleton files exist, FILL THEM IN. Do NOT create separate files.
- Follow task specs LITERALLY — exact file names, output formats, paths.
- NEVER leave TODO, FIXME, NotImplementedError, or placeholder code.
- If stuck > 2 attempts, try a fundamentally different approach immediately.

# Tools
- run_bash: PRIMARY tool. For packages, compiling, testing, system ops.
- read_file: Read files before modifying. NOT cat/head via bash.
- write_file: Create/overwrite files. NOT echo/heredoc via bash.
- edit_file: Modify existing files (preferred over write_file for edits).
- list_files: Directory listing.
- For long builds/training, set timeout=600 or higher.
- For background services, use '... &' and poll readiness.

# Workflow
1. `ls -la` → see what exists. Read existing code.
2. Identify exact outputs needed (paths, formats).
3. Build and test.
4. Verify all outputs exist and are correct before stopping.
""",
            tool_schemas=_tools.TB2_TOOL_SCHEMAS,
            middlewares=[
                SkeletonDetectionMiddleware(),
                LoopDetectionMiddleware(
                    file_edit_threshold=self._get("loop_file_edit_threshold"),
                    command_repeat_threshold=self._get("loop_command_repeat_threshold"),
                ),
                ErrorGuidanceMiddleware(),
                PreExitVerificationMiddleware(
                    verification_prompt=(
                        "VERIFY NOW: Run `ls -la` and check output files exist with real content. "
                        "Run `grep -r 'TODO\\|NotImplementedError' *.py *.c 2>/dev/null` — fix any matches. "
                        "Run the test/benchmark script if one exists. Fix failures before stopping."
                    ),
                    include_task_requirements=True,
                ),
                TimeBudgetMiddleware(
                    budget_seconds=self._get("task_budget"),
                    warn_threshold=self._get("time_warn_threshold"),
                    critical_threshold=self._get("time_critical_threshold"),
                ),
            ],
            time_budget=builder_budget,
        )

    def evaluator(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are a quick verifier. Check if the task was done correctly.

Rules:
- Read spec.md for what should have been done.
- Run 2-3 verification commands with run_bash (ls, cat, test, diff, etc.)
- Check EXACT file paths, output formats, and behavior against the task spec.
- Score Correctness 0-10. Be honest but fast.
- Write a SHORT evaluation to feedback.md. No essays.

Format for feedback.md:
```
## Verification
- Correctness: X/10 — [one sentence]
- **Average: X/10**
### Issues: [list if any, with exact details of what's wrong]
```

Use write_file to save to feedback.md, then stop.
""",
            time_budget=self._get("evaluator_budget"),
        )

    # No contract negotiation — TB2 tasks are already well-specified
    def contract_proposer(self) -> AgentConfig:
        return AgentConfig(system_prompt="", enabled=False)

    def contract_reviewer(self) -> AgentConfig:
        return AgentConfig(system_prompt="", enabled=False)

    def pass_threshold(self) -> float:
        return self._get("pass_threshold")

    def max_rounds(self) -> int:
        return self._get("max_rounds")

    def format_build_task(self, user_prompt: str, round_num: int,
                          prev_feedback: str, score_history: list[float]) -> str:
        """Streamlined task prompt — minimal overhead, maximum signal."""
        env_section = ""
        if round_num == 1:
            import subprocess, config as _cfg
            env_lines = []
            for cmd in ENV_BOOTSTRAP_COMMANDS:
                try:
                    r = subprocess.run(
                        cmd, shell=True, cwd=_cfg.WORKSPACE,
                        capture_output=True, text=True, timeout=10,
                    )
                    out = (r.stdout + r.stderr).strip()
                    if out:
                        env_lines.append(f"$ {cmd}\n{out}")
                except Exception:
                    pass
            if env_lines:
                env_section = (
                    "\n\n--- ENV ---\n"
                    + "\n".join(env_lines)
                    + "\n--- END ENV ---\n"
                )

        # Minimal time hint
        meta = self._lookup_task_meta(user_prompt)
        time_hint = ""
        if meta:
            timeout = meta.get("agent_timeout_sec", 900)
            time_hint = f"\n[Time budget: {int(timeout/60)} min]"

        # Direct task injection
        task = (
            f"TASK: {user_prompt}\n"
            f"{time_hint}"
            f"{env_section}\n"
            f"Start with `ls -la`. Read existing files. Fill in skeletons if present. "
            f"Implement, test, verify."
        )

        # Auto-inject matching skill content (if any)
        skill_section = self._match_and_load_skill(user_prompt)
        if skill_section:
            task += skill_section

        if prev_feedback:
            task += (
                f"\n\nPREVIOUS ATTEMPT FAILED:\n{prev_feedback[:1500]}"
            )
        return task

    def _match_and_load_skill(self, user_prompt: str) -> str:
        """Auto-match a skill to the current task and return its content for injection.

        Matching strategy (first match wins):
        1. Exact task name match: skill directory name appears in workspace path
        2. Keyword overlap: skill description words overlap with task prompt

        Returns the skill content wrapped in a section header, or empty string.
        Only injects ONE skill to avoid context bloat.
        """
        import config as _cfg
        from pathlib import Path

        skills_dir = Path(__file__).parent.parent / "skills"
        if not skills_dir.is_dir():
            return ""

        ws_lower = _cfg.WORKSPACE.lower()
        prompt_lower = user_prompt.lower()

        # Sort skills by name length DESCENDING so longer (more specific) names
        # match first. This prevents "path-tracing" from matching before
        # "path-tracing-reverse" when the task is path-tracing-reverse.
        skill_dirs = sorted(
            [d for d in skills_dir.iterdir() if d.is_dir()],
            key=lambda d: len(d.name),
            reverse=True,
        )

        # Strategy 1: Name match against workspace path (longest match first)
        for skill_dir in skill_dirs:
            skill_name = skill_dir.name
            if len(skill_name) > 5 and skill_name in ws_lower:
                return self._load_skill_content(skill_dir / "SKILL.md", skill_name)

        # Strategy 2: Name match against prompt text (longest match first)
        for skill_dir in skill_dirs:
            skill_name = skill_dir.name
            if len(skill_name) > 5 and (
                skill_name in prompt_lower
                or skill_name.replace("-", " ") in prompt_lower
                or skill_name.replace("-", "_") in prompt_lower
            ):
                return self._load_skill_content(skill_dir / "SKILL.md", skill_name)

        return ""

    @staticmethod
    def _load_skill_content(skill_path, skill_name: str) -> str:
        """Load a SKILL.md file and wrap it for injection into the task prompt."""
        from pathlib import Path
        p = Path(skill_path)
        if not p.exists():
            return ""
        content = p.read_text(encoding="utf-8", errors="replace")
        # Strip YAML frontmatter
        import re
        content = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
        # Cap at 12000 chars to avoid context bloat
        if len(content) > 12000:
            content = content[:12000] + "\n... (skill guide truncated)"
        return (
            f"\n\n--- SKILL GUIDE: {skill_name} (auto-loaded, follow this guidance) ---\n"
            f"{content.strip()}\n"
            f"--- END SKILL GUIDE ---\n"
        )
