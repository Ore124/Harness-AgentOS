"""
Agent middlewares — hooks that run at specific points in the agent loop.

Middlewares are the harness engineer's primary tool for shaping agent behavior
without changing the core loop. They intercept execution at defined points:

  - post_tool:    After a tool call completes. Use for loop detection, tracking.
  - pre_exit:     When the agent wants to stop (no more tool calls). Use for
                  forced verification passes.
  - per_iteration: At the start of each iteration. Use for time budget warnings.

Middlewares return an optional message to inject into the conversation.
Returning None means "no intervention."

Design principle: middlewares are composable and profile-specific.
The base Agent loop knows nothing about terminal tasks or time budgets —
profiles wire in the middlewares they need.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("harness")


class AgentMiddleware(ABC):
    """Base class for agent middlewares."""

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        """Called after each tool execution. Return a message to inject, or None."""
        return None

    def pre_exit(self, messages: list[dict]) -> str | None:
        """Called when the agent wants to stop. Return a message to force continuation, or None."""
        return None

    def per_iteration(self, iteration: int, messages: list[dict]) -> str | None:
        """Called at the start of each iteration. Return a message to inject, or None."""
        return None


# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------

class LoopDetectionMiddleware(AgentMiddleware):
    """
    Tracks per-file edit counts and detects repetitive command patterns.
    When the agent edits the same file or runs similar commands too many times,
    injects a nudge to reconsider the approach.

    Uses fuzzy matching for commands — catches variants like:
      python3 app.py  /  python3 app.py 2>&1  /  python3 ./app.py
    """

    def __init__(self, file_edit_threshold: int = 4, command_repeat_threshold: int = 3):
        self.file_edit_threshold = file_edit_threshold
        self.command_repeat_threshold = command_repeat_threshold
        self.file_edit_counts: dict[str, int] = {}
        self.recent_commands: list[str] = []
        self._file_warned: set[str] = set()  # avoid spamming same warning

    @staticmethod
    def _normalize_command(cmd: str) -> str:
        """Normalize a command for fuzzy comparison."""
        import re
        cmd = cmd.strip()
        # Remove common suffixes that don't change semantics
        cmd = re.sub(r'\s*2>&1\s*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*head.*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*tail.*$', '', cmd)
        # Normalize paths: ./foo → foo
        cmd = re.sub(r'\./(\S)', r'\1', cmd)
        # Collapse whitespace
        cmd = re.sub(r'\s+', ' ', cmd)
        return cmd.strip()

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        # Track file edits
        if tool_name == "write_file":
            path = tool_args.get("path", "")
            self.file_edit_counts[path] = self.file_edit_counts.get(path, 0) + 1
            count = self.file_edit_counts[path]
            if count >= self.file_edit_threshold and path not in self._file_warned:
                self._file_warned.add(path)
                log.warning(f"Loop detection: {path} edited {count} times")
                return (
                    f"[SYSTEM] You have edited '{path}' {count} times. "
                    "This pattern suggests your current approach may not be working. "
                    "STOP and reconsider:\n"
                    "1. Re-read the original task requirements.\n"
                    "2. Think about what's fundamentally wrong with your approach.\n"
                    "3. Try a completely different strategy."
                )

        # Track repeated commands (with fuzzy matching)
        if tool_name == "run_bash":
            cmd = tool_args.get("command", "").strip()
            self.recent_commands.append(cmd)
            if len(self.recent_commands) >= self.command_repeat_threshold:
                window = self.recent_commands[-self.command_repeat_threshold:]
                normalized = [self._normalize_command(c) for c in window]
                if len(set(normalized)) == 1:
                    log.warning(f"Loop detection: similar command repeated {self.command_repeat_threshold}x")
                    return (
                        f"[SYSTEM] You have run essentially the same command {self.command_repeat_threshold} "
                        f"times in a row with no progress.\n"
                        f"Command pattern: {normalized[0][:200]}\n"
                        "This is a doom loop. The same action will not produce a different result.\n"
                        "STOP. Re-read the error output carefully. Try a fundamentally different approach."
                    )

            # Also detect rapid-fire failed commands (different commands, same error)
            if "[error]" in result or "command not found" in result.lower():
                recent_errors = 0
                for msg in reversed(messages[-10:]):
                    content = msg.get("content", "")
                    if msg.get("role") == "tool" and (
                        "[error]" in content or "command not found" in content.lower()
                        or "[exit code:" in content
                    ):
                        recent_errors += 1
                if recent_errors >= 3:
                    return (
                        "[SYSTEM] Multiple consecutive commands have failed. "
                        "STOP trying variations of the same approach. "
                        "Diagnose the ROOT CAUSE:\n"
                        "1. Is the required tool/package installed?\n"
                        "2. Are you in the right directory? Run `pwd` and `ls -la`.\n"
                        "3. Is there a dependency missing? Check error messages carefully.\n"
                        "4. Consider a completely different approach to the problem."
                    )

        return None


# ---------------------------------------------------------------------------
# Pre-Exit Verification
# ---------------------------------------------------------------------------

class PreExitVerificationMiddleware(AgentMiddleware):
    """
    Forces the agent to run a verification pass before it's allowed to stop.

    Three-level exit gate:
    1. First exit attempt with NO tool calls ever made → force agent to start working
    2. First exit attempt after some work → force verification pass
    3. Second exit attempt after verification → allow exit

    This prevents the "3-second exit" problem where weak models return text
    without calling any tools, and PreExitVerification lets them go after
    just one retry.
    """

    def __init__(self, verification_prompt: str | None = None,
                 include_task_requirements: bool = True):
        self._exit_attempts = 0
        self._verification_prompt = verification_prompt
        self._include_task_requirements = include_task_requirements

    @staticmethod
    def _has_done_work(messages: list[dict]) -> bool:
        """Check if the agent has called any action tools (run_bash, write_file, delegate_task)."""
        action_tools = {"run_bash", "write_file", "delegate_task"}
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn_name = tc.get("function", {}).get("name", "")
                    if fn_name in action_tools:
                        return True
        return False

    @staticmethod
    def _check_workspace_outputs() -> str:
        """Check what files exist in the workspace and flag potential issues."""
        import config as _cfg
        from pathlib import Path
        import subprocess

        ws = Path(_cfg.WORKSPACE)
        if not ws.exists():
            return ""

        issues = []

        # Check for TODO/NotImplementedError in Python files
        try:
            result = subprocess.run(
                "grep -rl 'NotImplementedError\\|raise NotImplemented\\|# TODO\\|pass  # TODO' "
                "--include='*.py' . 2>/dev/null | head -5",
                shell=True, cwd=str(ws), capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                files = result.stdout.strip()
                issues.append(
                    f"WARNING: These files still contain TODO/NotImplementedError:\n{files}\n"
                    "You MUST fill in the implementation before stopping."
                )
        except Exception:
            pass

        # Check for empty key output files
        try:
            result = subprocess.run(
                "find . -maxdepth 2 -name '*.py' -o -name '*.c' -o -name '*.txt' "
                "-o -name '*.json' -o -name '*.csv' 2>/dev/null | head -20",
                shell=True, cwd=str(ws), capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                for f in result.stdout.strip().splitlines():
                    fp = ws / f.lstrip("./")
                    if fp.exists() and fp.stat().st_size == 0:
                        issues.append(f"WARNING: {f} exists but is EMPTY (0 bytes).")
        except Exception:
            pass

        return "\n".join(issues)

    @staticmethod
    def _extract_task_requirements(messages: list[dict]) -> str | None:
        """Extract the original task requirements from the conversation."""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 20:
                    if len(content) > 3000:
                        content = content[:3000] + "\n... (truncated)"
                    return content
        return None

    def pre_exit(self, messages: list[dict]) -> str | None:
        self._exit_attempts += 1
        has_worked = self._has_done_work(messages)

        # Gate 1: Agent hasn't done ANY work — force it to start
        if not has_worked:
            log.warning(f"Pre-exit: agent wants to stop but has done NO work (attempt {self._exit_attempts})")
            if self._exit_attempts <= 3:  # give up to 3 chances to start working
                return (
                    "[SYSTEM] You have NOT completed the task. You have not executed any commands "
                    "or written any files yet.\n"
                    "You MUST use run_bash to execute commands and write_file to create output files.\n"
                    "Read the task requirements again and START WORKING. Do not just describe "
                    "what you would do — actually DO it using the available tools."
                )
            # After 3 attempts with no work, give up
            log.error("Pre-exit: agent refused to work after 3 attempts")
            return None

        # Gate 2: Agent has done work, first exit → force verification
        if self._exit_attempts == 1:
            log.info("Pre-exit verification: forcing verification pass")

            parts = []
            parts.append(
                "[SYSTEM] MANDATORY VERIFICATION — You are about to finish, "
                "but you MUST verify your work first."
            )

            # Auto-check workspace for common issues
            workspace_issues = self._check_workspace_outputs()
            if workspace_issues:
                parts.append(
                    "\n⚠️ AUTOMATED CHECKS FOUND ISSUES:\n"
                    f"{workspace_issues}\n"
                    "You MUST fix these issues before stopping."
                )

            if self._include_task_requirements:
                task_text = self._extract_task_requirements(messages)
                if task_text:
                    parts.append(
                        "\n--- ORIGINAL TASK REQUIREMENTS (verify against these, not your memory) ---\n"
                        f"{task_text}\n"
                        "--- END ORIGINAL TASK REQUIREMENTS ---"
                    )

            if self._verification_prompt:
                parts.append(f"\n{self._verification_prompt}")
            else:
                parts.append(
                    "\nDo NOT just re-read your code. Run actual test/check commands:\n"
                    "1. Go through EACH requirement above one by one.\n"
                    "2. For each, run a concrete verification command "
                    "(cat, ls -la, test -f, diff, grep, python3 -c, etc.)\n"
                    "3. Compare ACTUAL output against what the task asked for.\n"
                    "4. Pay special attention to exact formats, column orders, "
                    "file paths, and edge-case rules mentioned in the task.\n"
                    "5. If ANY check fails, fix it before stopping.\n"
                    "Think like an automated test script — would your solution pass?"
                )

            return "\n".join(parts)

        # Gate 3: Agent has done work and verified → allow exit
        log.info("Pre-exit verification: agent verified, allowing exit")
        return None


# ---------------------------------------------------------------------------
# Time Budget
# ---------------------------------------------------------------------------

class TimeBudgetMiddleware(AgentMiddleware):
    """
    Injects time awareness into the agent loop.

    At configurable thresholds (default: 60% and 85% of budget),
    warns the agent about remaining time and nudges it toward
    wrapping up and verifying.

    Can track time from harness start (not just agent start) by calling
    sync_start_time() before the agent runs. This ensures the budget
    accounts for time already spent on planning/setup.
    """

    def __init__(self, budget_seconds: float,
                 warn_threshold: float = 0.60,
                 critical_threshold: float = 0.85):
        self.budget_seconds = budget_seconds
        self.warn_threshold = warn_threshold
        self.critical_threshold = critical_threshold
        self.start_time = time.time()
        self._warned = False
        self._critical = False

    def sync_start_time(self, harness_start: float):
        """Set start time to harness start, so budget includes planning/setup time."""
        self.start_time = harness_start

    def per_iteration(self, iteration: int, messages: list[dict]) -> str | None:
        elapsed = time.time() - self.start_time
        fraction = elapsed / self.budget_seconds
        remaining = self.budget_seconds - elapsed

        if remaining <= 0:
            if not self._critical:
                self._critical = True
                log.warning("Time budget EXPIRED")
                return (
                    "[SYSTEM] ⚠️ TIME IS UP. "
                    "Submit your current work NOW. Do not start anything new."
                )
            return None

        if fraction >= self.critical_threshold and not self._critical:
            self._critical = True
            mins_left = remaining / 60
            log.warning(f"Time budget critical: {mins_left:.1f} min remaining")
            return (
                f"[SYSTEM] ⚠️ CRITICAL: Only {mins_left:.0f} min left. "
                "STOP building. Verify outputs exist and are correct. Fix only critical bugs."
            )

        if fraction >= self.warn_threshold and not self._warned:
            self._warned = True
            mins_left = remaining / 60
            log.info(f"Time budget warning: {mins_left:.1f} min remaining")
            return (
                f"[SYSTEM] {mins_left:.0f} min remaining. "
                "Wrap up current work. Start testing and verification soon."
            )

        return None


# ---------------------------------------------------------------------------
# Task Tracking (forced decomposition)
# ---------------------------------------------------------------------------

class TaskTrackingMiddleware(AgentMiddleware):
    """
    Enforces explicit task tracking for multi-step work via _todo.md.

    Inspired by ForgeCode's todo_write enforcement — their single biggest
    improvement (38% → 66% on TB2). The key insight: optional planning tools
    get deprioritized under pressure. Making tracking mandatory prevents the
    agent from losing track of steps in complex tasks.

    Behavior:
    1. After the first few tool calls, if no _todo.md exists, inject a
       MANDATORY instruction to create one with a checklist.
    2. Periodically check that _todo.md is being updated. If the agent
       has made many tool calls without updating it, remind it.
    3. The _todo.md file lives in the workspace and persists across
       context compaction/reset (unlike mental checklists).

    This is NOT a soft nudge. The injection uses [MANDATORY] framing
    and includes the original task requirements for reference.
    """

    # How many tool calls before we demand a todo list
    DEMAND_AFTER = 4
    # How many tool calls between update reminders
    UPDATE_INTERVAL = 12

    def __init__(self, nudge_after_n_tools: int = 4):
        self.DEMAND_AFTER = nudge_after_n_tools
        self.tool_call_count = 0
        self._todo_created = False
        self._last_update_check = 0
        self._todo_content_hash: int = 0

    def _todo_exists(self) -> bool:
        """Check if _todo.md exists in the workspace."""
        import config as _cfg
        from pathlib import Path
        return (Path(_cfg.WORKSPACE) / "_todo.md").exists()

    def _read_todo(self) -> str:
        """Read current _todo.md content."""
        import config as _cfg
        from pathlib import Path
        p = Path(_cfg.WORKSPACE) / "_todo.md"
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return ""

    def _has_written_todo(self, messages: list[dict]) -> bool:
        """Check if agent has written to _todo.md in recent messages."""
        for msg in reversed(messages[-10:]):
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    if fn.get("name") == "write_file":
                        args_str = fn.get("arguments", "")
                        if "_todo" in args_str.lower() or "todo" in args_str.lower():
                            return True
        return False

    def _extract_task_text(self, messages: list[dict]) -> str:
        """Extract the original task description from messages."""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 50:
                    # Truncate to avoid bloating the injection
                    return content[:2000]
        return ""

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        self.tool_call_count += 1

        # Track if agent just wrote _todo.md
        if tool_name == "write_file":
            path = tool_args.get("path", "")
            if "todo" in path.lower():
                self._todo_created = True
                self._last_update_check = self.tool_call_count
                content = tool_args.get("content", "")
                self._todo_content_hash = hash(content)
                return None

        # Phase 1: Demand todo creation after DEMAND_AFTER tool calls
        if not self._todo_created and self.tool_call_count >= self.DEMAND_AFTER:
            # Check if it exists on disk (maybe created via run_bash)
            if self._todo_exists():
                self._todo_created = True
                self._todo_content_hash = hash(self._read_todo())
                self._last_update_check = self.tool_call_count
                return None

            # Also check if agent wrote it recently but we missed it
            if self._has_written_todo(messages):
                self._todo_created = True
                self._last_update_check = self.tool_call_count
                return None

            log.warning("Task tracking: DEMANDING todo creation")
            task_text = self._extract_task_text(messages)
            return (
                "[MANDATORY] You have been working without a task checklist. "
                "Before your next action, you MUST create _todo.md with a checklist "
                "of ALL remaining steps to complete this task.\n\n"
                "Use write_file to create _todo.md with this format:\n"
                "```\n"
                "# Task Checklist\n"
                "- [ ] Step 1: <description>\n"
                "- [ ] Step 2: <description>\n"
                "- [x] Step 3: <already done>\n"
                "```\n\n"
                "Mark steps you've already completed with [x]. "
                "This file persists even if your context is reset.\n\n"
                f"Original task for reference:\n{task_text[:1000]}"
            )

        # Phase 2: Periodic update reminders
        if self._todo_created:
            calls_since_update = self.tool_call_count - self._last_update_check
            if calls_since_update >= self.UPDATE_INTERVAL:
                self._last_update_check = self.tool_call_count

                # Check if content actually changed
                current = self._read_todo()
                current_hash = hash(current)
                if current_hash == self._todo_content_hash and current:
                    log.info("Task tracking: todo not updated, reminding")
                    self._todo_content_hash = current_hash
                    return (
                        "[SYSTEM] You have made many tool calls since last updating "
                        "_todo.md. Read it now, mark completed steps with [x], "
                        "and update any steps that changed. This keeps you on track."
                    )
                self._todo_content_hash = current_hash

        return None


# ---------------------------------------------------------------------------
# Skeleton/Template Detection (prevents ignoring existing TODO files)
# ---------------------------------------------------------------------------

class SkeletonDetectionMiddleware(AgentMiddleware):
    """
    Detects skeleton/template files with TODO markers at the start of work
    and injects a strong reminder to fill them in rather than create new files.

    This addresses a critical failure mode where models read skeleton files
    but then create separate implementations instead of filling in the TODOs,
    causing verifier tests to fail because they import from the original files.
    """

    def __init__(self):
        self._checked = False
        self._skeleton_files: list[str] = []

    def _scan_for_skeletons(self) -> list[tuple[str, list[str]]]:
        """Scan workspace for files containing TODO/NotImplementedError markers."""
        import config as _cfg
        import subprocess

        results = []
        try:
            # Find files with TODO markers
            r = subprocess.run(
                "grep -rn 'TODO\\|NotImplementedError\\|FIXME\\|PLACEHOLDER\\|FILL IN\\|YOUR CODE HERE' "
                "--include='*.py' --include='*.c' --include='*.cpp' --include='*.h' "
                "--include='*.java' --include='*.rs' --include='*.go' --include='*.v' "
                ". 2>/dev/null | head -30",
                shell=True, cwd=_cfg.WORKSPACE, capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip():
                # Group by file
                file_todos: dict[str, list[str]] = {}
                for line in r.stdout.strip().splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        fname = parts[0].lstrip("./")
                        todo_line = parts[2].strip()
                        if fname not in file_todos:
                            file_todos[fname] = []
                        file_todos[fname].append(todo_line[:100])
                results = [(f, todos) for f, todos in file_todos.items()]
        except Exception:
            pass
        return results

    def per_iteration(self, iteration: int, messages: list[dict]) -> str | None:
        # Only check once, early in the agent's work
        if self._checked or iteration > 3:
            return None

        # Only trigger after the agent has had a chance to read files
        # (iteration 2-3 is ideal)
        if iteration < 2:
            return None

        self._checked = True
        skeletons = self._scan_for_skeletons()

        if not skeletons:
            return None

        self._skeleton_files = [f for f, _ in skeletons]
        log.warning(f"Skeleton detection: found {len(skeletons)} files with TODOs")

        parts = [
            "[MANDATORY] SKELETON FILES DETECTED — You MUST fill in these files, "
            "not create new ones. The test scripts import from these exact files.\n"
        ]
        for fname, todos in skeletons[:5]:  # limit to 5 files
            parts.append(f"  📄 {fname}:")
            for todo in todos[:3]:
                parts.append(f"     → {todo}")

        parts.append(
            "\nACTION REQUIRED: Read each file above, find the TODO/NotImplementedError "
            "markers, and REPLACE them with working implementations. "
            "Do NOT create separate files — the tests import from these exact paths."
        )

        return "\n".join(parts)




class ErrorGuidanceMiddleware(AgentMiddleware):
    """
    Detects common error patterns in tool output and injects specific,
    actionable recovery suggestions.

    Weak models struggle to recover from errors on their own — they often
    retry the same failing command or give up. This middleware matches
    error patterns and provides concrete next steps.

    Based on TB2 command-level error analysis:
      - 24.1% of failures: command not found / not on PATH
      -  9.6% of failures: runtime errors in executables
      -  High rate: permission denied, missing dependencies
    """

    # Pattern → (description, recovery suggestion)
    # Patterns are checked in order; first match wins.
    ERROR_PATTERNS: list[tuple[str, str, str]] = [
        # --- Command not found ---
        (
            "command not found",
            "command_not_found",
            "The command is not installed. Try:\n"
            "  apt-get update && apt-get install -y <package>  (for system tools)\n"
            "  pip install <package>  (for Python tools)\n"
            "  which <command> || apt-cache search <keyword>  (to find the right package)\n"
            "If apt-get fails with permission denied, prefix with sudo.",
        ),
        (
            "no such file or directory",
            "file_not_found",
            "A file or directory doesn't exist. Check:\n"
            "  ls -la <parent_directory>  (does the path exist?)\n"
            "  pwd  (are you in the right directory?)\n"
            "  find . -name '<filename>'  (search for the file)",
        ),
        # --- Permission errors ---
        (
            "permission denied",
            "permission_denied",
            "Permission denied. Try:\n"
            "  chmod +x <file>  (if it needs to be executable)\n"
            "  sudo <command>  (if it needs root)\n"
            "  ls -la <file>  (check current permissions)",
        ),
        # --- Python/pip errors ---
        (
            "externally-managed-environment",
            "pip_managed_env",
            "This Python environment is externally managed (PEP 668). Use:\n"
            "  pip install --break-system-packages <package>\n"
            "  or: pip install --user <package>\n"
            "  or: python3 -m venv /tmp/venv && source /tmp/venv/bin/activate",
        ),
        (
            "modulenotfounderror",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "  pip install --break-system-packages <module_name>  (if managed env)\n"
            "Check the exact package name — it may differ from the import name.",
        ),
        (
            "no module named",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "Check: the pip package name may differ from the import name "
            "(e.g. 'import cv2' → 'pip install opencv-python').",
        ),
        # --- Compilation errors ---
        (
            "fatal error:",
            "compilation",
            "Compilation failed. Check:\n"
            "  1. Read the error — it shows the file and line number.\n"
            "  2. Missing header? Install dev packages: apt-get install -y lib<name>-dev\n"
            "  3. Use: apt-cache search <header_name> to find the right package.",
        ),
        (
            "undefined reference to",
            "linker",
            "Linker error — a symbol is missing. Check:\n"
            "  1. Are you linking all required libraries? (-l<lib> flag)\n"
            "  2. Is the library installed? apt-get install -y lib<name>-dev\n"
            "  3. Check library search path: ldconfig -p | grep <lib>",
        ),
        # --- Git errors ---
        (
            "not a git repository",
            "git",
            "Not in a git repository. Try:\n"
            "  git init  (to create one)\n"
            "  cd <correct_directory>  (you may be in the wrong dir)\n"
            "  find / -name '.git' -type d 2>/dev/null  (find existing repos)",
        ),
        # --- Disk/resource errors ---
        (
            "no space left on device",
            "disk_full",
            "Disk is full. Free space:\n"
            "  df -h  (check disk usage)\n"
            "  du -sh /* 2>/dev/null | sort -rh | head  (find large dirs)\n"
            "  apt-get clean  (clear package cache)\n"
            "  rm -rf /tmp/*  (clear temp files)",
        ),
        (
            "killed",
            "oom",
            "Process was killed (likely out of memory). Try:\n"
            "  free -h  (check available memory)\n"
            "  Reduce memory usage: smaller batch size, fewer workers, etc.\n"
            "  Use swap: fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile",
        ),
    ]

    def __init__(self):
        self._last_guidance_type: str | None = None

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        if tool_name != "run_bash":
            return None

        result_lower = result.lower()

        # Skip if no error indicators
        if "[error]" not in result_lower and "error" not in result_lower and "not found" not in result_lower:
            self._last_guidance_type = None
            return None

        for pattern, guidance_type, suggestion in self.ERROR_PATTERNS:
            if pattern in result_lower:
                # Don't repeat the same guidance type consecutively
                if guidance_type == self._last_guidance_type:
                    return None
                self._last_guidance_type = guidance_type
                log.info(f"Error guidance: matched '{guidance_type}'")
                return f"[SYSTEM] Error detected — here's how to fix it:\n{suggestion}"

        self._last_guidance_type = None
        return None
