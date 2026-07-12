"""
Harness configuration.
Uses OpenAI-compatible API so it works with any provider.

Setup:
  cp .env.template .env   # then fill in your real values
"""
import os
from pathlib import Path


def _load_dotenv():
    """Load .env file if it exists. No third-party dependency needed."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # .env keeps historical priority by default. Benchmark runners can set
        # HARNESS_DOTENV_OVERRIDE_ENV=0 so per-run env vars control workspace
        # and feature flags without editing the user's .env file.
        if key and (os.environ.get("HARNESS_DOTENV_OVERRIDE_ENV", "1") == "1" or key not in os.environ):
            os.environ[key] = value


_load_dotenv()

# --- API ---
API_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("HARNESS_MODEL", "gpt-4o")

# --- Token budgets ---
# Lower thresholds for models with smaller effective context windows.
# Aggressive compaction keeps the model focused and reduces latency.
COMPRESS_THRESHOLD = int(os.environ.get("COMPRESS_THRESHOLD", "50000"))
RESET_THRESHOLD = int(os.environ.get("RESET_THRESHOLD", "100000"))

# --- Harness loop ---
MAX_HARNESS_ROUNDS = int(os.environ.get("MAX_HARNESS_ROUNDS", "5"))
PASS_THRESHOLD = float(os.environ.get("PASS_THRESHOLD", "7.0"))

# --- Agent limits ---
# NOTE: Do NOT use iteration count as the primary stop condition.
# With ~8-9s per iteration, 80 iterations = ~700s, which silently
# truncates 900s+ tasks. Use a high ceiling here; TimeBudgetMiddleware
# handles the real time-based stop.
MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "500"))
MAX_TOOL_ERRORS = 5           # consecutive tool errors before abort

# --- Parallel tool calls ---
# Only enable for models that reliably produce valid parallel tool calls
# (e.g. Claude, GPT-4o). Disable for models that struggle with it.
ENABLE_PARALLEL_TOOL_CALLS = os.environ.get("ENABLE_PARALLEL_TOOL_CALLS", "0") == "1"

# --- Optimization feature flags ---
# Metrics are passive and low-overhead; all behavior-changing optimizations are
# disabled by default until benchmarked independently.
HARNESS_METRICS_ENABLED = os.environ.get("HARNESS_METRICS_ENABLED", "1") != "0"
HARNESS_PROMPT_PREFIX_V2 = os.environ.get("HARNESS_PROMPT_PREFIX_V2", "0") == "1"
HARNESS_DETERMINISTIC_OUTPUT_COMPRESSION = os.environ.get("HARNESS_DETERMINISTIC_OUTPUT_COMPRESSION", "0") == "1"
HARNESS_TOOL_CACHE = os.environ.get("HARNESS_TOOL_CACHE", "0") == "1"
HARNESS_STATE_VECTOR = os.environ.get("HARNESS_STATE_VECTOR", "0") == "1"
HARNESS_TOKEN_GOVERNOR = os.environ.get("HARNESS_TOKEN_GOVERNOR", "0") == "1"
HARNESS_PARALLEL_READ_TOOLS = os.environ.get("HARNESS_PARALLEL_READ_TOOLS", "0") == "1"
HARNESS_EVIDENCE_GUIDED_RECOVERY = os.environ.get("HARNESS_EVIDENCE_GUIDED_RECOVERY", "1") == "1"

# --- Paths ---
WORKSPACE = os.path.abspath(os.environ.get("HARNESS_WORKSPACE", "./workspace"))
WEB_TERMINAL_ENABLED = os.getenv("HARNESS_WEB_TERMINAL_ENABLED", "0") == "1"
WEB_TERMINAL_TOKEN = os.getenv("HARNESS_WEB_TERMINAL_TOKEN", "")
SPEC_FILE = "spec.md"
FEEDBACK_FILE = "feedback.md"
CONTRACT_FILE = "contract.md"
PROGRESS_FILE = "progress.md"
