"""
Tool definitions and execution for agents.
Each tool is an OpenAI function-calling schema + a Python implementation.
Agents operate inside config.WORKSPACE to keep generated code isolated.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import signal
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

import config
from orchestrator.canonical_trace import emit_event
from orchestrator.path_safety import WorkspacePathError, resolve_workspace_path
from orchestrator.run_context import RunContext

# Playwright is optional — only needed for evaluator browser testing
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE: ContextVar[Path | None] = ContextVar("harness_workspace", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("harness_run_id", default=None)


@dataclass(frozen=True)
class ToolResultOutcome:
    """Protocol-level interpretation of a tool result.

    Tool implementations continue to return plain strings for model/API
    compatibility. This value gives tracing, metrics, and completion logic one
    shared way to interpret the status markers embedded in those strings.
    """

    success: bool
    failure_kind: str | None = None
    exit_code: int | None = None


_EXIT_CODE_MARKER = re.compile(r"(?im)^[ \t]*\[exit code:\s*(-?\d+)\][ \t]*$")
_ERROR_MARKER = re.compile(r"(?im)^[ \t]*\[error\](?:[ \t]|$)")


def classify_tool_result(result: object, tool_name: str | None = None) -> ToolResultOutcome:
    """Classify a legacy tool result without requiring execution context.

    ``tool_name`` is accepted so callers can keep the classifier API stable as
    more tool-specific protocols are added. Exit-code markers are recognized
    for all tools because persisted/legacy traces may omit the tool name.
    """
    del tool_name  # Reserved for future tool-specific result protocols.

    if isinstance(result, BaseException):
        return ToolResultOutcome(False, "exception")
    if not isinstance(result, str):
        return ToolResultOutcome(False, "invalid_result")

    exit_match = _EXIT_CODE_MARKER.search(result)
    exit_code = int(exit_match.group(1)) if exit_match else None
    if _ERROR_MARKER.search(result):
        return ToolResultOutcome(False, "error_marker", exit_code)
    if exit_code is not None and exit_code != 0:
        return ToolResultOutcome(False, "nonzero_exit", exit_code)
    return ToolResultOutcome(True, exit_code=exit_code)


def current_workspace() -> Path:
    """Return the workspace for this run, with the legacy global as a fallback."""
    return _WORKSPACE.get() or Path(config.WORKSPACE).resolve()


def activate_workspace(workspace: Path) -> Token[Path | None]:
    """Install a workspace for the current execution context."""
    return _WORKSPACE.set(Path(workspace).resolve())


def reset_workspace(token: Token[Path | None]) -> None:
    """Restore the workspace that was active before ``activate_workspace``."""
    _WORKSPACE.reset(token)


def activate_run_id(run_id: str) -> Token[str | None]:
    """Install the owning run id for processes started in this context."""
    return _RUN_ID.set(str(run_id))


def reset_run_id(token: Token[str | None]) -> None:
    """Restore the run id active before ``activate_run_id``."""
    _RUN_ID.reset(token)


def current_run_id() -> str | None:
    """Return the active run id, if execution is inside a scheduler run."""
    return _RUN_ID.get()

def _resolve(path: str) -> Path:
    """Resolve a relative path inside the workspace. Prevent escaping."""
    try:
        return resolve_workspace_path(current_workspace(), path)
    except WorkspacePathError as exc:
        raise ValueError(str(exc)) from exc


def resolve_for_context(ctx: RunContext, path: str) -> Path:
    try:
        return resolve_workspace_path(ctx.workspace, path)
    except WorkspacePathError as exc:
        raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

_background_procs: list[subprocess.Popen] = []
_processes_by_run: dict[str, list[subprocess.Popen]] = {}
_dev_server_procs_by_run: dict[str, subprocess.Popen] = {}
_windows_jobs_by_process: dict[subprocess.Popen, int] = {}

def read_file(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[error] File not found: {path}"
    binary_error = _binary_file_error(p, path)
    if binary_error:
        return binary_error
    content = p.read_text(encoding="utf-8", errors="replace")
    limit = 40_000
    if len(content) > limit:
        total = len(content)
        content = content[:limit] + (
            f"\n\n[TRUNCATED] Showing {limit} of {total} chars. "
            f"Use run_bash with head/tail/sed to read the rest."
        )
    return content


_BINARY_READ_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".exe", ".dll", ".bin", ".wasm", ".pyc",
}


def _binary_file_error(resolved_path: Path, display_path: str) -> str | None:
    suffix = resolved_path.suffix.lower()
    if suffix in _BINARY_READ_EXTENSIONS:
        size = _safe_file_size(resolved_path)
        kind = "image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"} else "binary"
        hint = " Use the browser_test text report or visual inspection tooling instead." if kind == "image" else ""
        return f"[error] Refusing to read {kind} file as text: {display_path} ({size} bytes).{hint}"

    try:
        sample = resolved_path.read_bytes()[:4096]
    except OSError as exc:
        return f"[error] Could not read file: {display_path}: {exc}"
    if _looks_binary(sample):
        size = _safe_file_size(resolved_path)
        return f"[error] Refusing to read binary file as text: {display_path} ({size} bytes)."
    return None


def _safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text_controls = {7, 8, 9, 10, 12, 13, 27}
    suspicious = sum(1 for byte in sample if byte < 32 and byte not in text_controls)
    return suspicious / len(sample) > 0.02


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
    _emit_runtime_event("workspace_mutated", {"tool": "write_file", "path": path})
    return f"Wrote {len(content)} chars to {path}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in a file. For modifying existing files — only sends the diff."""
    p = _resolve(path)
    if not p.exists():
        if old_string == "":
            # Creating a new file
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_string, encoding="utf-8")
            _emit_runtime_event("workspace_mutated", {"tool": "edit_file", "path": path})
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
    _emit_runtime_event("workspace_mutated", {"tool": "edit_file", "path": path})
    return f"Edited {path}: replaced {len(old_string)} chars with {len(new_string)} chars"


