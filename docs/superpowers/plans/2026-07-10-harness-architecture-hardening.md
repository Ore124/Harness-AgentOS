# Harness Architecture Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Harness AgentOS safer and more reliable by isolating each run, hardening path and terminal boundaries, making state updates transactional, and reducing duplicated orchestration logic.

**Architecture:** Keep the current Python/FastAPI-style local app shape. Introduce small focused modules for path safety, run context, and persistent orchestration storage; do not introduce microservices, queues, or new external infrastructure. Migrate behavior behind existing public entry points so CLI, web API, tests, and profiles keep their current user-facing shape unless a task explicitly calls out a behavior change.

**Tech Stack:** Python stdlib, current project modules, `unittest`, optional existing web stack, SQLite via `sqlite3`.

---

## Assumptions

- This plan targets the repository at `D:\Codex Projects\Harness AgentOS`.
- The current test runner available in this environment is `python -m unittest discover -s tests -v`; `pytest` is not installed.
- Existing user changes in orchestrator, web, and tests must be preserved and reviewed before implementation starts.
- Security-sensitive changes such as terminal authentication and public API behavior require explicit user confirmation before execution.
- The first implementation pass should avoid broad file moves and keep backward-compatible wrappers around existing APIs.

## Current Problems To Solve

- `config.WORKSPACE` is mutable global process state, while web runs can be started in background threads. Concurrent runs can overwrite each other's workspace.
- Path boundary checks use string prefix checks such as `str(path).startswith(str(workspace))`, which can allow sibling paths with shared prefixes.
- State updates use JSON files plus process-local locks. This does not protect cross-process updates, and load-modify-save paths can overwrite events written by hooks.
- The local web terminal endpoint executes arbitrary shell commands and has no authentication boundary beyond assumed localhost usage.
- `Harness.run()` and the new orchestrator scheduler both own orchestration decisions, which will drift over time.
- Memory currently records potentially sensitive prompt/error data and treats completed runs without scores as success in some paths.
- Server-sent events poll and repeatedly read whole state/trace files, which is acceptable for one local user but becomes expensive and noisy as runs grow.

## Target File Structure

- Create `orchestrator/path_safety.py`: canonical path resolution and workspace containment checks.
- Create `orchestrator/run_context.py`: immutable per-run context carrying workspace, run id, trace root, and execution flags.
- Create `orchestrator/store.py`: SQLite-backed state, event, and lock persistence.
- Modify `tools.py`: replace direct global workspace access with context-aware helpers while keeping compatibility wrappers.
- Modify `agents.py`: pass trace writer and context explicitly where practical.
- Modify `orchestrator/state.py`: keep compatibility API, backed by `OrchestratorStore` after migration.
- Modify `orchestrator/hooks.py`: record events through transactional store APIs.
- Modify `orchestrator/scheduler.py`: own the state machine and phase execution contract.
- Modify `harness.py`: become a compatibility facade over scheduler, or keep only legacy CLI setup after migration.
- Modify `web/server.py`: secure terminal endpoint, worker ownership, artifact path validation, and event streaming.
- Modify `orchestrator/memory.py`: project-scoped retention, redaction, and stricter success semantics.
- Add or modify tests under `tests/` for each changed responsibility.

---

### Task 1: Baseline Safety Tests Before Refactor

**Files:**
- Create: `tests/test_path_safety.py`
- Create: `tests/test_run_isolation.py`
- Modify: none

- [ ] **Step 1: Add path traversal characterization tests**

```python
# tests/test_path_safety.py
import tempfile
import unittest
from pathlib import Path


class PathSafetyCharacterizationTests(unittest.TestCase):
    def test_sibling_prefix_path_must_not_be_allowed(self):
        from tools import _resolve

        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "run"
            sibling = Path(root) / "run-escape"
            workspace.mkdir()
            sibling.mkdir()

            import config
            old_workspace = config.WORKSPACE
            config.WORKSPACE = str(workspace)
            try:
                with self.assertRaises(Exception):
                    _resolve("../run-escape/secret.txt")
            finally:
                config.WORKSPACE = old_workspace

    def test_normal_relative_path_stays_inside_workspace(self):
        from tools import _resolve

        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "run"
            workspace.mkdir()

            import config
            old_workspace = config.WORKSPACE
            config.WORKSPACE = str(workspace)
            try:
                resolved = _resolve("logs/output.txt")
                self.assertTrue(str(resolved).endswith(str(Path("logs") / "output.txt")))
            finally:
                config.WORKSPACE = old_workspace
```

- [ ] **Step 2: Add workspace isolation characterization test**

