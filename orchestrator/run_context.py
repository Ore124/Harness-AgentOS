from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    run_id: str
    workspace: Path
    trace_dir: Path
    allow_terminal: bool = False

    @classmethod
    def from_state(cls, state: dict, *, allow_terminal: bool = False) -> "RunContext":
        workspace = Path(state["workspace"]).expanduser().resolve()
        run_id = str(state["run_id"])
        return cls(
            run_id=run_id,
            workspace=workspace,
            trace_dir=workspace / ".harness" / "traces",
            allow_terminal=allow_terminal,
        )
