# Benchmarks

Adapters for running the harness agent on standard evaluation benchmarks.

## Terminal-Bench 2.0 (via Harbor)

### Prerequisites

```bash
# Install harbor framework
pip install harbor

# Docker must be running (or use --env daytona for cloud)
docker info

# Export your API credentials
export $(grep -v '^#' .env | xargs)
```

### Run

```bash
# Test on a single task
harbor run -d "terminal-bench@2.0" \
  --agent-import-path benchmarks.harbor_agent:HarnessAgent \
  --task-names hello-world

# Full benchmark
harbor run -d "terminal-bench@2.0" \
  --agent-import-path benchmarks.harbor_agent:HarnessAgent

# With Daytona (no local Docker needed)
harbor run -d "terminal-bench@2.0" \
  --agent-import-path benchmarks.harbor_agent:HarnessAgent \
  --env daytona
```

### How it works

1. Harbor spins up a Docker container (or Daytona sandbox) for each task
2. `HarnessAgent.install()` installs Python + deps + clones our repo inside the container
3. Harbor runs `python3 harness.py --profile terminal "<task>"` in the container
4. Our agent's `run_bash` executes commands natively — no bridging needed
5. Harbor evaluates the result using the task's `tests/test.sh`
