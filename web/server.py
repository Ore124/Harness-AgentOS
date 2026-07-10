"""FastAPI browser console for state-driven Harness runs."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from orchestrator.path_safety import WorkspacePathError, resolve_workspace_path
from orchestrator.scheduler import Scheduler, confirm_profile, set_active
from orchestrator.state import STATE_FILE, create_run_state, load_state, save_state, state_path_for_workspace

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Harness AgentOS Console")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_workers: dict[str, threading.Thread] = {}
_worker_lock = threading.Lock()


class CreateRunRequest(BaseModel):
    prompt: str
    profile: str = "auto"


class ConfirmProfileRequest(BaseModel):
    profile: str


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/runs")
def create_run(request: CreateRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace = Path(config.WORKSPACE).resolve() / run_id
    suffix = 1
    while workspace.exists():
        workspace = Path(config.WORKSPACE).resolve() / f"{run_id}-{suffix}"
        suffix += 1
    run_id = workspace.name
    workspace.mkdir(parents=True, exist_ok=True)

    state = create_run_state(
        prompt=request.prompt.strip(),
        workspace=workspace,
        profile=request.profile,
        run_id=run_id,
    )
    state_path = state_path_for_workspace(workspace)
    save_state(state_path, state)
    background_tasks.add_task(start_worker, run_id)
    return load_state(state_path)


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    root = Path(config.WORKSPACE).resolve()
    runs = []
    if root.exists():
        for state_path in sorted(root.glob(f"*/{STATE_FILE}"), reverse=True):
            try:
                state = load_state(state_path)
                runs.append({
                    "run_id": state["run_id"],
                    "prompt": state["prompt"],
                    "profile": state["profile"],
                    "status": state["status"],
                    "phase": state["phase"],
                    "updated_at": state["updated_at"],
                })
            except Exception:
                continue
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return load_state(_state_path(run_id))


@app.post("/api/runs/{run_id}/confirm-profile")
def confirm_run_profile(run_id: str, request: ConfirmProfileRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    state = confirm_profile(_state_path(run_id), request.profile)
    background_tasks.add_task(start_worker, run_id)
    return state


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    state = set_active(_state_path(run_id), True)
    background_tasks.add_task(start_worker, run_id)
    return state


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str) -> dict[str, Any]:
    return set_active(_state_path(run_id), False)


@app.get("/api/runs/{run_id}/artifact/{name}")
def get_artifact(run_id: str, name: str):
    state = load_state(_state_path(run_id))
    root = Path(state["workspace"]).resolve()
    try:
        target = resolve_workspace_path(root, name)
    except WorkspacePathError:
        raise HTTPException(status_code=404, detail="artifact not found")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(target))


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    async def stream():
        last_payload = ""
        while True:
            try:
                state = load_state(_state_path(run_id))
                payload = {
                    "state": state,
                    "trace": _tail_traces(Path(state["workspace"])),
                }
                encoded = json.dumps(payload, ensure_ascii=False)
                if encoded != last_payload:
                    yield f"event: update\ndata: {encoded}\n\n"
                    last_payload = encoded
                if not state.get("active") and state.get("status") in {"completed", "error"}:
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
                await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")


def start_worker(run_id: str) -> None:
    with _worker_lock:
        existing = _workers.get(run_id)
        if existing and existing.is_alive():
            return
        worker = threading.Thread(target=_run_worker, args=(run_id,), daemon=True)
        _workers[run_id] = worker
        worker.start()


def _run_worker(run_id: str) -> None:
    scheduler = Scheduler(_state_path(run_id))
    scheduler.run_until_idle(poll_interval=0.5)


def _state_path(run_id: str) -> Path:
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail="invalid run id")
    state_path = Path(config.WORKSPACE).resolve() / run_id / STATE_FILE
    if not state_path.exists():
        raise HTTPException(status_code=404, detail="run not found")
    return state_path


def _tail_traces(workspace: Path, max_lines: int = 120) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for trace_file in sorted(workspace.glob("_trace_*.jsonl")):
        try:
            lines = trace_file.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
                event["_file"] = trace_file.name
                events.append(event)
            except json.JSONDecodeError:
                continue
    events.sort(key=lambda e: (e.get("_file", ""), float(e.get("t", 0) or 0)))
    return events[-max_lines:]


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run("web.server:app", host=host, port=port, reload=False)
