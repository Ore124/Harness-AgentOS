# Run-Owned Process Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register every long-lived process created by an Agent under the active run and clean only that run’s registered process trees when it completes or fails.

**Architecture:** Extend the existing context-local workspace with a context-local run id. Replace module-global background/dev-server ownership with a registry keyed by run id; each registered process is launched in its own process group/session and cleanup addresses only registered roots. Scheduler invokes cleanup on terminal success and exception/timeout paths, never by image name, command pattern, or workspace scan.

**Tech Stack:** Python 3.10+, stdlib `contextvars`, `subprocess`, `unittest`.

---

## File Structure

- `tools.py`: owns active run identity, run-keyed process registry, launch registration, and process-tree cleanup.
- `orchestrator/run_context.py`: activates and restores workspace plus run id together.
- `orchestrator/scheduler.py`: calls run cleanup on completed and failed/timeout runs.
- `tests/test_tools.py`: deterministic tests for run ownership and no cross-run/external termination.
- `tests/test_scheduler.py`: deterministic tests for cleanup after an exception and after normal completion.

### Task 1: Register processes by active run

**Files:**
- Modify: `tools.py:26-190,452-483`
- Modify: `orchestrator/run_context.py:26-38`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing ownership tests**

```python
with first_context.activate():
    tools._start_background_command("python -c \"...\"")
with second_context.activate():
    tools._start_background_command("python -c \"...\"")
tools.cleanup_run_processes("one")
self.assertTrue(first_process.terminated)
self.assertFalse(second_process.terminated)
```

Add a test with an unregistered mock process and assert cleanup never calls its termination method.

- [ ] **Step 2: Run targeted tests to verify failure**

Run: `python -m unittest tests.test_tools -v`

Expected: FAIL because process ownership is global and `cleanup_run_processes` does not exist.

- [ ] **Step 3: Implement run-keyed ownership**

```python
_ACTIVE_RUN_ID: ContextVar[str | None] = ContextVar("harness_run_id", default=None)
_PROCESSES_BY_RUN: dict[str, list[subprocess.Popen]] = {}

def cleanup_run_processes(run_id: str) -> str:
    for process in _PROCESSES_BY_RUN.pop(run_id, []):
        if process.poll() is None:
            _terminate_process_tree(process)
```

Have `RunContext.activate()` install both workspace and run id. Register only processes created by `_start_background_command` and `_ensure_dev_server`; launch them in a new session/process group. Keep the legacy no-active-run fallback isolated under a dedicated legacy registry rather than killing arbitrary system processes.

- [ ] **Step 4: Run targeted tests**

Run: `python -m unittest tests.test_tools tests.test_run_context -v`

Expected: PASS; a run cleans only its own registered live process roots and their children.

### Task 2: Clean registered processes at Scheduler terminal boundaries

**Files:**
- Modify: `orchestrator/scheduler.py:240-270,410-445`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
with patch("orchestrator.scheduler.tools.cleanup_run_processes") as cleanup:
    Scheduler(path, phase_runner=FailingRunner()).step_once()
cleanup.assert_called_once_with("run-id")
```

Add a completed-run test that advances to `analyze` and asserts cleanup receives that run id. Assert retrying failures also clean registered children before the retry so a timed-out phase leaves no owned process behind.

- [ ] **Step 2: Run targeted tests to verify failure**

Run: `python -m unittest tests.test_scheduler -v`

Expected: FAIL because Scheduler never owns cleanup.

- [ ] **Step 3: Implement terminal-boundary cleanup**

```python
def _cleanup_run_processes(self, state: dict[str, Any]) -> None:
    run_id = state.get("run_id")
    if run_id:
        tools.cleanup_run_processes(str(run_id))
```

Call it in the Scheduler exception path before recovery handling and in `_analyze` before marking the run complete. Do not call it for pause/confirmation states.

- [ ] **Step 4: Run focused regression tests**

Run: `python -m unittest tests.test_tools tests.test_run_context tests.test_scheduler -v`

Expected: PASS; cleanup is run-scoped and triggered for normal completion, failures, and timeout-derived exceptions.

### Task 3: Full verification

**Files:**
- Modify only files listed in Tasks 1 and 2.

- [ ] **Step 1: Run complete suite**

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 2: Review change scope**

Run: `git diff --check; git diff -- tools.py orchestrator/run_context.py orchestrator/scheduler.py tests/test_tools.py tests/test_scheduler.py`

Expected: only process ownership, lifecycle cleanup, and deterministic tests; no Prompt, Profile strategy, or performance changes.

- [ ] **Step 3: Commit**

```bash
git add tools.py orchestrator/run_context.py orchestrator/scheduler.py tests/test_tools.py tests/test_scheduler.py
git commit -m "fix: isolate child processes by harness run"
```

Do not commit without explicit user approval.

## Self-Review

- Scope coverage: Task 1 prevents global/cross-run cleanup; Task 2 cleans only the current run on complete, failure, or timeout; Task 3 checks all regressions.
- Placeholder scan: every test, implementation boundary, and command is specified.
- Type consistency: `RunContext.activate()` installs `run_id`; `tools.cleanup_run_processes(run_id)` is the only Scheduler cleanup API.

