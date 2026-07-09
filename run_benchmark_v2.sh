#!/bin/bash
# Optimized benchmark run for minimax m2.7
# Key changes from v1:
#   - Shorter system prompt (60% less tokens → faster API calls)
#   - TB2_TOOL_SCHEMAS: 5 tools vs 8 (34% smaller schema per call)
#   - Lower context thresholds (50k/100k → earlier compaction)
#   - Disabled parallel tool calls (minimax doesn't handle well)
#   - max_tokens 16384→8192 (forces concise output)
#   - run_bash default timeout 300→120 (faster failure detection)
#   - Earlier time warnings (45%/75% vs 55%/85%)
#   - Removed TaskTrackingMiddleware (saves LLM calls for _todo.md)
#   - Minimal ENV_BOOTSTRAP (4 commands vs 16)
#
# TB2 timeouts are FIXED by the benchmark (900s-12000s per task).
# We cannot change them. We must make the agent faster.

set -e

# Load env vars
export $(grep -v '^#' .env | grep -v '^\s*$' | xargs)

echo "=== Benchmark v2 Configuration ==="
echo "Model: $HARNESS_MODEL"
echo "Base URL: $OPENAI_BASE_URL"
echo "Parallel tool calls: ${ENABLE_PARALLEL_TOOL_CALLS:-0}"
echo "Compress threshold: ${COMPRESS_THRESHOLD:-50000}"
echo "Reset threshold: ${RESET_THRESHOLD:-100000}"
echo ""

harbor run \
  -d "terminal-bench@2.0" \
  --agent-import-path benchmarks.harbor_agent:HarnessAgent \
  -k 5 \
  --n-concurrent 1
