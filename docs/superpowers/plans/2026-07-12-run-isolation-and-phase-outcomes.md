# Run Isolation and Phase Outcomes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make concurrent runs use their own workspace deterministically and let Agent terminal failures drive Scheduler retry/failure handling.

**Architecture:** Keep `RunContext` as the immutable per-run source of workspace paths. Pass it to each phase Agent and install it only for the duration of that agent invocation so the existing tool API remains backward compatible. Replace the untyped `str` phase return with a small `AgentRunResult`; Scheduler converts non-success terminal reasons into exceptions so its existing evidence and recovery path applies.

**Tech Stack:** Python 3.10+, stdlib dataclasses/context managers, `unittest`.

---

## File Structure

- `agents.py`: owns the `AgentRunResult` type and reports why an agent loop stopped.
- `tools.py`: owns a context-local current workspace with a compatibility fallback to `config.WORKSPACE`.
- `orchestrator/run_context.py`: supplies a scoped context manager for one run.
- `orchestrator/scheduler.py`: injects `RunContext` into phase agents and maps unsuccessful results into existing recovery.
- `tests/test_run_isolation.py`: proves two interleaved contexts resolve files and traces independently.
- `tests/test_scheduler.py`: proves an unsuccessful phase result retries/fails rather than completing a phase.
- `tests/test_agents.py`: proves terminal Agent exit reasons are represented without an API call.

### Task 1: Scope workspace access to a run

**Files:**
- Modify: `tools.py:1-35`
- Modify: `orchestrator/run_context.py:1-40`
- Modify: `agents.py:25-46,188-199`
- Modify: `orchestrator/scheduler.py:51-67,69-150`
- Test: `tests/test_run_isolation.py`

- [ ] **Step 1: Write failing isolation tests**

```python
with RunContext(workspace=first, run_id="one").activate():
    tools.write_file("marker.txt", "one")
with RunContext(workspace=second, run_id="two").activate():
    tools.write_file("marker.txt", "two")
self.assertEqual((first / "marker.txt").read_text(), "one")
self.assertEqual((second / "marker.txt").read_text(), "two")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_run_isolation -v`

Expected: FAIL because `RunContext.activate` and context-local tool resolution do not yet exist.

- [ ] **Step 3: Write minimal implementation**

```python
_WORKSPACE: ContextVar[Path | None] = ContextVar("harness_workspace", default=None)

def current_workspace() -> Path:
    return _WORKSPACE.get() or Path(config.WORKSPACE).resolve()
```

Use `current_workspace()` in all workspace-relative tool and trace operations. Add `RunContext.activate()` to set and reset this context variable with `try/finally`; do not mutate `config.WORKSPACE` in `HarnessPhaseRunner._prepare`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_run_isolation tests.test_scheduler -v`

Expected: PASS; existing fake phase runners remain compatible.

### Task 2: Return structured Agent terminal outcomes

**Files:**
- Modify: `agents.py:159-470`
- Modify: `orchestrator/scheduler.py:32-47,69-150,587-599`
- Test: `tests/test_agents.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing outcome tests**

```python
result = AgentRunResult(text="", exit_reason="api_errors", iterations=5)
with self.assertRaises(PhaseExecutionError):
    _require_successful_agent_result(result, "build")
```

Add a Scheduler test whose fake agent returns `AgentRunResult(exit_reason="time_budget")` and assert the state gains `current_failure_evidence` and schedules a retry under the existing recovery policy.

- [ ] **Step 2: Run targeted tests and verify they fail**

Run: `python -m unittest tests.test_agents tests.test_scheduler -v`

Expected: FAIL because Agent returns strings and Scheduler accepts every return as successful.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class AgentRunResult:
    text: str
    exit_reason: str
    iterations: int

    @property
    def succeeded(self) -> bool:
        return self.exit_reason == "no_tool_calls"
```

Set `exit_reason` at every Agent loop break. Preserve compatibility for legacy fake agents that return strings; Scheduler raises `PhaseExecutionError` for an unsuccessful `AgentRunResult`, so `step_once()` uses its existing evidence-guided recovery logic.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_agents tests.test_scheduler tests.test_failure_evidence -v`

Expected: PASS; no-tool-call exits complete normally, API/timeout/iteration exits are recoverable failures.

### Task 3: Full verification and diff review

**Files:**
- Modify only files required by Tasks 1 and 2.

- [ ] **Step 1: Run complete test suite**

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 2: Review scoped diff**

Run: `git diff -- agents.py tools.py orchestrator/run_context.py orchestrator/scheduler.py tests/test_run_isolation.py tests/test_agents.py tests/test_scheduler.py`

Expected: only workspace isolation and phase-outcome changes, plus their tests.

- [ ] **Step 3: Commit**

```bash
git add agents.py tools.py orchestrator/run_context.py orchestrator/scheduler.py tests/test_run_isolation.py tests/test_agents.py tests/test_scheduler.py
git commit -m "fix: isolate runs and recover agent phase failures"
```

Do not commit without explicit user approval.

## Self-Review

- Scope coverage: Task 1 removes shared workspace mutation from scheduler and isolates tools/traces; Task 2 converts Agent terminal failure into Scheduler recovery; Task 3 verifies compatibility.
- Placeholder scan: no unspecified implementation or test steps remain.
- Type consistency: `AgentRunResult`, `PhaseExecutionError`, and `RunContext.activate()` are defined before their downstream use.

