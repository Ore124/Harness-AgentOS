"""Run analysis helpers for state-driven orchestration."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import config
from orchestrator.verification import is_verification_tool_call

ANALYSIS_FILE = "analysis.json"

_MUTATING_TOOLS = {"write_file", "edit_file", "delegate_task"}
_IGNORED_ARTIFACT_DIRS = {
    ".git",
    ".harness",
    ".cache",
    "cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "venv312",
    "node_modules",
    ".tox",
    ".nox",
    ".next",
    "build",
    "dist",
    "out",
    "target",
    "coverage",
}
_IGNORED_ARTIFACT_NAMES = {
    "harness_state.json",
    ANALYSIS_FILE,
    config.SPEC_FILE,
    config.FEEDBACK_FILE,
    config.CONTRACT_FILE,
    config.PROGRESS_FILE,
    "_screenshot.png",
    "server.log",
    ".env",
}
_FULL_CONTENT_HASH_LIMIT = 8 * 1024 * 1024
_LARGE_FILE_SAMPLE_BYTES = 64 * 1024


def analyze_workspace(workspace: str | Path) -> dict[str, Any]:
    # Imported lazily because tools imports orchestrator.run_context, while the
    # orchestrator package exports Scheduler (which imports this module).
    from tools import classify_tool_result

    root = Path(workspace)
    analysis = {
        "tool_calls": {
            "total": 0,
            "successful": 0,
            "failed": 0,
            "by_tool": {},
        },
        "verification": {"attempts": [], "latest": None},
        "agents": {},
        "errors": [],
        "finish_reasons": {},
        "scores": [],
        "artifacts": list_artifacts(root),
    }

    canonical_attempts = _canonical_verification_attempts(root)
    legacy_attempts: list[dict[str, Any]] = []
    for trace_file in sorted(root.glob("_trace_*.jsonl")):
        agent = trace_file.stem.replace("_trace_", "", 1)
        agent_stats = {"events": 0, "tool_calls": 0, "errors": 0, "last_event": None}
        for ordinal, event in enumerate(_read_jsonl(trace_file), start=1):
            agent_stats["events"] += 1
            agent_stats["last_event"] = event
            if event.get("event") == "tool_call":
                tool = event.get("tool", "unknown")
                agent_stats["tool_calls"] += 1
                analysis["tool_calls"]["total"] += 1
                by_tool = analysis["tool_calls"]["by_tool"]
                by_tool[tool] = by_tool.get(tool, 0) + 1
                arguments = _trace_tool_arguments(event.get("args"))
                outcome = classify_tool_result(event.get("result"), tool_name=tool)
                explicit_success = event.get("success")
                if isinstance(explicit_success, bool):
                    success = explicit_success
                else:
                    success = outcome.success
                outcome_key = "successful" if success else "failed"
                analysis["tool_calls"][outcome_key] += 1
                if is_verification_tool_call(tool, arguments):
                    attempt = {
                        "token": f"legacy:{trace_file.name}:{ordinal}",
                        "ordinal": ordinal,
                        "trace": trace_file.name,
                        "agent": agent,
                        "tool": tool,
                        "command": str(arguments.get("command", ""))[:500],
                        "success": success,
                        "failure_kind": event.get("failure_kind") or outcome.failure_kind,
                        "exit_code": (
                            event.get("exit_code")
                            if event.get("exit_code") is not None
                            else outcome.exit_code
                        ),
                        "stale": False,
                    }
                    legacy_attempts.append(attempt)
                elif _tool_invalidates_verification(tool, arguments, success):
                    _mark_attempts_stale(legacy_attempts, f"{trace_file.name}:{ordinal}")
            elif event.get("event") == "error":
                agent_stats["errors"] += 1
                analysis["errors"].append({
                    "agent": agent,
                    "type": event.get("type"),
                    "message": event.get("message"),
                })
            elif event.get("event") == "finish":
                reason = event.get("reason", "unknown")
                analysis["finish_reasons"][reason] = analysis["finish_reasons"].get(reason, 0) + 1
        analysis["agents"][agent] = agent_stats

    feedback = root / config.FEEDBACK_FILE
    if feedback.exists():
        text = feedback.read_text(encoding="utf-8", errors="replace")
        analysis["scores"] = [float(s) for s in re.findall(r"(\d+\.?\d*)\s*/\s*10", text)]
        analysis["feedback_preview"] = text[:4000]

    progress = root / config.PROGRESS_FILE
    if progress.exists():
        analysis["progress_preview"] = progress.read_text(encoding="utf-8", errors="replace")[:4000]

    all_attempts = canonical_attempts if canonical_attempts is not None else legacy_attempts
    attempt_count_total = len(all_attempts)
    stale_count_total = sum(1 for attempt in all_attempts if attempt.get("stale"))
    attempts = all_attempts[-50:]
    fresh_attempts = [attempt for attempt in attempts if not attempt.get("stale")]
    latest = (
        fresh_attempts[-1]
        if fresh_attempts
        else None
    ) if config.HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER else (attempts[-1] if attempts else None)
    analysis["verification"] = {
        "attempts": attempts,
        "latest": latest,
        "latest_attempt": attempts[-1] if attempts else None,
        "stale_count": sum(1 for attempt in attempts if attempt.get("stale")),
        "attempt_count_total": attempt_count_total,
        "stale_count_total": stale_count_total,
    }

    out_path = root / ANALYSIS_FILE
    out_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return analysis


def persist_analysis(workspace: str | Path, analysis: dict[str, Any]) -> None:
    """Persist an enriched analysis produced by the scheduler."""
    out_path = Path(workspace) / ANALYSIS_FILE
    out_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def workspace_artifact_fingerprint(workspace: str | Path) -> str:
    """Hash task artifacts while excluding Harness control and cache files.

    Paths and file contents are both included, so a same-size edit changes the
    fingerprint.  The result is stable across process restarts.  Files up to
    8 MiB are content-hashed; larger files use bounded head/tail samples plus
    size and mtime so fingerprinting remains inexpensive.
    """
    root = Path(workspace)
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()

    for path in _iter_acceptance_artifacts(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        try:
            stat = path.stat()
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b"\0")
            _update_fingerprint_with_file(digest, path, stat.st_size, stat.st_mtime_ns)
        except OSError:
            # A disappearing file is represented by its path plus this marker;
            # the next refresh will still detect a stable state change.
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def _iter_acceptance_artifacts(root: Path):
    """Yield files deterministically without descending into ignored trees."""
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory.lower() not in _IGNORED_ARTIFACT_DIRS
        )
        current_path = Path(current)
        for name in sorted(files):
            path = current_path / name
            if _is_acceptance_artifact(path, root):
                yield path


def _update_fingerprint_with_file(
    digest: Any,
    path: Any,
    size: int,
    mtime_ns: int,
) -> None:
    """Hash a file with bounded I/O for large generated/data artifacts."""
    with path.open("rb") as handle:
        if size <= _FULL_CONTENT_HASH_LIMIT:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            return

        # Large artifacts are sampled rather than read end-to-end.  Metadata
        # detects ordinary middle-only rewrites, while head/tail samples catch
        # replacements that preserve size and coarse timestamps.
        digest.update(str(mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(handle.read(_LARGE_FILE_SAMPLE_BYTES))
        handle.seek(max(0, size - _LARGE_FILE_SAMPLE_BYTES))
        digest.update(handle.read(_LARGE_FILE_SAMPLE_BYTES))


def list_artifacts(workspace: str | Path) -> list[dict[str, Any]]:
    root = Path(workspace)
    if not root.exists():
        return []
    names = [
        config.SPEC_FILE,
        config.FEEDBACK_FILE,
        config.CONTRACT_FILE,
        config.PROGRESS_FILE,
        "_screenshot.png",
        ANALYSIS_FILE,
    ]
    artifacts = []
    for name in names:
        path = root / name
        if path.exists():
            artifacts.append({
                "name": name,
                "path": str(path),
                "size": path.stat().st_size,
            })
    for path in sorted(root.glob("_trace_*.jsonl")):
        artifacts.append({
            "name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
        })
    known = {item["name"] for item in artifacts}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        rel_name = str(rel)
        if rel_name in known:
            continue
        if rel.parts and rel.parts[0] in {".git", "__pycache__", ".pytest_cache"}:
            continue
        if path.name in {"harness_state.json", "analysis.json"} or path.name.startswith(".harness_state.json."):
            continue
        if path.name.endswith((".pyc", ".tmp")):
            continue
        try:
            artifacts.append({
                "name": rel_name,
                "path": str(path),
                "size": path.stat().st_size,
            })
        except OSError:
            continue
    return artifacts


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _trace_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _canonical_verification_attempts(root: Path) -> list[dict[str, Any]] | None:
    trace = root / ".harness" / "canonical_trace.jsonl"
    if not trace.exists():
        return None

    requests: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    for fallback_ordinal, event in enumerate(_read_jsonl(trace), start=1):
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        tool_call_id = str(payload.get("tool_call_id") or "")
        # Physical JSONL position stays monotonic even if a restarted trace
        # writer begins its in-memory ``seq`` counter again.
        ordinal = fallback_ordinal
        if event_type == "tool_requested" and tool_call_id:
            request = {
                "token": str(event.get("event_id") or f"canonical:{ordinal}:{tool_call_id}"),
                "ordinal": ordinal,
                "trace_seq": event.get("seq"),
                "agent": event.get("role") or "unknown",
                "tool": str(payload.get("tool") or "unknown"),
                "command": str(payload.get("command") or "")[:500],
                "path": payload.get("path"),
            }
            requests[tool_call_id] = request
            # delegate_task may perform arbitrary nested writes.  Mark evidence
            # stale at dispatch so verification produced by the delegate later
            # in the canonical stream can itself become the fresh evidence.
            if request["tool"] == "delegate_task":
                _mark_attempts_stale(attempts, f"canonical:{ordinal}")
            continue

        if event_type not in {"tool_completed", "tool_failed"} or not tool_call_id:
            continue
        request = requests.get(tool_call_id)
        if not request:
            continue
        success = bool(payload.get("success", event_type == "tool_completed"))
        arguments = {"command": request["command"], "path": request.get("path")}
        if is_verification_tool_call(request["tool"], arguments):
            attempts.append({
                "token": request["token"],
                "ordinal": request["ordinal"],
                "trace": ".harness/canonical_trace.jsonl",
                "agent": request["agent"],
                "tool": request["tool"],
                "command": request["command"],
                "success": success,
                "failure_kind": payload.get("failure_kind"),
                "exit_code": payload.get("exit_code"),
                "stale": False,
            })
        elif request["tool"] != "delegate_task" and _tool_invalidates_verification(
            request["tool"], arguments, success
        ):
            _mark_attempts_stale(attempts, f"canonical:{ordinal}")
    return attempts


def _mark_attempts_stale(attempts: list[dict[str, Any]], marker: str) -> None:
    for attempt in attempts:
        if not attempt.get("stale"):
            attempt["stale"] = True
            attempt["stale_after"] = marker


def _tool_invalidates_verification(tool: str, arguments: dict[str, Any], success: bool) -> bool:
    if not success or tool not in _MUTATING_TOOLS:
        return False
    if tool == "delegate_task":
        return True
    path = str(arguments.get("path") or "")
    return not _is_control_artifact_path(path)


def _is_control_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip().lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        not normalized
        or name in {item.lower() for item in _IGNORED_ARTIFACT_NAMES}
        or name.startswith("_trace_") and name.endswith(".jsonl")
        or name.startswith(".harness_state.json.")
        or any(part in _IGNORED_ARTIFACT_DIRS for part in normalized.split("/")[:-1])
    )


def _is_acceptance_artifact(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    lowered_parts = tuple(part.lower() for part in relative.parts)
    if any(part in _IGNORED_ARTIFACT_DIRS for part in lowered_parts[:-1]):
        return False
    name = lowered_parts[-1]
    if name in {item.lower() for item in _IGNORED_ARTIFACT_NAMES}:
        return False
    if name.startswith("_trace_") and name.endswith(".jsonl"):
        return False
    if name.startswith(".harness_state.json."):
        return False
    if name.endswith((".pyc", ".tmp")):
        return False
    return True
