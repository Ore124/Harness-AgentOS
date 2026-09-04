# Benchmarks

Adapters for running the harness agent on standard evaluation benchmarks.

## Acceptance-controller long-task A/B

The long-task runner creates one fixture per scenario/repeat, copies that exact
fixture to controller OFF and ON workspaces, runs the harness, and then runs an
independent acceptance command. Its JSON report separates correct completion
from erroneous completion and excludes invalid/timeout pairs from comparative
rates while retaining them as explicit evidence. Repeated samples alternate
OFF/ON execution order to reduce warm-up and time-of-run bias.

The default is a five-scenario smoke suite (ten agent runs):

```bash
python benchmarks/run_acceptance_longtask_benchmark.py
```

Run one scenario when checking API health or the runner itself:

```bash
python benchmarks/run_acceptance_longtask_benchmark.py \
  --scenario inventory-multifile \
  --output benchmark_runs/acceptance_inventory_report.json
```

The larger eight-scenario suite, including delegation, forced interruption /
resume, and a short-budget task, requires an explicit option:

```bash
python benchmarks/run_acceptance_longtask_benchmark.py --suite full --repeat 2
```

Scenario fixtures and independent acceptance commands live in
`acceptance_longtask_scenarios.json`. `target_repair_rounds` is an intended
difficulty range only; the report always records actual repair and repeated
verification counts rather than claiming the model took the target number of
rounds.

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
