# Evaluator Gate Negative Result

Date: 2026-07-12

Experiment: `HARNESS_EVALUATOR_GATE`

Goal:
- Reduce repeated or premature full Evaluator invocations when no effective changes occurred since the previous evaluation.
- Do not change evaluator prompt, evaluator model, prompt cache, or builder behavior.
- Use programmatic signals only; do not add an LLM decision for skipping.

Baseline:
- Task: `python harness.py "Build a Pomodoro timer with start, pause, reset buttons. Single HTML file."`
- User-provided baseline metrics:

| Metric | Baseline |
| --- | ---: |
| Evaluator calls | 22 |
| Evaluator input tokens | 198,823 |
| Evaluator total tokens | 201,834 |
| Total LLM calls | 65 |
| Total input tokens | 387,129 |
| Total tokens | 403,295 |

Candidate flags:
- Same task.
- `HARNESS_EVALUATOR_GATE=1`
- `HARNESS_PROMPT_PREFIX_V2=0`
- `HARNESS_DETERMINISTIC_OUTPUT_COMPRESSION=0`
- `HARNESS_TOOL_CACHE=0`
- `HARNESS_STATE_VECTOR=0`
- `HARNESS_TOKEN_GOVERNOR=0`
- `HARNESS_PARALLEL_READ_TOOLS=0`

Candidate result:

| Metric | Baseline | Candidate | Delta |
| --- | ---: | ---: | ---: |
| Success rate | unknown | not completed | invalid |
| Evaluator calls | 22 | 15 | -7 |
| Evaluator skipped | 0 | 0 | 0 |
| Evaluator input tokens | 198,823 | 111,271 | -87,552 |
| Evaluator total tokens | 201,834 | 113,965 | -87,869 |
| Total LLM calls | 65 | 82 | +17 |
| Total input tokens | 387,129 | 339,134 | -47,995 |
| Total tokens | 403,295 | 362,535 | -40,760 |

Candidate workspace:
- `benchmark_runs/evaluator_gate_real_pomodoro_candidate/20260712-100020_build-a-pomodoro-timer-with-start-pause`

Observed failure:
- Candidate did not complete; `task_success` remained `null`.
- `evaluator_skipped_count` was `0`, so no Evaluator invocation was safely skipped.
- Evaluator token reduction was caused by premature termination, not by the gate.
- The builder executed `taskkill /f /im python.exe`, which killed the running harness process and local Python services.

Conclusion:
- The Evaluator Gate did not satisfy acceptance criteria on the real Pomodoro task.
- Per experiment rules, because no Evaluator call was safely skipped and the candidate did not complete, this optimization has no demonstrated opportunity for this benchmark.
- The Evaluator Gate implementation was reverted.
- Do not continue adding complexity to this experiment unless a future benchmark demonstrates safe skips on real tasks.

Follow-up candidate:
- Investigate command safety for `run_bash`, especially broad process-kill commands such as `taskkill /f /im python.exe`.
