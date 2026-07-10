"""Small JSON-backed memory store for routing feedback."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_PROFILES = {"app-builder", "terminal", "swe-bench", "reasoning"}
MEMORY_VERSION = 2
DEFAULT_SHORT_TERM_MAX_RUNS = 50
FAILURE_REASONS = {
    "api_error",
    "low_score",
    "timeout",
    "tool_missing",
    "browser_unavailable",
    "tests_failed",
    "route_mismatch",
    "agent_stalled",
    "unknown",
}


class MemoryStore:
    """Tracks coarse historical profile performance.

    The store intentionally keeps only aggregated data. It is used to nudge
    routing confidence, not to replace explicit user choices.
    """

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = os.environ.get("HARNESS_MEMORY_FILE")
        self.path = Path(path) if path else Path(__file__).resolve().parent.parent / ".harness_memory.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _default_memory()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _default_memory()
        return _normalize_memory(raw)

    def save(self, data: dict[str, Any]) -> None:
        data = _normalize_memory(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def adjust_candidates(
        self,
        candidates: list[dict[str, Any]],
        prompt: str,
        task_type: str = "unclear",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Apply short-term and long-term memory adjustments.

        Memory only nudges confidence. It never overrides manual profile
        choices, and deltas are intentionally small to keep route decisions
        explainable and reviewable.
        """
        data = self.load()
        short_term_refs: list[str] = []
        long_term_refs: list[str] = []
        memory_adjustments: list[dict[str, Any]] = []

        short_term_deltas, short_term_refs = _short_term_deltas(data, task_type)
        long_term_deltas, long_term_refs = _long_term_deltas(data, task_type)

        adjusted: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            profile = item.get("profile")
            short_delta = short_term_deltas.get(profile, 0.0)
            long_delta = long_term_deltas.get(profile, 0.0)
            delta = short_delta + long_delta
            if delta:
                before = float(item.get("confidence", 0.0))
                after = max(0.0, min(1.0, before + delta))
                item["confidence"] = round(after, 3)
                item["reason"] = f"{item.get('reason', '')} Memory adjusted by {delta:+.2f}."
                memory_adjustments.append({
                    "profile": profile,
                    "short_term_delta": round(short_delta, 3),
                    "long_term_delta": round(long_delta, 3),
                    "total_delta": round(delta, 3),
                    "before": round(before, 3),
                    "after": round(after, 3),
                })
            adjusted.append(item)
        adjusted.sort(key=lambda c: c.get("confidence", 0.0), reverse=True)

        info = {
            "memory_refs": short_term_refs + long_term_refs,
            "short_term_refs": short_term_refs,
            "long_term_refs": long_term_refs,
            "memory_adjustments": memory_adjustments,
            "strategy_hints": _strategy_hints(data, task_type, adjusted),
        }
        return adjusted, info

    def record_run(self, state: dict[str, Any], analysis: dict[str, Any]) -> None:
        profile = state.get("profile")
        if profile not in VALID_PROFILES:
            return

        data = self.load()
        task_type = _task_type_from_state(state)
        score_history = state.get("score_history") or []
        final_score = float(score_history[-1]) if score_history else 0.0
        passed = _run_passed(state, final_score, bool(score_history))
        tool_calls = int(analysis.get("tool_calls", {}).get("total", 0) or 0)
        failure_reason = normalize_failure_reason(
            state.get("last_error"),
            final_score,
            passed,
            bool(score_history),
            analysis,
        )

        run_entry = {
            "run_id": state.get("run_id"),
            "prompt_preview": _safe_preview(state.get("prompt", "")),
            "task_type": task_type,
            "profile": profile,
            "status": state.get("status"),
            "final_score": final_score,
            "passed": passed,
            "last_error": _safe_preview(str(state.get("last_error") or "")),
            "failure_reason": failure_reason,
            "tool_calls": tool_calls,
            "created_at": state.get("created_at") or _now_iso(),
        }

        short_term = data.setdefault("short_term", {"max_runs": DEFAULT_SHORT_TERM_MAX_RUNS, "runs": []})
        runs = short_term.setdefault("runs", [])
        runs.append(run_entry)
        max_runs = int(short_term.get("max_runs", DEFAULT_SHORT_TERM_MAX_RUNS) or DEFAULT_SHORT_TERM_MAX_RUNS)
        if len(runs) > max_runs:
            del runs[:-max_runs]

        long_term = data.setdefault("long_term", {"task_types": {}, "global_profiles": {}})
        _update_profile_stats(
            long_term.setdefault("global_profiles", {}),
            profile,
            final_score,
            passed,
            tool_calls,
            failure_reason,
        )
        task_type_bucket = (
            long_term
            .setdefault("task_types", {})
            .setdefault(task_type, {"profiles": {}})
        )
        _update_profile_stats(
            task_type_bucket.setdefault("profiles", {}),
            profile,
            final_score,
            passed,
            tool_calls,
            failure_reason,
        )
        _refresh_strategy_hints(data, task_type, profile)

        self.save(data)


