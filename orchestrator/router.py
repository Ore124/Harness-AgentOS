"""Four-layer profile routing for Harness tasks."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.memory import MemoryStore, VALID_PROFILES

CONFIRMATION_THRESHOLD = 0.55
RULE_HIGH_CONFIDENCE = 0.72


@dataclass
class RouteDecision:
    profile: str | None
    confidence: float
    source: str
    reasoning: str
    alternatives: list[dict[str, Any]]
    task_type: str
    task_type_confidence: float
    task_type_source: str
    requires_confirmation: bool = False
    memory_refs: list[str] | None = None
    short_term_refs: list[str] | None = None
    long_term_refs: list[str] | None = None
    memory_adjustments: list[dict[str, Any]] | None = None
    strategy_hints: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "reasoning": self.reasoning,
            "alternatives": self.alternatives,
            "task_type": self.task_type,
            "task_type_confidence": round(self.task_type_confidence, 3),
            "task_type_source": self.task_type_source,
            "requires_confirmation": self.requires_confirmation,
            "memory_refs": self.memory_refs or [],
            "short_term_refs": self.short_term_refs or [],
            "long_term_refs": self.long_term_refs or [],
            "memory_adjustments": self.memory_adjustments or [],
            "strategy_hints": self.strategy_hints or [],
        }


class Router:
    """Routes a task to one of the existing profiles."""

    def __init__(
        self,
        memory: MemoryStore | None = None,
        llm_router: Callable[[str, list[dict[str, Any]]], dict[str, Any]] | None = None,
        confirmation_threshold: float = CONFIRMATION_THRESHOLD,
    ):
        self.memory = memory or MemoryStore()
        self.llm_router = llm_router
        self.confirmation_threshold = confirmation_threshold

    def route(self, prompt: str, override_profile: str | None = None) -> RouteDecision:
        task_type, task_type_confidence, task_type_source = classify_task_type(prompt)

        if override_profile and override_profile != "auto":
            if override_profile not in VALID_PROFILES:
                raise ValueError(f"Unknown profile override: {override_profile}")
            return RouteDecision(
                profile=override_profile,
                confidence=1.0,
                source="manual",
                reasoning="User explicitly selected a profile.",
                alternatives=[],
                task_type=task_type,
                task_type_confidence=task_type_confidence,
                task_type_source=task_type_source,
                requires_confirmation=False,
                memory_refs=[],
                short_term_refs=[],
                long_term_refs=[],
                memory_adjustments=[],
                strategy_hints=[],
            )

        candidates = self._rule_candidates(prompt)
        top = candidates[0]
        close_second = len(candidates) > 1 and top["confidence"] - candidates[1]["confidence"] < 0.12

        source = "rule"
        if top["confidence"] < RULE_HIGH_CONFIDENCE or close_second:
            llm_decision = self._llm_route(prompt, candidates)
            if llm_decision:
                candidates = self._merge_llm_decision(candidates, llm_decision)
                source = "llm"

        candidates, memory_info = self.memory.adjust_candidates(candidates, prompt, task_type)
        top = candidates[0]
        confidence = float(top.get("confidence", 0.0))
        requires_confirmation = confidence < self.confirmation_threshold

        return RouteDecision(
            profile=top["profile"] if not requires_confirmation else None,
            confidence=confidence,
            source=source,
            reasoning=top.get("reason", ""),
            alternatives=candidates[:4],
            task_type=task_type,
            task_type_confidence=task_type_confidence,
            task_type_source=task_type_source,
            requires_confirmation=requires_confirmation,
            memory_refs=memory_info.get("memory_refs", []),
            short_term_refs=memory_info.get("short_term_refs", []),
            long_term_refs=memory_info.get("long_term_refs", []),
            memory_adjustments=memory_info.get("memory_adjustments", []),
            strategy_hints=memory_info.get("strategy_hints", []),
        )

    def _rule_candidates(self, prompt: str) -> list[dict[str, Any]]:
        text = prompt.lower()
        profiles = {
            "app-builder": self._score(text, [
                "build", "app", "web", "website", "browser", "ui", "page",
                "component", "react", "frontend", "dashboard", "html", "css",
                "javascript", "button", "form",
            ]),
            "terminal": self._score(text, [
                "terminal", "shell", "cli", "command", "linux", "file", "directory",
                "symlink", "permission", "install", "server", "bash", "script",
                "docker", "process",
            ]),
            "swe-bench": self._score(text, [
                "bug", "fix", "issue", "failing test", "regression", "traceback",
                "patch", "repository", "function", "class", "pytest", "unit test",
                "typeerror", "exception",
            ]),
            "reasoning": self._score(text, [
                "explain", "why", "calculate", "prove", "derive", "answer",
                "math", "reason", "logic", "compare", "analyze", "question",
            ]),
        }

        # Product creation prompts often say "build"; do not let that single word
        # dominate unless UI/web terms are also present.
        if re.search(r"\b(build|create|make)\b", text) and re.search(r"\b(app|web|ui|page|site|browser)\b", text):
            profiles["app-builder"] += 0.25

        total = sum(profiles.values()) or 1.0
        candidates = []
        for profile, score in profiles.items():
            confidence = 0.25 + min(score / total, 1.0) * 0.65
            candidates.append({
                "profile": profile,
                "confidence": round(confidence, 3),
                "reason": f"Rule keywords matched for {profile}.",
            })
        candidates.sort(key=lambda c: c["confidence"], reverse=True)
        return candidates

    @staticmethod
    def _score(text: str, terms: list[str]) -> float:
        score = 0.0
        for term in terms:
            if term in text:
                score += 1.0 + min(len(term), 12) / 20.0
        return score

    def _llm_route(self, prompt: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if self.llm_router:
            return self.llm_router(prompt, candidates)

        try:
            from agents import get_client
            import config
            import metrics

            started = time.perf_counter()
            response = get_client().chat.completions.create(
                model=config.MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Choose the best Harness profile for the task. "
                            "Return only JSON with profile, confidence, reasoning. "
                            "Valid profiles: app-builder, terminal, swe-bench, reasoning."
                        ),
                    },
                    {"role": "user", "content": f"Task:\n{prompt}\n\nRule candidates:\n{json.dumps(candidates)}"},
                ],
                max_tokens=300,
            )
            call_index = metrics.RECORDER.record_llm_call(
                role="router",
                phase="route",
                model=config.MODEL,
                latency_ms=int((time.perf_counter() - started) * 1000),
                usage=getattr(response, "usage", None),
            )
            content = response.choices[0].message.content or ""
            metrics.RECORDER.record_llm_result(
                call_index=call_index,
                tool_names=[],
                finish_reason=response.choices[0].finish_reason,
                content=content,
            )
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group(0))
            if data.get("profile") not in VALID_PROFILES:
                return None
            return data
        except Exception:
            return None

    @staticmethod
    def _merge_llm_decision(candidates: list[dict[str, Any]], llm_decision: dict[str, Any]) -> list[dict[str, Any]]:
        profile = llm_decision["profile"]
        llm_conf = max(0.0, min(1.0, float(llm_decision.get("confidence", 0.6))))
        reasoning = str(llm_decision.get("reasoning", "LLM router selected this profile."))
        merged = []
        for candidate in candidates:
            item = dict(candidate)
            if item["profile"] == profile:
                item["confidence"] = round(max(float(item["confidence"]), llm_conf), 3)
                item["reason"] = reasoning
            else:
                item["confidence"] = round(float(item["confidence"]) * 0.92, 3)
            merged.append(item)
        merged.sort(key=lambda c: c["confidence"], reverse=True)
        return merged


def classify_task_type(prompt: str) -> tuple[str, float, str]:
    """Classify the task into a stable memory bucket."""
    text = prompt.lower()
    buckets = {
        "web_app": _score_terms(text, [
            "web", "website", "browser", "ui", "page", "component", "react",
            "frontend", "dashboard", "html", "css", "javascript", "button",
            "form", "app",
        ]),
        "terminal_ops": _score_terms(text, [
            "terminal", "shell", "cli", "command", "linux", "file", "directory",
            "symlink", "permission", "install", "server", "bash", "script",
            "docker", "process",
        ]),
        "code_repair": _score_terms(text, [
            "bug", "fix", "issue", "failing test", "regression", "traceback",
            "patch", "repository", "function", "class", "pytest", "unit test",
            "typeerror", "exception",
        ]),
        "reasoning": _score_terms(text, [
            "explain", "why", "calculate", "prove", "derive", "answer",
            "math", "reason", "logic", "compare", "analyze", "question",
            "equation",
        ]),
    }
    if re.search(r"\b(build|create|make)\b", text) and re.search(r"\b(app|web|ui|page|site|browser)\b", text):
        buckets["web_app"] += 0.5

    best_type, best_score = max(buckets.items(), key=lambda item: item[1])
    total = sum(buckets.values())
    if best_score <= 0 or total <= 0:
        return "unclear", 0.25, "rule"
    confidence = 0.35 + min(best_score / total, 1.0) * 0.6
    return best_type, round(confidence, 3), "rule"


def _score_terms(text: str, terms: list[str]) -> float:
    score = 0.0
    for term in terms:
        if term in text:
            score += 1.0 + min(len(term), 12) / 20.0
    return score
