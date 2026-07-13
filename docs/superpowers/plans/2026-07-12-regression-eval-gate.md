# Regression Eval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a pure offline fast gate that rejects unsafe, invalid, incomparable, unsuccessful, or materially slower candidate traces.

**Architecture:** Build `orchestrator/regression_gate.py` on canonical replay records and expose it through a small CLI. The gate reads JSONL only, pairs samples by task identity, and returns one structured PASS, FAIL, or INCONCLUSIVE report.

**Tech Stack:** Python 3.10+, JSONL, argparse, unittest.

---

### Task 1: Offline gate and compatibility metadata

**Files:**
- Create: `orchestrator/regression_gate.py`
- Modify: `orchestrator/scheduler.py`
- Modify: `agents.py`
- Test: `tests/test_regression_gate.py`

- [ ] **Step 1: Write deterministic gate fixtures**

```python
report = evaluate_gate(baseline_paths, candidate_paths)
self.assertEqual(report["conclusion"], "PASS")
```

Include unsafe-command failure, invalid-terminal inconclusive, workspace mismatch inconclusive, and a correctness regression failure.

- [ ] **Step 2: Implement replay-only evaluation**

```python
def evaluate_gate(baseline_paths, candidate_paths, performance_tolerance=0.05):
    """Replay trace paths only and return a JSON-serializable verdict."""
```

Reject missing initial-workspace metadata, aggregate success and normalized token/wall/LLM/round metrics, and scan requested command/path events for safety violations.

- [ ] **Step 3: Add passive metadata**

Emit `initial_workspace` on `run_started`, an `agent_round` for each managed iteration, and tool command/path metadata. These are observational fields only.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_regression_gate -v`

Expected: PASS.

### Task 2: Fast CLI and verification

**Files:**
- Create: `scripts/run_fast_regression_gate.py`
- Test: `tests/test_regression_gate.py`

- [ ] **Step 1: Add the command entry point**

```powershell
python scripts/run_fast_regression_gate.py --baseline trace-a.jsonl --candidate trace-b.jsonl
```

Print the structured report and return non-zero for FAIL only.

- [ ] **Step 2: Add four small deterministic performance cases**

Use four fixture task identities with fixed token, wall, call, and round totals. Assert PASS for equal/lower totals and FAIL above the five-percent tolerance.

- [ ] **Step 3: Run full validation**

Run: `python -m unittest discover -s tests -v`

Expected: PASS.
