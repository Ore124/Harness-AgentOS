"""FastAPI browser console for state-driven Harness runs."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from orchestrator.path_safety import WorkspacePathError, resolve_workspace_path
from orchestrator.scheduler import Scheduler, approve_human_action, confirm_profile, set_active
from orchestrator.state import _store_for_workspace, STATE_FILE, create_run_state, load_state, save_state, state_path_for_workspace

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Harness AgentOS Console")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_workers: dict[str, threading.Thread] = {}
_worker_lock = threading.Lock()
_terminal_lock = threading.Lock()
_terminal_cwds: dict[str, Path] = {}


class CreateRunRequest(BaseModel):
    prompt: str
    profile: str = "auto"


class ConfirmProfileRequest(BaseModel):
    profile: str


class TerminalRunRequest(BaseModel):
    command: str
    session_id: str = "default"
    run_id: str | None = None
    timeout: int = 120


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        content=(STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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
                    "run_id": state_path.parent.name,
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
    return _load_api_state(run_id)


@app.post("/api/runs/{run_id}/confirm-profile")
def confirm_run_profile(run_id: str, request: ConfirmProfileRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    state = confirm_profile(_state_path(run_id), request.profile)
    state["run_id"] = run_id
    background_tasks.add_task(start_worker, run_id)
    return state


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    state = set_active(_state_path(run_id), True)
    state["run_id"] = run_id
    background_tasks.add_task(start_worker, run_id)
    return state


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    state = approve_human_action(_state_path(run_id))
    state["run_id"] = run_id
    background_tasks.add_task(start_worker, run_id)
    return state


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str) -> dict[str, Any]:
    state = set_active(_state_path(run_id), False)
    state["run_id"] = run_id
    return state


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


def _require_terminal_access(request: Request) -> None:
    if not config.WEB_TERMINAL_ENABLED:
        raise HTTPException(status_code=403, detail="terminal disabled")
    if config.WEB_TERMINAL_TOKEN:
        token = request.headers.get("x-harness-token", "")
        if token != config.WEB_TERMINAL_TOKEN:
            raise HTTPException(status_code=401, detail="terminal token required")


@app.post("/api/terminal/run")
def run_terminal_command(payload: TerminalRunRequest, request: Request) -> dict[str, Any]:
    _require_terminal_access(request)
    command = payload.command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required")

    session_id = payload.session_id.strip() or "default"
    timeout = max(1, min(int(payload.timeout or 120), 600))

    with _terminal_lock:
        cwd = _terminal_cwds.get(session_id)
        if cwd is None:
            cwd = _initial_terminal_cwd(payload.run_id)
            _terminal_cwds[session_id] = cwd

        cd_result = _handle_cd(command, cwd)
        if cd_result is not None:
            _terminal_cwds[session_id] = cd_result
            return {
                "command": command,
                "cwd": str(cd_result),
                "exit_code": 0,
                "stdout": str(cd_result),
                "stderr": "",
                "duration": 0.0,
            }

    started = datetime.now()
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        duration = (datetime.now() - started).total_seconds()
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": result.returncode,
            "stdout": result.stdout[-20000:],
            "stderr": result.stderr[-20000:],
            "duration": round(duration, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": 124,
            "stdout": (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"Command timed out after {timeout}s",
            "duration": timeout,
        }
    except Exception as exc:
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration": 0.0,
        }


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    async def stream():
        last_event_id = 0
        last_snapshot_key = None
        while True:
            try:
                store = _store_for_run(run_id)
                events = store.list_events(run_id, after_id=last_event_id, limit=100)
                for event in events:
                    last_event_id = max(last_event_id, int(event["_event_id"]))
                    yield f"event: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

                state = _load_api_state(run_id)
                trace = _tail_traces(Path(state["workspace"]).resolve())
                trace_key = trace[-1].get("t") if trace else None
                snapshot_key = (state.get("updated_at"), state.get("last_event_at"), len(trace), trace_key)
                if snapshot_key != last_snapshot_key:
                    last_snapshot_key = snapshot_key
                    update_payload = json.dumps({"state": state, "trace": trace}, ensure_ascii=False)
                    yield f"event: update\ndata: {update_payload}\n\n"
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


def _load_api_state(run_id: str) -> dict[str, Any]:
    state = load_state(_state_path(run_id))
    state["run_id"] = run_id
    return state


def _store_for_run(run_id: str):
    state = load_state(_state_path(run_id))
    return _store_for_workspace(state["workspace"])


def _initial_terminal_cwd(run_id: str | None) -> Path:
    if run_id:
        try:
            state = load_state(_state_path(run_id))
            workspace = Path(state["workspace"]).resolve()
            if workspace.exists():
                return workspace
        except Exception:
            pass
    return BASE_DIR.parent.resolve()


def _handle_cd(command: str, cwd: Path) -> Path | None:
    stripped = command.strip()
    lower = stripped.lower()
    if lower in {"cd", "pwd"}:
        return cwd
    if not (lower.startswith("cd ") or lower.startswith("chdir ")):
        return None

    _, _, target_text = stripped.partition(" ")
    target_text = target_text.strip().strip('"').strip("'")
    if not target_text:
        return cwd
    target = Path(target_text)
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve()
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=400, detail=f"directory not found: {target_text}")
    return target


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