```python
# tests/test_run_isolation.py
import tempfile
import unittest
from pathlib import Path


class RunIsolationCharacterizationTests(unittest.TestCase):
    def test_workspace_global_can_be_changed_between_runs(self):
        import config

        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "first"
            second = Path(root) / "second"
            first.mkdir()
            second.mkdir()

            old_workspace = config.WORKSPACE
            try:
                config.WORKSPACE = str(first)
                self.assertEqual(Path(config.WORKSPACE), first)
                config.WORKSPACE = str(second)
                self.assertEqual(Path(config.WORKSPACE), second)
            finally:
                config.WORKSPACE = old_workspace
```

- [ ] **Step 3: Run baseline tests**

Run:

```powershell
python -m unittest tests.test_path_safety tests.test_run_isolation -v
```

Expected:

```text
test_sibling_prefix_path_must_not_be_allowed ... FAIL
test_normal_relative_path_stays_inside_workspace ... ok
test_workspace_global_can_be_changed_between_runs ... ok
```

- [ ] **Step 4: Commit baseline tests**

```powershell
git add tests/test_path_safety.py tests/test_run_isolation.py
git commit -m "test: characterize path and workspace safety"
```

---

### Task 2: Add Canonical Path Safety Helper

**Files:**
- Create: `orchestrator/path_safety.py`
- Modify: `tools.py`
- Modify: `web/server.py`
- Test: `tests/test_path_safety.py`

- [ ] **Step 1: Create the shared path helper**

```python
# orchestrator/path_safety.py
from __future__ import annotations

from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a requested path escapes its workspace boundary."""


def resolve_workspace_path(workspace: str | Path, requested: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.expanduser().resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(f"path escapes workspace: {requested}") from exc

    return candidate
```

- [ ] **Step 2: Replace `tools._resolve` implementation**

```python
# tools.py
from orchestrator.path_safety import WorkspacePathError, resolve_workspace_path


def _resolve(path: str) -> Path:
    try:
        return resolve_workspace_path(config.WORKSPACE, path)
    except WorkspacePathError as exc:
        raise ValueError(str(exc)) from exc
```

- [ ] **Step 3: Replace artifact path validation in `web/server.py`**

```python
# web/server.py
from orchestrator.path_safety import WorkspacePathError, resolve_workspace_path


@app.get("/api/artifacts/{run_id}/{path:path}")
def get_artifact(run_id: str, path: str):
    root = _run_root(run_id)
    try:
        target = resolve_workspace_path(root, path)
    except WorkspacePathError:
        raise HTTPException(status_code=404, detail="artifact not found")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(target)
```

- [ ] **Step 4: Run tests**

```powershell
python -m unittest tests.test_path_safety -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```powershell
git add orchestrator/path_safety.py tools.py web/server.py tests/test_path_safety.py
git commit -m "fix: enforce workspace path containment"
```

---

### Task 3: Introduce Explicit Run Context

**Files:**
- Create: `orchestrator/run_context.py`
- Modify: `orchestrator/scheduler.py`
- Modify: `tools.py`
- Test: `tests/test_run_context.py`

- [ ] **Step 1: Add immutable context type**

```python
# orchestrator/run_context.py
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
```

- [ ] **Step 2: Add context-aware tool resolver without removing legacy API**

```python
# tools.py
from orchestrator.run_context import RunContext


def resolve_for_context(ctx: RunContext, path: str) -> Path:
    try:
        return resolve_workspace_path(ctx.workspace, path)
    except WorkspacePathError as exc:
        raise ValueError(str(exc)) from exc
```

- [ ] **Step 3: Use context in scheduler preparation**

```python
# orchestrator/scheduler.py
from orchestrator.run_context import RunContext


class HarnessPhaseRunner:
    def _prepare(self, state: dict) -> RunContext:
        ctx = RunContext.from_state(state)
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        self._ensure_git(ctx.workspace)
        return ctx
```

- [ ] **Step 4: Add tests for context construction**

```python
# tests/test_run_context.py
import tempfile
import unittest
from pathlib import Path


class RunContextTests(unittest.TestCase):
    def test_context_uses_state_workspace_and_run_id(self):
        from orchestrator.run_context import RunContext

        with tempfile.TemporaryDirectory() as root:
            state = {"run_id": "abc123", "workspace": str(Path(root) / "run")}
            ctx = RunContext.from_state(state)

        self.assertEqual(ctx.run_id, "abc123")
        self.assertTrue(str(ctx.workspace).endswith("run"))
        self.assertTrue(str(ctx.trace_dir).endswith(str(Path("run") / ".harness" / "traces")))
        self.assertFalse(ctx.allow_terminal)
