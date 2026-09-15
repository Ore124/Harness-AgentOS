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
HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER = os.environ.get(
    "HARNESS_ACCEPTANCE_PROGRESS_CONTROLLER", "1"
) != "0"

# --- Paths ---
WORKSPACE = os.path.abspath(os.environ.get("HARNESS_WORKSPACE", "./workspace"))
WEB_TERMINAL_ENABLED = os.getenv("HARNESS_WEB_TERMINAL_ENABLED", "0") == "1"
WEB_TERMINAL_TOKEN = os.getenv("HARNESS_WEB_TERMINAL_TOKEN", "")
SPEC_FILE = "spec.md"
FEEDBACK_FILE = "feedback.md"
CONTRACT_FILE = "contract.md"
PROGRESS_FILE = "progress.md"

# --- Durable runtime ---
HARNESS_RUNTIME = os.environ.get("HARNESS_RUNTIME", "legacy").strip().lower()
DURABLE_PROJECTS = tuple(
    value.strip()
    for value in os.environ.get("HARNESS_DURABLE_PROJECTS", "").split(",")
    if value.strip()
)
DURABLE_ROLLOUT_PERCENT = int(
    os.environ.get("HARNESS_DURABLE_ROLLOUT_PERCENT", "0")
)
DATABASE_URL = os.environ.get(
    "HARNESS_DATABASE_URL",
    f"sqlite:///{Path(WORKSPACE).resolve() / '.harness' / 'durable.db'}",
)
DURABLE_EXECUTION_CAPABILITY = os.environ.get(
    "HARNESS_EXECUTION_CAPABILITY", "local"
)
WORKER_EXECUTOR = os.environ.get("HARNESS_WORKER_EXECUTOR", "local").strip().lower()
AGENT_SANDBOX_IMAGE = os.environ.get(
    "HARNESS_AGENT_SANDBOX_IMAGE", "harness-runtime:latest"
)
AGENT_EGRESS_NETWORK = os.environ.get("HARNESS_AGENT_EGRESS_NETWORK", "") or None
AGENT_EGRESS_PROXY = os.environ.get("HARNESS_AGENT_EGRESS_PROXY", "") or None
AGENT_NETWORK_HOSTS = tuple(
    value.strip()
    for value in os.environ.get("HARNESS_AGENT_NETWORK_HOSTS", "api.openai.com").split(",")
    if value.strip()
)
WORKER_CAPABILITIES = tuple(
    value.strip()
    for value in os.environ.get("HARNESS_WORKER_CAPABILITIES", "local").split(",")
    if value.strip()
)
WORKER_HEARTBEAT_SECONDS = float(
    os.environ.get("HARNESS_WORKER_HEARTBEAT_SECONDS", "15")
)
WORKER_POLL_SECONDS = float(os.environ.get("HARNESS_WORKER_POLL_SECONDS", "1"))
RETRY_BACKOFF_SECONDS = float(os.environ.get("HARNESS_RETRY_BACKOFF_SECONDS", "5"))
GIT_REPOSITORY = os.environ.get("HARNESS_GIT_REPOSITORY", "").strip()
GIT_BASE_REVISION = os.environ.get("HARNESS_GIT_BASE_REVISION", "HEAD").strip()
GIT_TARGET_BRANCH = os.environ.get("HARNESS_GIT_TARGET_BRANCH", "main").strip()
GIT_WORKTREE_ROOT = os.environ.get(
    "HARNESS_GIT_WORKTREE_ROOT",
    str(Path(WORKSPACE).resolve() / ".harness" / "worktrees"),
)

# --- Team security ---
AUTH_MODE = os.environ.get("HARNESS_AUTH_MODE", "disabled").strip().lower()
OIDC_ISSUER = os.environ.get("HARNESS_OIDC_ISSUER", "")
OIDC_AUDIENCE = os.environ.get("HARNESS_OIDC_AUDIENCE", "")
OIDC_JWKS_URL = os.environ.get("HARNESS_OIDC_JWKS_URL", "")

# --- Artifact storage ---
ARTIFACT_STORE = os.environ.get("HARNESS_ARTIFACT_STORE", "local").strip().lower()
ARTIFACT_ROOT = os.environ.get(
    "HARNESS_ARTIFACT_ROOT",
    str(Path(WORKSPACE).resolve() / ".harness" / "artifacts"),
)
S3_BUCKET = os.environ.get("HARNESS_S3_BUCKET", "")
S3_PREFIX = os.environ.get("HARNESS_S3_PREFIX", "harness-artifacts")
S3_ENDPOINT_URL = os.environ.get("HARNESS_S3_ENDPOINT_URL", "") or None
S3_PUBLIC_ENDPOINT_URL = os.environ.get("HARNESS_S3_PUBLIC_ENDPOINT_URL", "") or None

# --- Observability ---
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "harness-runtime")
OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
