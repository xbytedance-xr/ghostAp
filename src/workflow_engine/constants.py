"""Default constants for the Workflow Engine."""

from __future__ import annotations

# --- Timeouts ---
AGENT_CALL_TIMEOUT_S: int = 300  # Per agent() call timeout (seconds)
WORKFLOW_TOTAL_TIMEOUT_S: int = 1800  # Total workflow execution timeout (30 min)
SESSION_CREATE_TIMEOUT_S: int = 100

# --- Concurrency ---
DEFAULT_MAX_CONCURRENT: int = 10  # Default parallel agent slots
HARD_MAX_CONCURRENT: int = 16  # Absolute ceiling regardless of config
MAX_TOTAL_AGENTS: int = 200  # Max agent() calls per workflow run (safety fuse)

# --- Nesting ---
MAX_NESTING_DEPTH: int = 3  # Max sub-workflow nesting (parent→child→grandchild)

# --- Tool descriptions (DEPRECATED — use tool_registry.get_available_tools()) ---
# Kept as import-time fallback; runtime code should use the registry.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "coco": "全栈编程·支持 subagent",
    "aiden": "代码审查·架构设计",
    "codex": "OpenAI 自主编程",
    "claude": "Anthropic 深度推理",
    "traex": "高并发推理·轻量任务",
    "gemini": "Google 多模态推理",
    "ttadk": "TTADK CLI 桥接",
}

# --- Journal ---
JOURNAL_DIR: str = ".ghostap/workflow_journal"
DEFAULT_CACHE_MAX_ENTRIES: int = 100  # Hard cap for in-memory LRU cache size
WORKFLOW_TEMPLATES_DIR: str = ".ghostap/workflows"
GLOBAL_TEMPLATES_DIR: str = "~/.ghostap/workflows"

# --- User-level template namespacing ---
# Each user gets their own template directory to avoid cross-user conflicts.
# Format string accepts a user_id (open_id or similar stable identifier).
USER_WORKFLOW_DIR: str = "~/.ghostap/workflows/{user_id}/"

# --- Built-in template whitelist ---
# These templates are bundled with the application and cannot be modified
# or deleted by users. Names are derived at import time from the files in
# builtin_templates/*.js to guarantee the set never drifts from what is
# actually shipped.  Each entry is the filename without the .js extension.
def _discover_builtin_templates() -> frozenset[str]:
    """Scan the builtin_templates directory and return the set of names."""
    import os as _os
    from pathlib import Path as _Path

    builtin_dir = _Path(_os.path.dirname(__file__)) / "builtin_templates"
    try:
        return frozenset(
            path.stem for path in builtin_dir.iterdir()
            if path.is_file() and path.suffix == ".js" and path.stem
        )
    except OSError:
        # Fall back to a stable baseline so import never fails at runtime
        # (e.g. frozen binaries, filesystem errors at import time).
        return frozenset({
            "code-audit",
            "refactor-pipeline",
            "test-generation",
            "doc-generation",
            "performance-analysis",
            "adversarial-review",
            "batch-migration",
        })


BUILTIN_TEMPLATES: frozenset[str] = _discover_builtin_templates()

# --- Global template allowlist (user-scope enforcement) ---
# Template names that may be resolved from the shared GLOBAL_TEMPLATES_DIR
# (~/.ghostap/workflows/) by any user.  Templates stored under the user's
# own namespace (~/.ghostap/workflows/{user_id}/) are always visible to
# that user.  Anything else under GLOBAL_TEMPLATES_DIR is forbidden unless
# listed here.  Empty by default — add template names (without .js) to
# opt-in shared templates.
WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST: frozenset[str] = frozenset()

# --- Schema retry ---
SCHEMA_RETRY_MAX: int = 2  # Max retries when schema validation fails

# --- General retry ---
MAX_RETRIES: int = 3  # Max retries for transient agent call failures
RETRY_BACKOFF_BASE_S: float = 1.0  # Base delay for exponential backoff (seconds)

# --- Queue ---
MAX_QUEUE_SIZE: int = 10_000  # Max pending messages in bridge queue

# --- Runtime ---
RUNTIME_JS_PATH: str = "src/workflow_engine/runtime/runtime.js"
NODE_MIN_VERSION: tuple[int, ...] = (20, 0, 0)

# --- Progress ---
PROGRESS_DEBOUNCE_S: float = 2.0  # Max 1 card update per N seconds

# --- Template roots (trusted for sub-workflow loading) ---
# NOTE: Project root is implicitly trusted (cwd_realpath check in bridge.py)
TRUSTED_TEMPLATE_ROOTS: tuple[str, ...] = (
    "~/.ghostap/workflows",  # Global user templates
    # Builtin templates path is resolved at runtime via templates._BUILTIN_TEMPLATES_DIR
)

# --- Script generation ---
# Default agent type used for AI script generation (can be overridden per workflow)
DEFAULT_SCRIPT_GEN_AGENT_TYPE: str = "coco"
# Keep backward compatibility
SCRIPT_GEN_AGENT_TYPE: str = DEFAULT_SCRIPT_GEN_AGENT_TYPE

# ---------------------------------------------------------------------------
# Orchestrator agent selection
# ---------------------------------------------------------------------------

# Available orchestrator agents for workflow script generation
# Each entry: (agent_type, display_name, description)
ORCHESTRATOR_AGENT_OPTIONS: list[tuple[str, str, str]] = [
    ("coco", "Coco", "全栈编程·支持 subagent·默认推荐"),
    ("claude", "Claude", "Anthropic 深度推理·复杂任务编排"),
    ("aiden", "Aiden", "代码审查·架构设计"),
    ("codex", "Codex", "OpenAI 自主编程"),
    ("gemini", "Gemini", "Google 多模态推理"),
    ("traex", "Traex", "高并发推理·轻量任务"),
]

# Default orchestrator agent
DEFAULT_ORCHESTRATOR_AGENT: str = "coco"

# --- Engine state filenames ---
STATE_FILENAME: str = ".workflow_engine_state.json"