def list_files(directory: str = ".") -> str:
    p = _resolve(directory)
    if not p.is_dir():
        return f"[error] Not a directory: {directory}"
    entries = []
    for item in sorted(p.rglob("*")):
        if item.is_file():
            rel = item.relative_to(current_workspace())
            entries.append(str(rel))
    if not entries:
        return "(empty)"
    return "\n".join(entries[:200])


def run_bash(command: str, timeout: int = 120) -> str:
    """Run a shell command inside the workspace. Returns stdout+stderr."""
    unsafe_message = _unsafe_process_kill_error(command)
    if unsafe_message:
        return unsafe_message

    command, run_in_background = _split_background_command(command)
    if run_in_background or _looks_like_long_running_server(command):
        return _start_background_command(command)

    proc: subprocess.Popen | None = None
    try:
        popen_args, use_shell = _shell_invocation(command)
        proc = subprocess.Popen(
            popen_args,
            shell=use_shell,
            cwd=current_workspace(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_process_group_kwargs(),
        )
        _attach_windows_process_job(proc)
        _register_process(proc)
        _emit_runtime_event("managed_process_started", {"pid": proc.pid, "command": command})
        stdout, stderr = proc.communicate(timeout=timeout)
        output = _smart_truncate_output(stdout, stderr)
        # Prepend exit code for non-zero returns — helps weak models detect failures
        if proc.returncode != 0:
            output = f"[exit code: {proc.returncode}]\n{output}"
        if use_shell and not _bash_executable():
            output = (output or "") + _missing_bash_hint()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        if proc is not None:
            _terminate_process_tree(proc)
        return (
            f"[error] Command timed out after {timeout}s. "
            f"If this command legitimately needs more time (e.g. compilation, training), "
            f"retry with a larger timeout parameter."
        )
    except Exception as e:
        return f"[error] {e}"

    finally:
        if proc is not None:
            _unregister_process(proc)


def _shell_invocation(command: str) -> tuple[list[str] | str, bool]:
    bash = _bash_executable()
    if bash:
        return [bash, "-lc", command], False
    return command, True


def _bash_executable() -> str | None:
    if os.name == "nt":
        for candidate in _windows_bash_candidates():
            if candidate.exists():
                return str(candidate)
        path_bash = shutil.which("bash")
        if path_bash and not _is_windows_wsl_bash(Path(path_bash)):
            return path_bash
        return None

    return shutil.which("bash") or shutil.which("sh")


def _windows_bash_candidates() -> list[Path]:
    return [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "usr" / "bin" / "bash.exe",
    ]


def _is_windows_wsl_bash(path: Path) -> bool:
    normalized = str(path).lower().replace("/", "\\")
    return (
        normalized.endswith(r"\windows\system32\bash.exe")
        or r"\appdata\local\microsoft\windowsapps\bash.exe" in normalized
    )


def _missing_bash_hint() -> str:
    if os.name != "nt":
        return ""
    return (
        "\n[warning] No Git Bash/MSYS bash executable was found, so run_bash "
        "fell back to the Windows shell. Install Git for Windows or put a real "
        "bash.exe on PATH for full bash command compatibility."
    )


def _unsafe_process_kill_error(command: str) -> str | None:
    """Reject broad process kills that can terminate the harness runtime."""
    import re

    normalized = " ".join(command.strip().split()).lower()
    if not normalized:
        return None

    segments = [segment.strip() for segment in re.split(r"[;&|]+", normalized)]
    python_process = r'"?python(?:\d+(?:\.\d+)?)?(?:\.exe)?"?'

    for segment in segments:
        if not segment:
            continue
        words = _shell_words(segment)
        command_name = _direct_command_name(words)
        if command_name == "taskkill" and re.search(rf"(^|\s)/im\s+{python_process}(\s|$)", segment):
            return _unsafe_process_kill_message()
        if command_name == "pkill" and re.search(r"(^|\s)-f(\s|$)", segment) and "python" in segment:
            return _unsafe_process_kill_message()
        if command_name == "pkill" and re.search(rf"(^|\s){python_process}(\s|$)", segment):
            return _unsafe_process_kill_message()
        if command_name == "killall" and re.search(rf"(^|\s){python_process}(\s|$)", segment):
            return _unsafe_process_kill_message()

    return None


def _shell_words(command: str) -> list[str]:
    import shlex

    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _direct_command_name(words: list[str]) -> str:
    while words and words[0] in {"sudo", "command"}:
        words = words[1:]
    if not words:
        return ""
    return Path(words[0]).name


def _unsafe_process_kill_message() -> str:
    return (
        "[error] Refusing to run broad Python process-kill command because it "
        "could terminate the harness, agent runtime, or local Python services. "
        "Use stop_background_commands for processes started by run_bash, or "
        "kill a specific PID instead."
    )


def _split_background_command(command: str) -> tuple[str, bool]:
    """Return command without a trailing shell background marker.

    Agents often use Unix-style `&` for long-running dev servers. On Windows
    cmd.exe treats that as a command separator, so subprocess.run waits forever
    on the server process until the tool timeout. Treat a final single `&` as
    a portable request to run the command in the background.
    """
    stripped = command.rstrip()
    if not stripped.endswith("&") or stripped.endswith("&&"):
        return command, False
    return stripped[:-1].rstrip(), True


def _looks_like_long_running_server(command: str) -> bool:
    normalized = " ".join(command.strip().split()).lower()
    return (
        normalized.startswith("python -m http.server")
        or normalized.startswith("python3 -m http.server")
        or normalized.startswith("py -m http.server")
    )


def _start_background_command(command: str) -> str:
    if not command:
        return "[error] Empty background command"
    try:
        popen_args, use_shell = _shell_invocation(command)
        proc = subprocess.Popen(
            popen_args,
            shell=use_shell,
            cwd=current_workspace(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_process_group_kwargs(),
        )
        _attach_windows_process_job(proc)
        time.sleep(0.5)
        returncode = proc.poll()
        if returncode is not None:
            _release_windows_process_job(proc, terminate=True)
            return f"[exit code: {returncode}]\nBackground command exited immediately."
        _register_process(proc)
        _emit_runtime_event("managed_process_started", {"pid": proc.pid, "command": command})
        return f"Started background command (pid={proc.pid})"
    except Exception as e:
        return f"[error] {e}"


def stop_background_commands() -> str:
    """Stop only commands owned by the current run (or legacy callers)."""
    run_id = current_run_id()
    if run_id:
        return cleanup_run_processes(run_id)

    stopped = 0
    while _background_procs:
        proc = _background_procs.pop()
        if proc.poll() is not None:
            _release_windows_process_job(proc, terminate=True)
            continue
        _terminate_process_tree(proc)
        stopped += 1
        _emit_runtime_event("managed_process_stopped", {"pid": proc.pid})
    return f"Stopped {stopped} background command(s)"


def cleanup_run_processes(run_id: str) -> str:
    """Terminate only live process trees registered to ``run_id``."""
    processes = _processes_by_run.pop(str(run_id), [])
    _dev_server_procs_by_run.pop(str(run_id), None)
    stopped = 0
    for proc in processes:
        if proc.poll() is not None:
            _release_windows_process_job(proc, terminate=True)
            continue
        _terminate_process_tree(proc)
        stopped += 1
        _emit_runtime_event("managed_process_stopped", {"pid": proc.pid})
    _emit_runtime_event("managed_process_cleanup", {"run_id": str(run_id), "stopped": stopped})
    return f"Stopped {stopped} process(es) for run {run_id}"


def _register_process(proc: subprocess.Popen) -> None:
    """Register a process under the active run without inspecting other PIDs."""
    run_id = current_run_id()
    if run_id is None:
        _background_procs.append(proc)
        return
    _processes_by_run.setdefault(run_id, []).append(proc)


def _emit_runtime_event(event_type: str, payload: dict) -> None:
    run_id = current_run_id()
    if run_id:
        emit_event(current_workspace(), run_id, event_type, payload)


def _unregister_process(proc: subprocess.Popen) -> None:
    """Remove a completed foreground process without touching any other process."""
    _release_windows_process_job(proc, terminate=True)
    run_id = current_run_id()
    if run_id is None:
        return
    processes = _processes_by_run.get(run_id)
    if not processes:
        return
    try:
        processes.remove(proc)
    except ValueError:
        return
    if not processes:
        _processes_by_run.pop(run_id, None)


def _process_group_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _attach_windows_process_job(proc: subprocess.Popen) -> bool:
    """Put one managed Windows process tree in its own kill-on-close Job.

    Git Bash can replace or detach from the native PID returned by ``Popen``.
    A Job follows descendants at creation time, so cleanup remains scoped to the
    exact process tree even when the original shell PID is no longer queryable.
    Failure is non-fatal: ``_terminate_process_tree`` retains its PID fallback.
    """
    if os.name != "nt" or proc in _windows_jobs_by_process:
        return False

    job_handle = _create_windows_kill_job()
    if job_handle is None:
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        process_handle = int(proc._handle)  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
            _close_windows_handle(job_handle)
            return False
    except (AttributeError, OSError, TypeError, ValueError):
        _close_windows_handle(job_handle)
        return False

    _windows_jobs_by_process[proc] = job_handle
    return True


def _create_windows_kill_job() -> int | None:
    """Create a Job whose descendants are terminated when its handle closes."""
    if os.name != "nt":
        return None
    job_handle: int | None = None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int

        raw_job_handle = kernel32.CreateJobObjectW(None, None)
        if not raw_job_handle:
            return None
        job_handle = int(raw_job_handle)
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            _close_windows_handle(job_handle)
            return None
        return job_handle
    except (AttributeError, OSError, TypeError, ValueError):
        if job_handle is not None:
            _close_windows_handle(job_handle)
        return None


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _release_windows_process_job(proc: subprocess.Popen, *, terminate: bool) -> bool:
    """Release ``proc``'s Job; report whether requested termination succeeded."""
    job_handle = _windows_jobs_by_process.pop(proc, None)
    if job_handle is None:
        return False
    terminated = not terminate
    try:
        if terminate:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.TerminateJobObject.restype = ctypes.c_int
            terminated = bool(kernel32.TerminateJobObject(job_handle, 1))
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    finally:
        _close_windows_handle(job_handle)
    return terminated


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        used_job = _release_windows_process_job(proc, terminate=True)
        if used_job:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return
        if proc.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return

    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


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

    if result is None:
        return "[sub-agent returned no output]"

    # Agent.run returns a structured outcome. Keep compatibility with test
    # doubles and older Agent implementations that still return a string.
    if isinstance(result, str):
        text = result
        failure_reason = None
    else:
        text = str(getattr(result, "text", "") or "")
        succeeded = bool(getattr(result, "succeeded", False))
        failure_reason = None if succeeded else str(
            getattr(result, "failure_reason", None)
            or getattr(result, "exit_reason", None)
            or "unknown"
        )

    if not text:
        text = "[sub-agent returned no output]"
    if failure_reason:
        text = f"[sub-agent incomplete: {failure_reason}]\n{text}"

    # Truncate to avoid blowing up the parent's context
    if len(text) > 8000:
        text = text[:8000] + "\n...(truncated)"

    return text


# ---------------------------------------------------------------------------
# Playwright browser testing
# ---------------------------------------------------------------------------

# Holds a background dev server process so we can start it once and reuse
_dev_server_proc: subprocess.Popen | None = None


def _ensure_dev_server(start_command: str, port: int, startup_wait: int = 8) -> str:
    """Start a dev server in the background if not already running."""
    global _dev_server_proc
    run_id = current_run_id()
    existing = _dev_server_procs_by_run.get(run_id) if run_id else _dev_server_proc
    if existing is not None:
        if existing.poll() is None:
            return f"Dev server already running (pid={existing.pid})"
        if run_id:
            _unregister_process(existing)
            _dev_server_procs_by_run.pop(run_id, None)
        else:
            _release_windows_process_job(existing, terminate=True)
            _dev_server_proc = None
    proc = subprocess.Popen(
        start_command,
        shell=True,
        cwd=current_workspace(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_process_group_kwargs(),
    )
    _attach_windows_process_job(proc)
    if run_id:
        _dev_server_procs_by_run[run_id] = proc
        _register_process(proc)
    else:
        _dev_server_proc = proc
    time.sleep(startup_wait)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode(errors="replace")[:2000]
        if run_id:
            _unregister_process(proc)
            _dev_server_procs_by_run.pop(run_id, None)
        else:
            _release_windows_process_job(proc, terminate=True)
            _dev_server_proc = None
        return f"[error] Dev server exited immediately: {stderr}"
    return f"Dev server started (pid={proc.pid}, port={port})"


def stop_dev_server() -> str:
    """Stop the background dev server."""
    global _dev_server_proc
    run_id = current_run_id()
    if run_id:
        return cleanup_run_processes(run_id)
    background_result = stop_background_commands()
    if _dev_server_proc is None:
        return f"No dev server running\n{background_result}"
    _terminate_process_tree(_dev_server_proc)
    _dev_server_proc = None
    return f"Dev server stopped\n{background_result}"


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
        if srv_result.startswith("[error]"):
            return "\n".join(report_lines)

    preflight_error = _preflight_http_url(url)
    if preflight_error:
        if not start_command:
            auto_start = _auto_start_static_server_for_url(url, startup_wait=min(startup_wait, 2))
            if auto_start:
                report_lines.append(f"Server: {auto_start}")
                preflight_error = _preflight_http_url(url)
        if preflight_error:
            report_lines.append(preflight_error)
            return "\n".join(report_lines)

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as launch_error:
                browser = None
                fallback_errors = []
                for channel in ("msedge", "chrome"):
                    try:
                        browser = p.chromium.launch(channel=channel, headless=True)
                        report_lines.append(f"Browser: using system {channel} fallback")
                        break
                    except Exception as fallback_error:
                        fallback_errors.append(f"{channel}: {fallback_error}")
                if browser is None:
                    report_lines.append(f"[error] Chromium launch failed: {launch_error}")
                    for fallback_error in fallback_errors:
                        report_lines.append(f"[error] Fallback launch failed: {fallback_error[:300]}")
                    return "\n".join(report_lines)
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
                ss_path = current_workspace() / "_screenshot.png"
                page.screenshot(path=str(ss_path), full_page=False)
                report_lines.append(f"Screenshot saved to _screenshot.png")

            browser.close()

    except Exception as e:
        report_lines.append(f"[error] Browser test failed: {e}")

    return "\n".join(report_lines)


def _preflight_http_url(url: str, timeout: float = 2.0) -> str | None:
    """Fail fast when the target HTTP server or page is unavailable."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None

    try:
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "HarnessBrowserPreflight/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
        return None
    except urllib.error.HTTPError:
        # The server is reachable. Let Playwright load the page so the
        # evaluator can inspect the actual browser-visible error page.
        return None
    except urllib.error.URLError as exc:
        return (
            f"[error] Browser preflight failed: cannot reach {url} within {timeout:g}s "
            f"({exc.reason}). Start the dev server with start_command or use the correct port."
        )
    except TimeoutError:
        return (
            f"[error] Browser preflight failed: cannot reach {url} within {timeout:g}s. "
            "Start the dev server with start_command or use the correct port."
        )
    except Exception as exc:
        return f"[error] Browser preflight failed before Playwright navigation: {exc}"


def _auto_start_static_server_for_url(url: str, startup_wait: int = 2) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        return None

    target = _static_file_for_url(parsed)
    if target is None or not target.exists() or not target.is_file():
        return None

    server_port = parsed.port or 8000
    return _ensure_dev_server(f"python -m http.server {server_port}", server_port, startup_wait)


def _static_file_for_url(parsed_url) -> Path | None:
    raw_path = urllib.parse.unquote(parsed_url.path or "/")
    relative_path = "index.html" if raw_path in {"", "/"} else raw_path.lstrip("/")
    if not relative_path or relative_path.startswith(("../", "..\\")):
        return None
    try:
        return _resolve(relative_path)
    except ValueError:
        return None


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
        persisted_path = current_workspace() / f"_tool_output_{name}.txt"
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
