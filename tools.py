"""
Tool definitions and execution for agents.
Each tool is an OpenAI function-calling schema + a Python implementation.
Agents operate inside config.WORKSPACE to keep generated code isolated.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import config

# Playwright is optional — only needed for evaluator browser testing
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str) -> Path:
    """Resolve a relative path inside the workspace. Prevent escaping."""
    p = Path(config.WORKSPACE, path).resolve()
    ws = Path(config.WORKSPACE).resolve()
    if not str(p).startswith(str(ws)):
        raise ValueError(f"Path escapes workspace: {path}")
    return p


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[error] File not found: {path}"
    content = p.read_text(encoding="utf-8", errors="replace")
    limit = 40_000
    if len(content) > limit:
        total = len(content)
        content = content[:limit] + (
            f"\n\n[TRUNCATED] Showing {limit} of {total} chars. "
            f"Use run_bash with head/tail/sed to read the rest."
        )
    return content


def read_skill_file(path: str) -> str:
    """Read a file from the skills directory (outside workspace). Path must be relative to project root."""
    project_root = Path(__file__).parent
    p = (project_root / path).resolve()
    # Must stay within the skills directory
    skills_dir = (project_root / "skills").resolve()
    if not str(p).startswith(str(skills_dir)):
        return f"[error] Path must be inside skills/ directory: {path}"
    if not p.exists():
        return f"[error] Skill file not found: {path}"
    return p.read_text(encoding="utf-8", errors="replace")[:60_000]


def write_file(path: str, content: str) -> str:
    if not path or not path.strip():
        return "[error] Empty file path"
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in a file. For modifying existing files — only sends the diff."""
    p = _resolve(path)
    if not p.exists():
        if old_string == "":
            # Creating a new file
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_string, encoding="utf-8")
            return f"Created new file {path} ({len(new_string)} chars)"
        return f"[error] File not found: {path}"

    content = p.read_text(encoding="utf-8", errors="replace")

    if old_string not in content:
        # Try to find a close match and report
        lines_with_match = []
        for i, line in enumerate(content.splitlines(), 1):
            if old_string[:40] in line or (len(old_string) > 10 and old_string[:20] in line):
                lines_with_match.append(f"  line {i}: {line.strip()[:100]}")
        hint = ""
        if lines_with_match:
            hint = "\nPartial matches found:\n" + "\n".join(lines_with_match[:3])
        return (
            f"[error] old_string not found in {path}. "
            f"Make sure it matches EXACTLY (including whitespace/indentation).{hint}"
        )

    count = content.count(old_string)
    if count > 1:
        return (
            f"[error] old_string appears {count} times in {path}. "
            f"Provide more surrounding context to make it unique, "
            f"or use write_file to replace the entire file."
        )

    new_content = content.replace(old_string, new_string, 1)
    p.write_text(new_content, encoding="utf-8")
    return f"Edited {path}: replaced {len(old_string)} chars with {len(new_string)} chars"


def list_files(directory: str = ".") -> str:
    p = _resolve(directory)
    if not p.is_dir():
        return f"[error] Not a directory: {directory}"
    entries = []
    for item in sorted(p.rglob("*")):
        if item.is_file():
            rel = item.relative_to(Path(config.WORKSPACE).resolve())
            entries.append(str(rel))
    if not entries:
        return "(empty)"
    return "\n".join(entries[:200])