```

- [ ] **Step 5: Run tests**

```powershell
python -m unittest tests.test_run_context tests.test_scheduler -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

```powershell
git add orchestrator/run_context.py orchestrator/scheduler.py tools.py tests/test_run_context.py
git commit -m "refactor: introduce explicit run context"
```

---

### Task 4: Move State Persistence To SQLite Transactions

**Files:**
- Create: `orchestrator/store.py`
- Modify: `orchestrator/state.py`
- Modify: `orchestrator/hooks.py`
- Modify: `orchestrator/scheduler.py`
- Test: `tests/test_store.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Add SQLite store**

```python
# orchestrator/store.py
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class OrchestratorStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def save_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (state["run_id"], payload, time.time()),
            )

    def load_state(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT state_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(run_id)
        return json.loads(row[0])

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(run_id, event_json, created_at) VALUES (?, ?, ?)",
                (run_id, payload, time.time()),
            )

    def list_events(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_json
                FROM events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        events = []
        for event_id, payload in rows:
            event = json.loads(payload)
            event["_event_id"] = event_id
            events.append(event)
        return events
```

- [ ] **Step 2: Add store tests**

```python
# tests/test_store.py
import tempfile
import unittest
from pathlib import Path


class OrchestratorStoreTests(unittest.TestCase):
    def test_save_load_and_append_events(self):
        from orchestrator.store import OrchestratorStore

        with tempfile.TemporaryDirectory() as root:
            store = OrchestratorStore(Path(root) / "state.db")
            state = {"run_id": "r1", "workspace": str(Path(root) / "run"), "status": "created"}

            store.save_state(state)
            store.append_event("r1", {"type": "created"})

            self.assertEqual(store.load_state("r1")["status"], "created")
            self.assertEqual(store.list_events("r1")[0]["type"], "created")
```

- [ ] **Step 3: Keep JSON compatibility wrapper in `state.py` during migration**

```python
# orchestrator/state.py
from orchestrator.store import OrchestratorStore


def _store_for_workspace(workspace: str | Path) -> OrchestratorStore:
    return OrchestratorStore(Path(workspace) / ".harness" / "orchestrator.db")
```

- [ ] **Step 4: Convert hooks to append events through store**

```python
# orchestrator/hooks.py
def _record(self, state: dict, event: dict) -> None:
    store = _store_for_workspace(state["workspace"])
    store.append_event(state["run_id"], event)
```

- [ ] **Step 5: Run tests**

```powershell
python -m unittest tests.test_store tests.test_state tests.test_hooks_analytics tests.test_scheduler -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

```powershell
git add orchestrator/store.py orchestrator/state.py orchestrator/hooks.py orchestrator/scheduler.py tests/test_store.py tests/test_state.py
git commit -m "refactor: persist orchestrator state transactionally"
```

---

### Task 5: Secure The Local Terminal Endpoint

**Files:**
- Modify: `web/server.py`
- Modify: `config.py`
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Add explicit terminal settings**

```python
# config.py
WEB_TERMINAL_ENABLED = os.getenv("HARNESS_WEB_TERMINAL_ENABLED", "0") == "1"
WEB_TERMINAL_TOKEN = os.getenv("HARNESS_WEB_TERMINAL_TOKEN", "")
```

- [ ] **Step 2: Add terminal guard in `web/server.py`**

```python
# web/server.py
def _require_terminal_access(request: Request) -> None:
    if not config.WEB_TERMINAL_ENABLED:
        raise HTTPException(status_code=403, detail="terminal disabled")
    if config.WEB_TERMINAL_TOKEN:
        token = request.headers.get("x-harness-token", "")
        if token != config.WEB_TERMINAL_TOKEN:
            raise HTTPException(status_code=401, detail="terminal token required")
```

- [ ] **Step 3: Apply the guard to command execution**

```python
# web/server.py
@app.post("/api/terminal/run")
def run_terminal_command(payload: TerminalCommand, request: Request):
    _require_terminal_access(request)
    return _run_terminal_command(payload)
```

- [ ] **Step 4: Add disabled-by-default test**

```python
# tests/test_web_server.py
def test_terminal_endpoint_disabled_by_default(monkeypatch):
    import config
    from web.server import app

    monkeypatch.setattr(config, "WEB_TERMINAL_ENABLED", False)
    client = TestClient(app)
    response = client.post("/api/terminal/run", json={"command": "echo hi"})
    assert response.status_code == 403
```

If the test suite is pure `unittest` and does not support `monkeypatch`, write it as:

```python
class TerminalSecurityTests(unittest.TestCase):
    def test_terminal_endpoint_disabled_by_default(self):
        import config
        from web.server import app

        old_value = config.WEB_TERMINAL_ENABLED
        config.WEB_TERMINAL_ENABLED = False
        try:
            client = TestClient(app)
            response = client.post("/api/terminal/run", json={"command": "echo hi"})
            self.assertEqual(response.status_code, 403)
        finally:
            config.WEB_TERMINAL_ENABLED = old_value
```

- [ ] **Step 5: Run tests**

```powershell
python -m unittest tests.test_web_server -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

```powershell
git add config.py web/server.py tests/test_web_server.py
git commit -m "fix: gate local terminal command execution"
```

---

### Task 6: Consolidate Orchestration Ownership

**Files:**
- Modify: `harness.py`
- Modify: `orchestrator/scheduler.py`
- Modify: `orchestrator/router.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Decide ownership**

Use this rule:

```text
orchestrator/scheduler.py owns phase transitions, retries, state persistence, and hook dispatch.
harness.py owns CLI/backward-compatible construction and delegates execution.
profiles/*.py own profile-specific prompts, budgets, and tool policy.
```

- [ ] **Step 2: Replace direct phase loop in `Harness.run()` with scheduler delegation**

```python
# harness.py
def run(self) -> dict:
    from orchestrator.scheduler import Scheduler
    from orchestrator.state import create_run_state

    state = create_run_state(
        prompt=self.prompt,
        workspace=config.WORKSPACE,
        profile=self.profile.name,
    )
    scheduler = Scheduler()
    return scheduler.run_until_idle(state)
```

- [ ] **Step 3: Keep legacy phase methods temporarily as private compatibility hooks**

```python
# harness.py
def _legacy_run_phase(self, phase: str) -> None:
    if phase == "plan":
        self.run_planner()
        return
    if phase == "build":
        self.run_builder()
        return
    if phase == "evaluate":
        self.run_evaluator()
        return
    raise ValueError(f"unknown phase: {phase}")
```

- [ ] **Step 4: Assert scheduler is the owner in tests**

```python
# tests/test_scheduler.py
def test_harness_delegates_to_scheduler(monkeypatch):
    import harness

    calls = []

    class FakeScheduler:
        def run_until_idle(self, state):
            calls.append(state)
            return {"status": "completed"}

    monkeypatch.setattr("orchestrator.scheduler.Scheduler", lambda: FakeScheduler())
    result = harness.Harness("build x").run()
    assert result["status"] == "completed"
    assert calls
```

If the suite stays on `unittest`, convert the patch to `unittest.mock.patch`.

- [ ] **Step 5: Run tests**

```powershell
python -m unittest tests.test_scheduler -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

```powershell
git add harness.py orchestrator/scheduler.py orchestrator/router.py tests/test_scheduler.py
git commit -m "refactor: make scheduler own orchestration"
```

---

### Task 7: Fix Memory Success Semantics And Data Retention

**Files:**
- Modify: `orchestrator/memory.py`
- Test: `tests/test_memory.py`

- [ ] **Step 1: Tighten run success logic**

```python
# orchestrator/memory.py
def _run_passed(state: dict) -> bool:
    evaluation = state.get("evaluation") or {}
    if "passed" in evaluation:
        return bool(evaluation["passed"])
    if "score" in evaluation:
        return float(evaluation["score"]) >= 0.8
    return state.get("status") == "completed" and state.get("validated") is True
```

- [ ] **Step 2: Redact stored prompt and error previews**

```python
# orchestrator/memory.py
SENSITIVE_MARKERS = ("api_key", "apikey", "authorization", "bearer ", "password", "secret", "token")


def _safe_preview(value: str, limit: int = 300) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in SENSITIVE_MARKERS):
        return "[redacted]"
    return value[:limit]
