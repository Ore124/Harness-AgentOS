# Phase 1 Prompt Prefix V2 Negative Result

Date: 2026-07-11

Experiment: `HARNESS_PROMPT_PREFIX_V2`

Baseline flags:
- `HARNESS_PROMPT_PREFIX_V2=0`
- `HARNESS_DETERMINISTIC_OUTPUT_COMPRESSION=0`
- `HARNESS_TOOL_CACHE=0`
- `HARNESS_STATE_VECTOR=0`
- `HARNESS_TOKEN_GOVERNOR=0`
- `HARNESS_PARALLEL_READ_TOOLS=0`

Candidate flags:
- Same as baseline, except `HARNESS_PROMPT_PREFIX_V2=1`

Smoke benchmark:
- Profile: `terminal`
- Repeat: 2
- Task: `Create a file named answer.txt containing exactly OK and verify it exists.`
- Report: `benchmark_runs/phase1_smoke_report.json`

Measured result:

| Metric | Baseline | Candidate | Delta |
| --- | ---: | ---: | ---: |
| Success rate | 1.000 | 1.000 | 0.000 |
| Total input tokens | 89,848 | 112,105 | +22,257 |
| Cached tokens | 86,854 | 108,825 | +21,971 |
| Output tokens | 794 | 845 | +51 |
| Total tokens / success | 45,321 | 56,475 | +11,154 |
| LLM calls / success | 9.0 | 11.0 | +2.0 |
| Agent rounds / success | 9.0 | 11.0 | +2.0 |
| Tool calls / success | 7.0 | 9.0 | +2.0 |
| Wall time / success | 17.165s | 18.792s | +1.627s |

Conclusion:
- Prompt cache had real hits in both variants.
- Candidate cache hit ratio was slightly higher, but total tokens and wall time increased.
- `HARNESS_PROMPT_PREFIX_V2` is a negative result and must remain disabled unless a future benchmark proves a net gain.
- Do not add new prompt rules for this experiment.
