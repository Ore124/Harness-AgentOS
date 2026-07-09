"""Small JSON-backed memory store for routing feedback."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

VALID_PROFILES = {"app-builder", "terminal", "swe-bench", "reasoning"}


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
            return {"version": 1, "profiles": {}, "runs": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "profiles": {}, "runs": []}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def adjust_candidates(self, candidates: list[dict[str, Any]], prompt: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply small confidence adjustments from profile history."""
        data = self.load()
        profiles = data.get("profiles", {})
        refs: list[str] = []
        adjusted: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            profile = item.get("profile")
            stats = profiles.get(profile, {})
            attempts = int(stats.get("attempts", 0) or 0)
            if attempts >= 3:
                success_rate = float(stats.get("successes", 0) or 0) / attempts
                average_score = float(stats.get("average_score", 0.0) or 0.0)
                delta = (success_rate - 0.5) * 0.08 + ((average_score - 7.0) / 10.0) * 0.04
                item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.0)) + delta))
                item["reason"] = f"{item.get('reason', '')} Memory adjusted by {delta:+.2f}."
                refs.append(f"{profile}: attempts={attempts}, success_rate={success_rate:.2f}, avg={average_score:.1f}")
            adjusted.append(item)
        adjusted.sort(key=lambda c: c.get("confidence", 0.0), reverse=True)
        return adjusted, refs

    def record_run(self, state: dict[str, Any], analysis: dict[str, Any]) -> None:
        profile = state.get("profile")
        if profile not in VALID_PROFILES:
            return

        data = self.load()
        profiles = data.setdefault("profiles", {})
        stats = profiles.setdefault(profile, {
            "attempts": 0,
            "successes": 0,
            "average_score": 0.0,
            "last_error": None,
        })

        attempts = int(stats.get("attempts", 0) or 0)
        old_avg = float(stats.get("average_score", 0.0) or 0.0)
        score_history = state.get("score_history") or []
        final_score = float(score_history[-1]) if score_history else 0.0
        passed = state.get("status") == "completed" and (final_score >= 0.01 or not score_history)

        stats["attempts"] = attempts + 1
        stats["successes"] = int(stats.get("successes", 0) or 0) + (1 if passed else 0)
        stats["average_score"] = round(((old_avg * attempts) + final_score) / (attempts + 1), 2)
        stats["last_error"] = state.get("last_error")

        runs = data.setdefault("runs", [])
        runs.append({
            "run_id": state.get("run_id"),
            "profile": profile,
            "prompt": state.get("prompt", "")[:300],
            "status": state.get("status"),
            "score": final_score,
            "tool_calls": analysis.get("tool_calls", {}).get("total", 0),
        })
        if len(runs) > 200:
            del runs[:-200]
        self.save(data)