```

- [ ] **Step 3: Use `_safe_preview` when recording memory**

```python
# orchestrator/memory.py
record = {
    "run_id": state.get("run_id"),
    "profile": state.get("profile"),
    "prompt_preview": _safe_preview(state.get("prompt", "")),
    "last_error": _safe_preview(state.get("last_error", "")),
    "passed": _run_passed(state),
}
```

- [ ] **Step 4: Add tests**

```python
# tests/test_memory.py
import unittest


class MemorySafetyTests(unittest.TestCase):
    def test_completed_without_validation_is_not_success(self):
        from orchestrator.memory import _run_passed

        self.assertFalse(_run_passed({"status": "completed"}))

    def test_completed_with_validation_is_success(self):
        from orchestrator.memory import _run_passed

        self.assertTrue(_run_passed({"status": "completed", "validated": True}))

    def test_sensitive_preview_is_redacted(self):
        from orchestrator.memory import _safe_preview

        self.assertEqual(_safe_preview("Authorization: Bearer abc"), "[redacted]")
```

- [ ] **Step 5: Run tests**

```powershell
python -m unittest tests.test_memory -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

```powershell
git add orchestrator/memory.py tests/test_memory.py
git commit -m "fix: tighten memory success and redaction rules"
```

---

### Task 8: Make Event Streaming Incremental

