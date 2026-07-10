"""Strategy hint selection and prompt formatting.

Strategy hints come from historical memory. They are useful, but risky if
treated as hard requirements, so this module keeps all injection policy in one
place: strict filtering, low volume, and advisory wording.
"""
from __future__ import annotations

from typing import Any

MIN_STRATEGY_HINT_CONFIDENCE = 0.75
MAX_STRATEGY_HINTS_PER_PHASE = 2

PHASE_REASON_ALLOWLIST = {
    "builder": {
        "tool_missing",
        "timeout",
        "browser_unavailable",
        "agent_stalled",
        "tests_failed",
        "low_score",
    },
    "evaluator": {
        "browser_unavailable",
        "tests_failed",
        "low_score",
    },
}


def select_strategy_hints_for_phase(
    state: dict[str, Any],
    phase: str,
    min_confidence: float = MIN_STRATEGY_HINT_CONFIDENCE,
    max_hints: int = MAX_STRATEGY_HINTS_PER_PHASE,
) -> list[dict[str, Any]]:
    """Return relevant hints for a phase, filtered to reduce negative transfer."""
    allowed_reasons = PHASE_REASON_ALLOWLIST.get(phase, set())
    if not allowed_reasons:
        return []

    route_decision = state.get("route_decision") or {}
    task_type = state.get("task_type") or route_decision.get("task_type")
    profile = state.get("profile") or route_decision.get("profile")
    raw_hints = state.get("strategy_hints") or route_decision.get("strategy_hints") or []

    selected: list[dict[str, Any]] = []
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict):
            continue
        hint_profile = raw_hint.get("profile")
        hint_task_type = raw_hint.get("task_type")
        reason = raw_hint.get("failure_reason")
        confidence = float(raw_hint.get("confidence", 0.0) or 0.0)

        if hint_profile and profile and hint_profile != profile:
            continue
        if hint_task_type and task_type and hint_task_type != task_type:
            continue
        if reason not in allowed_reasons:
            continue
        if confidence < min_confidence:
            continue
        if not str(raw_hint.get("hint", "")).strip():
            continue

        selected.append(dict(raw_hint))

    selected.sort(key=lambda hint: float(hint.get("confidence", 0.0) or 0.0), reverse=True)
    return selected[:max_hints]


def format_strategy_hints_for_prompt(hints: list[dict[str, Any]]) -> str:
    """Format strategy hints as a short, explicitly low-priority prompt block."""
    if not hints:
        return ""

    lines = [
        "Historical strategy hints (advisory only):",
        "- Current task requirements, actual tool results, and existing project files take priority.",
        "- Use these hints to avoid repeated historical failure patterns; do not treat them as hard constraints.",
    ]
    for hint in hints:
        reason = hint.get("failure_reason", "pattern")
        confidence = float(hint.get("confidence", 0.0) or 0.0)
        text = str(hint.get("hint", "")).strip()
        lines.append(f"- [{reason}, confidence {confidence:.2f}] {text}")
    return "\n".join(lines)


def append_strategy_hints_to_prompt(prompt: str, state: dict[str, Any], phase: str) -> str:
    """Append selected hints to a phase prompt, or return the original prompt."""
    hints = select_strategy_hints_for_phase(state, phase)
    hint_block = format_strategy_hints_for_prompt(hints)
    if not hint_block:
        return prompt
    return f"{prompt.rstrip()}\n\n{hint_block}"
