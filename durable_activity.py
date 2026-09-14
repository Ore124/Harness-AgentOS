"""Container entry point for one durable Agent activity."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from orchestrator.run_context import RunContext
from orchestrator.runtime import ProfileAgentAdapter


def main() -> int:
    work_item = json.loads(os.environ["HARNESS_WORK_ITEM_JSON"])
    checkpoint = json.loads(os.environ.get("HARNESS_CHECKPOINT_JSON", "{}"))
    run_id = os.environ["HARNESS_RUN_ID"]
    workspace = Path("/workspace").resolve()
    checkpoint_journal = Path(os.environ["HARNESS_CHECKPOINT_JOURNAL"])

    def checkpoint(payload) -> None:
        checkpoint_journal.parent.mkdir(parents=True, exist_ok=True)
        with checkpoint_journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")

    context = RunContext(
        run_id=run_id,
        workspace=workspace,
        trace_dir=workspace / ".harness" / "traces",
        allow_terminal=True,
        task_id=str(work_item["id"]),
        checkpoint_callback=checkpoint,
    )
    outcome = ProfileAgentAdapter().run(work_item, checkpoint, context)
    destination = Path(os.environ["HARNESS_ACTIVITY_OUTCOME"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(asdict(outcome), ensure_ascii=False),
        encoding="utf-8",
    )
    return 0 if outcome.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