**Files:**
- Modify: `web/server.py`
- Modify: `orchestrator/store.py`
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Read events by cursor instead of whole state every second**

```python
# web/server.py
async def run_events(run_id: str):
    last_event_id = 0
    store = _store_for_run(run_id)
    while True:
        events = store.list_events(run_id, after_id=last_event_id, limit=100)
        for event in events:
            last_event_id = max(last_event_id, int(event["_event_id"]))
            yield {"event": "message", "data": json.dumps(event)}
        await asyncio.sleep(1)
```

- [ ] **Step 2: Keep compatibility snapshot endpoint separate**

```python
# web/server.py
@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    store = _store_for_run(run_id)
    return store.load_state(run_id)
```

- [ ] **Step 3: Add cursor test at store level**

```python
# tests/test_web_server.py
def test_event_cursor_returns_only_new_events():
    with tempfile.TemporaryDirectory() as root:
        store = OrchestratorStore(Path(root) / "state.db")
        store.save_state({"run_id": "r1", "workspace": root, "status": "running"})
        store.append_event("r1", {"type": "one"})
        first = store.list_events("r1")
        store.append_event("r1", {"type": "two"})
        second = store.list_events("r1", after_id=first[-1]["_event_id"])
        assert [event["type"] for event in second] == ["two"]
```

If the suite stays on `unittest`, convert `assert` to `self.assertEqual`.

- [ ] **Step 4: Run tests**

```powershell
python -m unittest tests.test_web_server tests.test_store -v
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```powershell
git add web/server.py orchestrator/store.py tests/test_web_server.py
git commit -m "perf: stream run events incrementally"
```

---

### Task 9: Final Integration Pass

**Files:**
- Modify only files already changed in Tasks 1-8 if integration failures require it.

- [ ] **Step 1: Run compile check**

```powershell
python -m compileall -q .
```

Expected:

```text
No output and exit code 0.
```

- [ ] **Step 2: Run full unittest suite**

```powershell
python -m unittest discover -s tests -v
```

Expected:

```text
All tests pass.
```

- [ ] **Step 3: Run whitespace check**

```powershell
git diff --check
```

Expected:

```text
No whitespace errors.
```

- [ ] **Step 4: Review changed public behavior**

Confirm these are the only intended behavior changes:

```text
1. Workspace path traversal into sibling directories is rejected.
2. Web terminal execution is disabled unless HARNESS_WEB_TERMINAL_ENABLED=1.
3. Memory no longer treats unvalidated completed runs as successful training examples.
4. Event streaming sends incremental events instead of repeatedly sending full snapshots.
```

- [ ] **Step 5: Commit final integration fixes**

```powershell
git add .
git commit -m "test: validate architecture hardening integration"
```

---

## Execution Order

1. Task 1: Add failing/safety characterization tests.
2. Task 2: Fix path boundary checks first because it is a direct security issue.
3. Task 3: Add `RunContext` before removing global workspace usage.
4. Task 4: Move state persistence to SQLite transactions.
5. Task 5: Gate terminal execution after the basic safety layer is in place.
6. Task 6: Consolidate orchestration ownership.
7. Task 7: Correct memory training signal and redaction.
8. Task 8: Make event streaming incremental.
9. Task 9: Run final integration validation.

## Risk Notes

- Task 4 is the highest implementation risk because it changes persistence semantics. Keep compatibility wrappers in `orchestrator/state.py` until all call sites are migrated.
- Task 5 intentionally changes default terminal behavior. Confirm this with the project owner before executing.
- Task 6 can expose hidden coupling between `Harness` and agents. Do not delete legacy methods in the same pass.
- SQLite is the recommended persistence upgrade here because it solves local transactional state without introducing a server, broker, or deployment dependency.

## Self-Review

- Spec coverage: The plan covers run isolation, path safety, transactional state, terminal hardening, orchestration ownership, memory governance, event streaming, and validation.
- Placeholder scan: The plan contains concrete files, commands, expected outcomes, and code snippets for each task.
- Type consistency: `RunContext`, `OrchestratorStore`, `resolve_workspace_path`, `_safe_preview`, and `_run_passed` names are used consistently across tasks.