def run_bash(command: str, timeout: int = 120) -> str:
    """Run a shell command inside the workspace. Returns stdout+stderr."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=config.WORKSPACE,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = _smart_truncate_output(result.stdout, result.stderr)
        # Prepend exit code for non-zero returns — helps weak models detect failures
        if result.returncode != 0:
            output = f"[exit code: {result.returncode}]\n{output}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return (
            f"[error] Command timed out after {timeout}s. "
            f"If this command legitimately needs more time (e.g. compilation, training), "
            f"retry with a larger timeout parameter."
        )
    except Exception as e:
        return f"[error] {e}"


def _smart_truncate_output(stdout: str, stderr: str, limit: int = 20_000) -> str:
    """Truncate command output while preserving the most useful information.

    Strategy:
    - Always keep stderr in full (up to half the budget) — errors live here.
    - Extract lines containing error/warning keywords from the middle of stdout
      that would otherwise be lost in a naive head+tail cut.
    - Use head + important-middle + tail for stdout.
    """
    import re

    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()
    combined = (stdout + "\n" + stderr).strip() if stderr else stdout

    if len(combined) <= limit:
        return combined

    # Reserve up to 40% of budget for stderr, rest for stdout
    stderr_budget = min(len(stderr), int(limit * 0.4))
    stdout_budget = limit - stderr_budget

    # Truncate stderr if needed (keep tail — most recent errors matter most)
    if len(stderr) > stderr_budget:
        stderr = "...[stderr truncated]\n" + stderr[-(stderr_budget - 30):]

    # Smart-truncate stdout
    if len(stdout) <= stdout_budget:
        truncated_stdout = stdout
    else:
        # Head and tail get 40% each, important middle lines get 20%
        head_size = int(stdout_budget * 0.40)
        tail_size = int(stdout_budget * 0.40)
        middle_budget = stdout_budget - head_size - tail_size - 200  # 200 for markers

        head = stdout[:head_size]
        tail = stdout[-tail_size:]

        # Extract important lines from the middle that would be lost
        middle = stdout[head_size:-tail_size] if tail_size else stdout[head_size:]
        important_lines = []
        _error_pattern = re.compile(
            r'(?i)(error|fail|assert|exception|traceback|warning|not found|denied|refused|fatal)',
        )
        if middle and middle_budget > 0:
            for line in middle.splitlines():
                if _error_pattern.search(line):
                    important_lines.append(line)

        important_section = "\n".join(important_lines)
        if len(important_section) > middle_budget:
            important_section = important_section[:middle_budget]

        middle_part = ""
        if important_section:
            middle_part = (
                f"\n\n[...{len(middle)} chars omitted — key lines extracted:]\n"
                + important_section
                + "\n[...end extracted lines]\n\n"
            )
        else:
            middle_part = (
                f"\n\n[TRUNCATED — {len(middle)} chars omitted from middle]\n\n"
            )

        truncated_stdout = head + middle_part + tail

    if stderr:
        return truncated_stdout + "\n\n--- STDERR ---\n" + stderr
    return truncated_stdout


# ---------------------------------------------------------------------------
# Sub-agent delegation (context isolation)
# ---------------------------------------------------------------------------

def delegate_task(task: str, role: str = "assistant") -> str:
    """
    Spawn a sub-agent in a completely isolated context to handle a subtask.

    The sub-agent gets a clean context window — it does NOT inherit the parent's
    conversation history. It has access to the same workspace and tools.
    Only the structured result comes back to the parent.

    Use this for:
    - Exploring/reading many files without polluting your context
    - Running a series of bash commands and summarizing results
    - Any "dirty work" that would bloat your context window

    The sub-agent's internal reasoning is invisible to the caller.
    """
    # Lazy import to avoid circular dependency
    from agents import Agent

    sub = Agent(
        name=f"sub_{role}",
        system_prompt=(
            f"You are a sub-agent with the role: {role}. "
            f"Complete the assigned task and provide a concise, structured summary of your findings. "
            f"You have access to the workspace files and bash. "
            f"Focus only on the task — do not do extra work.\n"
            f"When done, respond with a clear summary of:\n"
            f"1. What you found or did\n"
            f"2. Key results or artifacts created\n"
            f"3. Any issues encountered"
        ),
        use_tools=True,
    )

    result = sub.run(task)

    if not result:
        return "[sub-agent returned no output]"

    # Truncate to avoid blowing up the parent's context
    if len(result) > 8000:
        result = result[:8000] + "\n...(truncated)"

    return result


# ---------------------------------------------------------------------------
# Playwright browser testing
# ---------------------------------------------------------------------------

# Holds a background dev server process so we can start it once and reuse
_dev_server_proc: subprocess.Popen | None = None


def _ensure_dev_server(start_command: str, port: int, startup_wait: int = 8) -> str:
    """Start a dev server in the background if not already running."""
    global _dev_server_proc
    if _dev_server_proc is not None and _dev_server_proc.poll() is None:
        return f"Dev server already running (pid={_dev_server_proc.pid})"
    _dev_server_proc = subprocess.Popen(
        start_command,
        shell=True,
        cwd=config.WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(startup_wait)
    if _dev_server_proc.poll() is not None:
        stderr = _dev_server_proc.stderr.read().decode(errors="replace")[:2000]
        return f"[error] Dev server exited immediately: {stderr}"
    return f"Dev server started (pid={_dev_server_proc.pid}, port={port})"


def stop_dev_server() -> str:
    """Stop the background dev server."""
    global _dev_server_proc
    if _dev_server_proc is None:
        return "No dev server running"
    _dev_server_proc.terminate()
    try:
        _dev_server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _dev_server_proc.kill()
    _dev_server_proc = None
    return "Dev server stopped"


def browser_test(
    url: str,
    actions: list[dict] | None = None,
    screenshot: bool = True,
    start_command: str | None = None,
    port: int = 5173,
    startup_wait: int = 8,
) -> str:
    """
    Launch a headless browser, navigate to a URL, perform actions, and
    optionally take a screenshot. Returns a text report of what happened.

    actions is a list of dicts, each with:
      - type: "click" | "fill" | "wait" | "evaluate" | "scroll"
      - selector: CSS selector (for click/fill)
      - value: text to type (for fill), JS code (for evaluate)
      - delay: ms to wait (for wait)

    If start_command is provided, starts a dev server first.
    """
    if not HAS_PLAYWRIGHT:
        return (
            "[error] Playwright not installed. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )

    report_lines = []

    # Optionally start dev server
    if start_command:
        srv_result = _ensure_dev_server(start_command, port, startup_wait)
        report_lines.append(f"Server: {srv_result}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})

            # Navigate
            try:
                page.goto(url, timeout=15000)
                report_lines.append(f"Navigated to {url} — title: {page.title()}")
            except Exception as e:
                report_lines.append(f"[error] Navigation failed: {e}")
                browser.close()
                return "\n".join(report_lines)

            # Check for console errors
            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            # Execute actions
            for action in (actions or []):
                action_type = action.get("type", "")
                selector = action.get("selector", "")
                value = action.get("value", "")
                delay = action.get("delay", 1000)

                try:
                    if action_type == "click":
                        page.click(selector, timeout=5000)
                        report_lines.append(f"Clicked: {selector}")
                    elif action_type == "fill":
                        page.fill(selector, value, timeout=5000)
                        report_lines.append(f"Filled '{selector}' with '{value[:50]}'")
                    elif action_type == "wait":
                        page.wait_for_timeout(delay)
                        report_lines.append(f"Waited {delay}ms")
                    elif action_type == "evaluate":
                        result = page.evaluate(value)
                        report_lines.append(f"JS eval result: {str(result)[:500]}")
                    elif action_type == "scroll":
                        page.evaluate(f"window.scrollBy(0, {value or 500})")
                        report_lines.append(f"Scrolled by {value or 500}px")
                    else:
                        report_lines.append(f"[warn] Unknown action type: {action_type}")
                except Exception as e:
                    report_lines.append(f"[error] Action {action_type}('{selector}'): {e}")

                page.wait_for_timeout(300)  # brief pause between actions

            # Gather page info
            report_lines.append(f"Final URL: {page.url}")
            report_lines.append(f"Visible text (first 2000 chars): {page.inner_text('body')[:2000]}")

            if console_errors:
                report_lines.append(f"Console errors ({len(console_errors)}):")
                for err in console_errors[:10]:
                    report_lines.append(f"  - {err[:200]}")

            # Screenshot
            if screenshot:
                ss_path = Path(config.WORKSPACE) / "_screenshot.png"
                page.screenshot(path=str(ss_path), full_page=False)
                report_lines.append(f"Screenshot saved to _screenshot.png")

            browser.close()

    except Exception as e:
        report_lines.append(f"[error] Browser test failed: {e}")

    return "\n".join(report_lines)


# ---------------------------------------------------------------------------
# OpenAI function-calling schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "Read a skill guide from the skills/ directory (e.g. 'skills/frontend-design/SKILL.md').",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path to skill file from project root"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "File content to write"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in a directory recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default: root)",
                        "default": ".",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a shell command in the workspace directory.",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120). Increase for long builds/training.",
                        "default": 120,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Spawn a sub-agent in an isolated context to handle a subtask. Returns only its summary.",
            "parameters": {
                "type": "object",
                "required": ["task"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Detailed description of the subtask to delegate",
                    },
                    "role": {
                        "type": "string",
                        "description": "Role hint (e.g. 'codebase_explorer', 'test_runner')",
                        "default": "assistant",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 5)",
                        "default": 5,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page as text. Use after web_search.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
            },
        },
    },
]

# --- Minimal tool set for TB2 (no network, no sub-agents) ---
# Removes web_search, web_fetch, delegate_task, read_skill_file
# Fewer tools = smaller prompt = faster API calls = more iterations per task

TB2_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "File content to write"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file. Preferred for modifying existing files.",
            "parameters": {
                "type": "object",
                "required": ["path", "old_string", "new_string"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "old_string": {"type": "string", "description": "Exact string to find (must be unique)"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in a directory recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default: root)",
                        "default": ".",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a shell command in the workspace directory.",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120). Increase for long builds/training.",
                        "default": 120,
                    },
                },
            },
        },
    },
]

# --- Evaluator-only tools (browser testing) ---

BROWSER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_test",
            "description": (
                "Launch a headless Chromium browser to test the running application. "
                "Navigates to a URL, performs UI actions (click, fill, scroll, evaluate JS), "
                "captures console errors, and takes a screenshot. "
                "Optionally starts a dev server first via start_command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to (e.g. http://localhost:5173)",
                    },
                    "actions": {
                        "type": "array",
                        "description": "List of browser actions to perform sequentially",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "fill", "wait", "evaluate", "scroll"],
                                    "description": "Action type",
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector (for click/fill)",
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Text for fill, JS code for evaluate, pixels for scroll",
                                },
                                "delay": {
                                    "type": "integer",
                                    "description": "Milliseconds to wait (for wait action)",
                                },
                            },
                        },
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a screenshot after actions (default: true)",
                        "default": True,
                    },
                    "start_command": {
                        "type": "string",
                        "description": "Shell command to start the dev server (e.g. 'npm run dev'). Only needed on first call.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Port the dev server runs on (default: 5173)",
                        "default": 5173,
                    },
                    "startup_wait": {
                        "type": "integer",
                        "description": "Seconds to wait for dev server to start (default: 8)",
                        "default": 8,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_dev_server",
            "description": "Stop the background dev server started by browser_test.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ---------------------------------------------------------------------------
# Tool-call pre-validation & auto-correction
# ---------------------------------------------------------------------------

def _validate_and_fix(name: str, arguments: dict) -> tuple[dict, str | None]:
    """
    Pre-validate tool arguments and auto-correct common mistakes.
    Returns (fixed_arguments, warning_message_or_None).

    This is a lightweight heuristic layer — no LLM calls.
    Catches the most common tool-call errors from weaker models:
      - Empty/missing required arguments
      - Absolute paths that should be relative
      - Obvious typos in common patterns
    """
    warning = None

    if name == "write_file":
        path = arguments.get("path", "")
        content = arguments.get("content")

        # Empty path
        if not path or not path.strip():
            return arguments, "[auto-fix] Empty file path. You must specify a path."

        # Absolute path → make relative to workspace
        if path.startswith("/"):
            import re
            # Strip common workspace prefixes
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if path.startswith(prefix):
                    arguments["path"] = path[len(prefix):]
                    warning = f"[auto-fix] Converted absolute path '{path}' to relative '{arguments['path']}'"
                    break

        # Missing content
        if content is None:
            arguments["content"] = ""
            warning = "[auto-fix] Missing 'content' argument — writing empty file."

    elif name == "read_file":
        path = arguments.get("path", "")

        # Absolute path → relative
        if path.startswith("/"):
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if path.startswith(prefix):
                    arguments["path"] = path[len(prefix):]
                    warning = f"[auto-fix] Converted absolute path '{path}' to relative '{arguments['path']}'"
                    break

    elif name == "run_bash":
        command = arguments.get("command", "")

        # Empty command
        if not command or not command.strip():
            return arguments, "[auto-fix] Empty command. You must specify a command to run."

        # Detect interactive commands that will hang
        import re
        interactive_cmds = ["vim", "nano", "vi", "less", "more", "top", "htop"]
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in interactive_cmds:
            return arguments, (
                f"[auto-fix] '{first_word}' is an interactive command that will hang. "
                f"Use non-interactive alternatives: "
                f"for editing use write_file, for viewing use cat/head/tail."
            )

    elif name == "list_files":
        directory = arguments.get("directory", ".")
        if directory.startswith("/"):
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if directory.startswith(prefix):
                    arguments["directory"] = directory[len(prefix):] or "."
                    warning = f"[auto-fix] Converted absolute path '{directory}' to relative '{arguments['directory']}'"
                    break

    return arguments, warning


# ---------------------------------------------------------------------------
# Web search (lightweight, no external deps)
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return text results.
    Uses DDG's lite HTML endpoint — no API key needed, works in any container.
    """
    import urllib.request
    import urllib.parse
    import re
    import html as html_mod

    try:
        encoded = urllib.parse.urlencode({"q": query})
        url = f"https://lite.duckduckgo.com/lite/?{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")

        # Extract result links (DDG lite uses rel="nofollow" for result links)
        links = re.findall(
            r'<a[^>]*rel="nofollow"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            raw, re.DOTALL
        )

        # Extract snippets (text in <td> cells that aren't links/navigation)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', raw, re.DOTALL)
        snippets = []
        for cell in cells:
            text = re.sub(r'<[^>]+>', '', cell).strip()
            if len(text) > 50 and not text.startswith('http'):
                snippets.append(text)

        results = []
        for i, (href, title) in enumerate(links):
            if i >= max_results:
                break
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', title).strip())
            # Decode DDG redirect URL
            real_url = href
            m = re.search(r'uddg=([^&]+)', href)
            if m:
                real_url = urllib.parse.unquote(m.group(1))
            snippet = snippets[i] if i < len(snippets) else ""
            results.append(f"{i+1}. {title}\n   {real_url}\n   {snippet[:200]}\n")

        if results:
            return f"Search results for: {query}\n\n" + "\n".join(results)

        return f"No results found for: {query}"

    except Exception as e:
        return f"[error] Web search failed: {e}"


