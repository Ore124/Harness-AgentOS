# Canonical Trace and Offline Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit a versioned run-local canonical event stream and replay/compare it without calling models or tools.

**Architecture:** Add a stdlib-only trace module that appends JSONL envelope events under `.harness/canonical_trace.jsonl`. Instrument existing lifecycle boundaries as passive side effects. Replay reads events only, validates IDs/terminal accounting and aggregates by role/phase; compare rejects task/config/schema mismatches before metric deltas.

**Tech Stack:** Python 3.10+, JSONL, `uuid`, `unittest`.

---

### Task 1: Canonical schema, writer, replay and comparison

**Files:**
- Create: `orchestrator/canonical_trace.py`
- Test: `tests/test_canonical_trace.py`

- [ ] **Step 1: Write deterministic fixture tests**

```python
writer.emit("run_started", {"task_id": "task-a"})
writer.emit("run_completed", {"task_success": True})
replay = replay_trace(path)
self.assertTrue(replay["valid"])
self.assertEqual(replay["by_role"]["builder"]["llm_calls"], 1)
```

Include invalid fixtures for no terminal event, null task success, and aggregate/detail token mismatch; include a comparison fixture and a task-id mismatch rejection.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_canonical_trace -v`

Expected: FAIL because canonical trace APIs do not exist.

- [ ] **Step 3: Implement schema and offline APIs**

Define envelope fields `schema_version,event_id,run_id,seq,ts_ms,event_type,role,phase,payload`. Add `CanonicalTraceWriter.emit`, `replay_trace(path)`, and `compare_replays(baseline,candidate)`. Replay must never import Agent/tools or invoke subprocess; it validates one terminal event, non-null task success, unique IDs, detail-to-summary token conservation, and call counts.

- [ ] **Step 4: Run fixture tests**

Run: `python -m unittest tests.test_canonical_trace -v`

Expected: PASS.

### Task 2: Passive lifecycle instrumentation

**Files:**
- Modify: `agents.py`
- Modify: `tools.py`
- Modify: `orchestrator/scheduler.py`
- Modify: `metrics.py`
- Test: `tests/test_canonical_trace.py`

- [ ] **Step 1: Add integration fixture test**

Create a writer-backed run that emits run/phase/state, LLM request/result, tool request/result, safeguard, workspace mutation, and managed-process lifecycle events. Assert each emitted event has a unique ID and the replay aggregate is valid.

- [ ] **Step 2: Implement side-effect-only events**

Emit events around existing boundaries only: Scheduler run/state/phase transitions and terminal status; Agent LLM request/result; tool dispatch, safeguard decision, mutation result, process lifecycle; Metrics final summary with git commit, feature flags, model config and task success. Do not change prompts, agent decisions, tool execution order, or retry policy.

- [ ] **Step 3: Run focused tests**

Run: `python -m unittest tests.test_canonical_trace tests.test_metrics tests.test_tools tests.test_scheduler -v`

Expected: PASS.

### Task 3: Full verification

- [ ] **Step 1: Run full suite**

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 2: Review scope**

Run: `git diff --check; git diff -- agents.py tools.py metrics.py orchestrator/scheduler.py orchestrator/canonical_trace.py tests/test_canonical_trace.py`

Expected: trace/replay-only changes; no Prompt, Profile strategy, Scheduler decision, or benchmark-run changes.

- [ ] **Step 3: Commit**

```bash
git add agents.py tools.py metrics.py orchestrator/scheduler.py orchestrator/canonical_trace.py tests/test_canonical_trace.py
git commit -m "feat: add canonical trace replay"
```

Do not commit without explicit user approval.

