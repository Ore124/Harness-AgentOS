"""Deterministic failure evidence extraction for retry guidance."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

FAILURE_TYPES = {
    "test_failure",
    "syntax_error",
    "dependency_error",
    "build_failure",
    "runtime_error",
    "timeout",
    "browser_failure",
    "unknown",
}


def build_failure_evidence(
    state: dict[str, Any],
    *,
    phase: str | None = None,
    feedback_text: str = "",
    error: BaseException | dict[str, Any] | None = None,
    retry_goal: str | None = None,
    max_evidence_items: int = 8,
) -> dict[str, Any]:
    """Create a compact, deterministic evidence object from existing run data."""
    workspace = Path(state["workspace"])
    snippets = _collect_snippets(workspace, feedback_text, error)
    primary = _primary_error(snippets, error)
    failure_type = _classify_failure(primary, snippets, phase)
    failed_checks = _extract_failed_checks("\n".join(snippets))
    suspected_files = _extract_file_refs("\n".join(snippets), workspace)
    recent_changed_files = _recent_changed_files(workspace)
    for file_name in recent_changed_files:
        if file_name not in suspected_files and _looks_relevant(file_name, snippets):
            suspected_files.append(file_name)

    signature = _signature(failure_type, failed_checks, primary)
    same_count = _same_failure_count(state, signature)
    strategy = _strategy_for_count(same_count)
    evidence_items = [_clean_line(item) for item in snippets if _clean_line(item)]
    evidence_items = _dedupe(evidence_items)[:max_evidence_items]

    return {
        "failure_type": failure_type,
        "failure_signature": signature,
        "failed_checks": failed_checks[:8],
        "evidence": evidence_items,
        "suspected_files": suspected_files[:12],
        "recent_changed_files": recent_changed_files[:20],
        "retry_goal": retry_goal or _default_retry_goal(failure_type, failed_checks, primary),
        "same_failure_count": same_count,
        "recovery_strategy": strategy,
    }


def append_failure_evidence(state: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    history = state.setdefault("failure_evidence_history", [])
    history.append(evidence)
    if len(history) > 20:
        del history[:-20]
    state["current_failure_evidence"] = evidence
    recovery = state.setdefault("recovery", {})
    recovery["failed_attempt_count"] = int(recovery.get("failed_attempt_count") or 0) + 1
    recovery["repeated_failure_count"] = int(
        sum(1 for item in history if int(item.get("same_failure_count") or 0) > 1)
    )
    if int(evidence.get("same_failure_count") or 0) >= 3:
        recovery["same_failure_escalation_count"] = int(recovery.get("same_failure_escalation_count") or 0) + 1
    return state


def mark_recovery_attempt(state: dict[str, Any]) -> dict[str, Any]:
    recovery = state.setdefault("recovery", {})
    recovery["recovery_attempt_count"] = int(recovery.get("recovery_attempt_count") or 0) + 1
    return state


def render_retry_context(state: dict[str, Any], prev_feedback: str = "") -> str:
    evidence = state.get("current_failure_evidence") or {}
    if not evidence:
        return prev_feedback

    lines = [
        "# Evidence-Guided Recovery",
        "",
        "Use this targeted retry context instead of replaying old logs.",
        "",
        "## Current Failure Evidence",
        "```json",
        json.dumps(evidence, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Repair Instructions",
        f"- Recovery strategy: {evidence.get('recovery_strategy', 'targeted_fix')}",
        f"- Retry goal: {evidence.get('retry_goal', 'Fix the current failure and verify the task.')}",
        "- Focus first on suspected_files and recent_changed_files.",
        "- Do not reintroduce already-resolved failures or copy large historical logs into your reasoning.",
    ]
    strategy = evidence.get("recovery_strategy")
    if strategy == "reinspect_assumptions":
        lines.extend([
            "- Do not repeat the previous repair path unchanged.",
            "- Reinspect the relevant implementation, tests, and assumptions before editing.",
        ])
    elif strategy == "escalate_analysis":
        lines.extend([
            "- Stop repeating the same fix path.",
            "- Broaden inspection through the existing analyze/recovery path and verify the root cause before editing.",
        ])

    if prev_feedback:
        lines.extend([
            "",
            "## Latest Feedback Excerpt",
            _trim(prev_feedback, 1200),
        ])
    return "\n".join(lines) + "\n"


def _collect_snippets(workspace: Path, feedback_text: str, error: BaseException | dict[str, Any] | None) -> list[str]:
    snippets: list[str] = []
    if error:
        if isinstance(error, dict):
            snippets.append(f"{error.get('type', 'Error')}: {error.get('message', '')}")
        else:
            snippets.append(f"{type(error).__name__}: {error}")
    if feedback_text:
        snippets.extend(_interesting_lines(feedback_text))
    for trace_path in sorted(workspace.glob("_trace_*.jsonl")):
        snippets.extend(_trace_failure_lines(trace_path))
    return _dedupe(snippets)


def _trace_failure_lines(path: Path) -> list[str]:
    out: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "error":
            out.append(f"{event.get('type', 'Error')}: {event.get('message', '')}")
        result = str(event.get("result") or "")
        if _contains_failure_marker(result):
            tool = event.get("tool") or "tool"
            out.append(f"{tool}: {_trim(result, 1200)}")
    return out


def _interesting_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if _contains_failure_marker(line):
            lines.append(line)
    if lines:
        return lines[:20]
    return [line for line in text.splitlines() if line.strip()][:8]


def _contains_failure_marker(text: str) -> bool:
    lowered = str(text).lower()
    return any(marker in lowered for marker in [
        "[exit code:",
        "[error]",
        "traceback",
        "assert",
        "failed",
        "failure",
        "exception",
        "error:",
        "syntaxerror",
        "modulenotfounderror",
        "timeout",
        "timed out",
    ])


def _classify_failure(primary: str, snippets: list[str], phase: str | None) -> str:
    text = "\n".join([primary, *snippets]).lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "syntaxerror" in text or "parse error" in text or "unexpected token" in text:
        return "syntax_error"
    if "modulenotfounderror" in text or "no module named" in text or "cannot find module" in text or "importerror" in text:
        return "dependency_error"
    if "playwright" in text or "browser" in text or "page.goto" in text:
        return "browser_failure"
    if re.search(r"(^|\b)(pytest|unittest|assertionerror|assert|tests? failed|failed tests?)\b", text):
        return "test_failure"
    if "make:" in text or "build failed" in text or "compilation terminated" in text or "error:" in text and phase == "build":
        return "build_failure"
    if "traceback" in text or "exception" in text or "runtimeerror" in text:
        return "runtime_error"
    return "unknown"


def _extract_failed_checks(text: str) -> list[str]:
    checks: list[str] = []
    patterns = [
        r"FAILED\s+([^\s:]+(?:::[^\s:]+)*)",
        r"([A-Za-z_][\w./-]*::[A-Za-z_][\w.-]*)\s+FAILED",
        r"FAIL:\s+([A-Za-z_][\w.:-]*)",
        r"AssertionError:\s*([^\n]+)",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text):
            checks.append(_clean_line(match))
    return _dedupe(checks)


def _extract_file_refs(text: str, workspace: Path) -> list[str]:
    refs: list[str] = []
    patterns = [
        r"([A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|c|cpp|h|hpp|html|css|json|yaml|yml|toml|md))(?::\d+)?",
        r'File "([^"]+)", line \d+',
    ]
    for pattern in patterns:
        for raw in re.findall(pattern, text):
            ref = str(raw).replace("\\", "/")
            if _is_temp_path(ref):
                continue
            refs.append(_workspace_relative(ref, workspace))
    return _dedupe([ref for ref in refs if ref])


def _recent_changed_files(workspace: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    files = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path:
            files.append(path.replace("\\", "/"))
    return _dedupe(files)


def _primary_error(snippets: list[str], error: BaseException | dict[str, Any] | None) -> str:
    if error:
        if isinstance(error, dict):
            return _clean_line(f"{error.get('type', 'Error')}: {error.get('message', '')}")
        return _clean_line(f"{type(error).__name__}: {error}")
    for snippet in snippets:
        if _contains_failure_marker(snippet):
            return _clean_line(snippet)
    return _clean_line(snippets[0]) if snippets else "unknown failure"


def _signature(failure_type: str, failed_checks: list[str], primary: str) -> str:
    normalized = _normalize_for_signature(primary)
    check = failed_checks[0] if failed_checks else ""
    raw = f"{failure_type}|{check}|{normalized}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{failure_type}:{digest}"


def _normalize_for_signature(text: str) -> str:
    value = text.lower()
    value = re.sub(r"\d{4}-\d{2}-\d{2}[t ][\d:.+-]+", "<timestamp>", value)
    value = re.sub(r"[a-f0-9]{8,}", "<id>", value)
    value = re.sub(r"([a-z]:)?[/\\][^\s:]+", "<path>", value)
    value = re.sub(r":\d+\b", ":<line>", value)
    value = re.sub(r"\b\d+\b", "<num>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:500]


def _same_failure_count(state: dict[str, Any], signature: str) -> int:
    history = state.get("failure_evidence_history") or []
    return 1 + sum(1 for item in history if item.get("failure_signature") == signature)


def _strategy_for_count(count: int) -> str:
    if count <= 1:
        return "targeted_fix"
    if count == 2:
        return "reinspect_assumptions"
    return "escalate_analysis"


def _default_retry_goal(failure_type: str, failed_checks: list[str], primary: str) -> str:
    check = f" `{failed_checks[0]}`" if failed_checks else ""
    if failure_type == "test_failure":
        return f"Fix the failing test/check{check} and rerun the focused verification."
    if failure_type == "syntax_error":
        return "Fix the syntax error, then rerun the build or test command that exposed it."
    if failure_type == "dependency_error":
        return "Resolve the missing dependency/import issue without masking the underlying failure."
    if failure_type == "timeout":
        return "Identify why the previous command timed out and choose a bounded verification path."
    return f"Fix the primary failure and verify it: {_trim(primary, 180)}"


def _looks_relevant(file_name: str, snippets: list[str]) -> bool:
    stem = Path(file_name).stem.lower()
    text = "\n".join(snippets).lower()
    return bool(stem and stem in text)


def _workspace_relative(ref: str, workspace: Path) -> str:
    path = Path(ref)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            return path.name
    return ref.lstrip("./")


def _is_temp_path(ref: str) -> bool:
    lowered = ref.lower()
    return "/tmp/" in lowered or "\\tmp\\" in lowered or "temp/" in lowered or "temporary" in lowered


def _clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip())


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _trim(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[truncated]...\n" + text[-limit // 2 :]