def web_fetch(url: str) -> str:
    """Fetch the content of a web page and return as text."""
    import urllib.request
    import re

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        # Strip HTML tags, keep text
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) > 10000:
            text = text[:10000] + "\n\n[TRUNCATED]"

        return text or "(empty page)"

    except Exception as e:
        return f"[error] Web fetch failed: {e}"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

TOOL_DISPATCH = {
    "read_file": read_file,
    "read_skill_file": read_skill_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_files": list_files,
    "run_bash": run_bash,
    "delegate_task": delegate_task,
    "web_search": web_search,
    "web_fetch": web_fetch,
    "browser_test": browser_test,
    "stop_dev_server": stop_dev_server,
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with pre-validation and auto-correction.

    Inspired by Claude Code's tool result handling:
    - Empty results get a marker so the model doesn't get confused
    - Large results get persisted to disk with a preview (prevents context bloat)
    """
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return f"[error] Unknown tool: {name}"

    # Pre-validate and auto-correct arguments
    arguments, fix_warning = _validate_and_fix(name, arguments)

    # If validation returned a blocking error (no fix possible), return it
    if fix_warning and fix_warning.startswith("[auto-fix] Empty"):
        return fix_warning
    if fix_warning and "interactive command" in fix_warning:
        return fix_warning

    try:
        result = fn(**arguments)
    except Exception as e:
        result = f"[error] {type(e).__name__}: {e}"

    # Prepend the auto-fix warning so the model knows what was corrected
    if fix_warning:
        result = f"{fix_warning}\n\n{result}"

    # Claude Code pattern: empty results get a marker
    if not result or (isinstance(result, str) and not result.strip()):
        result = f"({name} completed with no output)"

    # Claude Code pattern: persist large tool results to disk
    if isinstance(result, str) and len(result) > 50_000 and name == "run_bash":
        persisted_path = Path(config.WORKSPACE) / f"_tool_output_{name}.txt"
        try:
            persisted_path.write_text(result, encoding="utf-8")
            preview = result[:2000]
            result = (
                f"Output too large ({len(result)} chars). Full output saved to: "
                f"{persisted_path.name}\n\n"
                f"Preview (first 2000 chars):\n{preview}\n...\n"
                f"Use `cat {persisted_path.name}` or `tail {persisted_path.name}` "
                f"to read specific parts."
            )
        except Exception:
            # If persistence fails, truncate instead
            result = result[:30_000] + f"\n\n[TRUNCATED — {len(result)} total chars]"

    return result