def _default_memory() -> dict[str, Any]:
    return {
        "version": MEMORY_VERSION,
        "short_term": {
            "max_runs": DEFAULT_SHORT_TERM_MAX_RUNS,
            "runs": [],
        },
        "long_term": {
            "task_types": {},
            "global_profiles": {},
        },
        "strategy_hints": {},
    }


def _normalize_memory(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version") == MEMORY_VERSION:
        normalized = _default_memory()
        normalized.update(data)
        normalized["short_term"] = {
            **_default_memory()["short_term"],
            **normalized.get("short_term", {}),
        }
        normalized["long_term"] = {
            **_default_memory()["long_term"],
            **normalized.get("long_term", {}),
        }
        normalized["strategy_hints"] = dict(normalized.get("strategy_hints", {}) or {})
        return normalized

    migrated = _default_memory()
    for profile, stats in data.get("profiles", {}).items():
        if profile in VALID_PROFILES:
            migrated["long_term"]["global_profiles"][profile] = _normalize_stats(stats)

    legacy_runs = data.get("runs", [])
    for run in legacy_runs[-DEFAULT_SHORT_TERM_MAX_RUNS:]:
        migrated["short_term"]["runs"].append({
            "run_id": run.get("run_id"),
            "prompt_preview": run.get("prompt", "")[:300],
            "task_type": run.get("task_type", "unclear"),
            "profile": run.get("profile"),
            "status": run.get("status"),
            "final_score": float(run.get("score", 0.0) or 0.0),
            "passed": bool(run.get("status") == "completed"),
            "last_error": run.get("last_error"),
            "failure_reason": _normalize_reason_name(run.get("failure_reason")),
            "tool_calls": int(run.get("tool_calls", 0) or 0),
            "created_at": run.get("created_at") or _now_iso(),
        })
    return migrated


def _normalize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempts": int(stats.get("attempts", 0) or 0),
        "successes": int(stats.get("successes", 0) or 0),
        "average_score": float(stats.get("average_score", 0.0) or 0.0),
        "average_tool_calls": float(stats.get("average_tool_calls", 0.0) or 0.0),
        "failure_reasons": dict(stats.get("failure_reasons", {}) or {}),
        "last_error": stats.get("last_error"),
        "last_used_at": stats.get("last_used_at"),
    }


def _short_term_deltas(data: dict[str, Any], task_type: str) -> tuple[dict[str, float], list[str]]:
    runs = [
        run for run in data.get("short_term", {}).get("runs", [])
        if run.get("task_type") == task_type and run.get("profile") in VALID_PROFILES
    ][-10:]
    if not runs:
        return {}, []

    by_profile: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_profile.setdefault(run["profile"], []).append(run)

    deltas: dict[str, float] = {}
    refs: list[str] = []
    for profile, profile_runs in by_profile.items():
        successes = sum(1 for run in profile_runs if run.get("passed"))
        failures = len(profile_runs) - successes
        delta = max(-0.06, min(0.06, (successes - failures) * 0.025))
        if delta:
            deltas[profile] = delta
            refs.append(
                f"short:{task_type}/{profile}: recent_successes={successes}, "
                f"recent_failures={failures}, delta={delta:+.2f}"
            )
    return deltas, refs


