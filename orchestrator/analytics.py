"""Run analysis helpers for state-driven orchestration."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import config

ANALYSIS_FILE = "analysis.json"


def analyze_workspace(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace)
    analysis = {
        "tool_calls": {"total": 0, "by_tool": {}},
        "agents": {},
        "errors": [],
        "finish_reasons": {},
        "scores": [],
        "artifacts": list_artifacts(root),
    }

    for trace_file in sorted(root.glob("_trace_*.jsonl")):
        agent = trace_file.stem.replace("_trace_", "", 1)
        agent_stats = {"events": 0, "tool_calls": 0, "errors": 0, "last_event": None}
        for event in _read_jsonl(trace_file):
            agent_stats["events"] += 1
            agent_stats["last_event"] = event
            if event.get("event") == "tool_call":
                tool = event.get("tool", "unknown")
                agent_stats["tool_calls"] += 1
                analysis["tool_calls"]["total"] += 1
                by_tool = analysis["tool_calls"]["by_tool"]
                by_tool[tool] = by_tool.get(tool, 0) + 1
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

    out_path = root / ANALYSIS_FILE
    out_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return analysis


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