def _long_term_deltas(data: dict[str, Any], task_type: str) -> tuple[dict[str, float], list[str]]:
    profiles = (
        data.get("long_term", {})
        .get("task_types", {})
        .get(task_type, {})
        .get("profiles", {})
    )
    deltas: dict[str, float] = {}
    refs: list[str] = []
    for profile, raw_stats in profiles.items():
        if profile not in VALID_PROFILES:
            continue
        stats = _normalize_stats(raw_stats)
        attempts = stats["attempts"]
        if attempts < 3:
            continue
        success_rate = stats["successes"] / attempts
        average_score = stats["average_score"]
        delta = (success_rate - 0.5) * 0.08 + ((average_score - 7.0) / 10.0) * 0.04
        delta = max(-0.10, min(0.10, delta))
        if delta:
            deltas[profile] = delta
            refs.append(
                f"long:{task_type}/{profile}: attempts={attempts}, "
                f"success_rate={success_rate:.2f}, avg={average_score:.1f}, delta={delta:+.2f}"
            )
    return deltas, refs


def _update_profile_stats(
    profiles: dict[str, Any],
    profile: str,
    final_score: float,
    passed: bool,
    tool_calls: int,
    failure_reason: str | None,
) -> None:
    stats = _normalize_stats(profiles.get(profile, {}))
    attempts = stats["attempts"]
    stats["attempts"] = attempts + 1
    stats["successes"] += 1 if passed else 0
    stats["average_score"] = round(((stats["average_score"] * attempts) + final_score) / (attempts + 1), 2)
    stats["average_tool_calls"] = round(((stats["average_tool_calls"] * attempts) + tool_calls) / (attempts + 1), 2)
    stats["last_used_at"] = _now_iso()
    if not passed and failure_reason:
        reasons = stats.setdefault("failure_reasons", {})
        normalized_reason = _normalize_reason_name(failure_reason)
        reasons[normalized_reason] = int(reasons.get(normalized_reason, 0) or 0) + 1
    profiles[profile] = stats


def _task_type_from_state(state: dict[str, Any]) -> str:
    route_decision = state.get("route_decision") or {}
    task_type = route_decision.get("task_type") or state.get("task_type") or "unclear"
    return str(task_type)


SENSITIVE_MARKERS = ("api_key", "apikey", "authorization", "bearer ", "password", "secret", "token")


def _safe_preview(value: str, limit: int = 300) -> str:
    if any(marker in value.lower() for marker in SENSITIVE_MARKERS):
        return "[redacted]"
    return value[:limit]


def _run_passed(state: dict[str, Any], final_score: float = 0.0, has_score: bool = False) -> bool:
    if state.get("status") != "completed":
        return False
    if not has_score:
        return state.get("validated") is True
    return final_score >= 7.0


def normalize_failure_reason(
    last_error: Any,
    final_score: float,
    passed: bool,
    has_score: bool,
    analysis: dict[str, Any] | None = None,
) -> str | None:
    if passed:
        return None
    analysis = analysis or {}
    error_text = ""
    if isinstance(last_error, dict):
        error_text = f"{last_error.get('type', '')} {last_error.get('message', '')}".lower()
    elif last_error:
        error_text = str(last_error).lower()

    analysis_text = json.dumps(analysis, ensure_ascii=False).lower()
    combined = f"{error_text} {analysis_text}"

    if any(term in combined for term in ["rate_limit", "429", "api_error", "authentication", "openai"]):
        return "api_error"
    if any(term in combined for term in ["timeout", "timed out", "time budget"]):
        return "timeout"
    if any(term in combined for term in ["playwright", "chromium", "browser_test", "executable doesn't exist"]):
        return "browser_unavailable"
    if any(term in combined for term in ["command not found", "modulenotfounderror", "no module named", "missing tool"]):
        return "tool_missing"
    if any(term in combined for term in ["pytest", "unittest", "assert", "test failed", "tests failed", "failed"]):
        return "tests_failed"
    if any(term in combined for term in ["route mismatch", "wrong profile", "profile mismatch"]):
        return "route_mismatch"
    if any(term in combined for term in ["stall", "no progress", "max_iterations", "agent_stalled"]):
        return "agent_stalled"
    if has_score and final_score < 7.0:
        return "low_score"
    tool_call_total = int(analysis.get("tool_calls", {}).get("total", 0) or 0)
    if tool_call_total or analysis.get("tool_calls", {}).get("by_tool"):
        return "low_score"
    return "unknown"


def _normalize_reason_name(reason: Any) -> str:
    if not reason:
        return "unknown"
    normalized = str(reason).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in FAILURE_REASONS else "unknown"


def _refresh_strategy_hints(data: dict[str, Any], task_type: str, profile: str) -> None:
    stats = (
        data.get("long_term", {})
        .get("task_types", {})
        .get(task_type, {})
        .get("profiles", {})
        .get(profile, {})
    )
    stats = _normalize_stats(stats)
    attempts = stats["attempts"]
    failure_reasons = stats.get("failure_reasons", {})
    hints = []
    for reason, count in sorted(failure_reasons.items(), key=lambda item: item[1], reverse=True):
        if count < 2 and attempts < 4:
            continue
        hint = _hint_for_failure(task_type, profile, reason)
        if not hint:
            continue
        confidence = min(0.95, 0.55 + (count / max(attempts, 1)) * 0.4)
        hints.append({
            "task_type": task_type,
            "profile": profile,
            "failure_reason": reason,
            "hint": hint,
            "source": "failure_pattern",
            "confidence": round(confidence, 3),
            "support": {"count": count, "attempts": attempts},
            "updated_at": _now_iso(),
        })

    strategy_hints = data.setdefault("strategy_hints", {})
    task_hints = strategy_hints.setdefault(task_type, {})
    if hints:
        task_hints[profile] = hints[:3]
    else:
        task_hints.pop(profile, None)
    if not task_hints:
        strategy_hints.pop(task_type, None)


def _strategy_hints(data: dict[str, Any], task_type: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    task_hints = data.get("strategy_hints", {}).get(task_type, {})
    hints: list[dict[str, Any]] = []
    candidate_profiles = [candidate.get("profile") for candidate in candidates]
    for profile in candidate_profiles:
        for hint in task_hints.get(profile, []):
            hints.append(dict(hint))
    hints.sort(key=lambda item: item.get("confidence", 0.0), reverse=True)
    return hints[:5]


def _hint_for_failure(task_type: str, profile: str, reason: str) -> str | None:
    reason = _normalize_reason_name(reason)
    if reason == "browser_unavailable":
        return (
            "Playwright Chromium is unavailable on this machine; install it or prefer "
            "static checks and lightweight server smoke tests until browser testing works."
        )
    if reason == "tests_failed":
        return "Prior runs failed tests; run the smallest relevant test early and keep fixes minimal."
    if reason == "tool_missing":
        return "A required tool or package was missing; probe the environment before choosing an implementation path."
    if reason == "timeout":
        return "Prior runs timed out; reduce planning overhead, verify incrementally, and avoid long exploratory commands."
    if reason == "api_error":
        return "Prior runs hit API errors; keep phase prompts concise and retry only after checking credentials/rate limits."
    if reason == "low_score":
        return "Prior runs completed with low score; tighten acceptance criteria and verify against feedback before another build pass."
    if reason == "agent_stalled":
        return "Prior runs stalled; force smaller steps and use concrete tool actions before extended reasoning."
    if reason == "route_mismatch":
        return f"Prior {task_type} tasks may have used the wrong profile; consider confirming {profile} before execution."
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
