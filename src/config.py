import shlex
import threading
from typing import Optional, Callable

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.utils.env import is_test_environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_id: str = ""
    app_secret: str = ""

    ark_api_key: str = ""
    ark_model: str = ""
    ark_base_url: str = "https://ark-cn-beijing.bytedance.net/api/v3"

    sandbox_timeout: int = 30
    sandbox_max_output_length: int = 4000
    sandbox_command_blacklist: str = (
        "rm -rf /,rm -rf /*,mkfs,dd if=,shutdown,reboot,halt,poweroff,init 0,init 6,:(){ :|:& };:"
    )
    sandbox_use_whitelist: bool = False
    sandbox_command_whitelist: str = ""

    coco_execution_timeout: int = 7200
    coco_session_timeout: int = 86400
    coco_max_output_length: int = 30000
    coco_default_model: str = ""

    claude_execution_timeout: int = 7200
    claude_session_timeout: int = 86400
    claude_max_output_length: int = 30000

    # ACP agent process startup timeout (seconds)
    acp_startup_timeout: int = 20

    # ACP agent startup retries (1 means no retry)
    acp_startup_retries: int = 2

    # ACP health check timeout (seconds)
    acp_healthcheck_timeout: float = 2.0

    # ACP permission auto-approve (True = agent actions auto-approved, False = denied by default)
    acp_permission_auto_approve: bool = True

    # Auto-update agent CLI when ACP server mode is not supported
    acp_auto_update: bool = True
    # Timeout for agent CLI auto-update subprocess (seconds)
    acp_auto_update_timeout: int = 120

    # Engine eval prompt timeout (seconds) — used by Loop and Spec engines
    engine_eval_prompt_timeout: int = 60

    # ACP stdio stream buffer limit (bytes). Default asyncio limit is 64KB which
    # is too small for large agent responses (code generation, file contents).
    # Set to 0 to use the asyncio default (64KB). 10MB should be generous enough.
    acp_stream_buffer_limit: int = 10 * 1024 * 1024

    acp_keepalive_interval: int = 300

    acp_session_idle_healthcheck_s: float = 120.0

    # Maximum characters for file content in ACP read/write operations
    acp_max_file_chars: int = 200_000

    # ------------------------------------------------------------------
    # ACP startup diagnostics (redaction + truncation)
    # ------------------------------------------------------------------
    # Safety-first: redact sensitive values from diagnostics logs.
    acp_diagnostics_redact_enabled: bool = True
    acp_diagnostics_redact_replacement: str = "***REDACTED***"
    # Regex patterns applied to args/stdout_snippet/stderr_snippet/spec strings.
    # NOTE: Keep patterns conservative to avoid excessive false positives.
    acp_diagnostics_redact_patterns: list[str] = [
        r"(?i)authorization\s*:\s*[^\s]+",
        r"(?i)bearer\s+[^\s]+",
        r"sk-[A-Za-z0-9]{10,}",
        r"AKIA[0-9A-Z]{16}",
        r"(?i)api[_-]?key\s*[:=]\s*[^\s]+",
        r"(?i)secret\s*[:=]\s*[^\s]+",
        r"(?i)token\s*[:=]\s*[^\s]+",
    ]
    # Unified truncation limits for diagnostics output.
    # - args_limit: approximated length of joined args (best-effort)
    # - snippet_limit: stdout/stderr snippet length
    # - total_limit: final formatted JSON line length
    acp_diagnostics_args_limit: int = 600
    acp_diagnostics_snippet_limit: int = 240
    acp_diagnostics_total_limit: int = 2000

    # Claude CLI backend: skip Claude's built-in permission checks.
    # GhostAP has its own sandbox safety layer, so this is usually safe.
    claude_cli_skip_permissions: bool = True

    # ACP agent command overrides (optional)
    # Example:
    #   COCO_ACP_CMD=coco
    #   COCO_ACP_ARGS="acp serve"
    coco_acp_cmd: str = ""
    coco_acp_args: str = ""
    claude_acp_cmd: str = ""
    claude_acp_args: str = ""

    ttadk_auto_update: bool = True
    ttadk_update_timeout: int = 120

    ttadk_default_tool: str = "coco"
    ttadk_default_model: str = ""
    ttadk_yolo_default_enabled: bool = False
    # Max models to probe in interactive PTY discovery (clamped to [1, 50])
    ttadk_interactive_max_models: int = 12

    # TTADK common tool model preheating (probe-based, best-effort)
    # - enabled: master switch
    # - on_startup: trigger once on application startup
    # - on_first_use: trigger once when first accessing a tool's models
    # - tools: comma/space separated tool names
    # - timeout: probe subprocess timeout (seconds)
    ttadk_preheat_enabled: bool = True
    ttadk_preheat_on_startup: bool = True
    ttadk_preheat_on_first_use: bool = True
    ttadk_preheat_tools: str = "claude,coco,trae,opencode,codex"
    ttadk_preheat_timeout: float = 2.5

    # TTADK model list fetch strategy knobs
    # Interactive strategy is risky in multi-threaded service (pty + fork), so it is disabled by default.
    ttadk_interactive_enabled: bool = False
    ttadk_probe_timeout: float = 10.0
    ttadk_structured_timeout: float = 8.0

    # TTADK official CLI models strategy (non-PTY, preferred when available)
    # - enabled: master switch for official_cli strategy
    # - timeout: subprocess timeout (seconds)
    ttadk_official_cli_enabled: bool = True
    ttadk_official_timeout: float = 4.0

    # TTADK CLI capabilities probe (`ttadk --help`)
    # - ttl_s: cache TTL for parsed Commands list
    # - timeout_s: subprocess timeout for `ttadk --help`
    ttadk_cli_capabilities_ttl_s: float = 300.0
    ttadk_cli_capabilities_timeout_s: float = 2.0

    # TTADK model fetch strategy order (comma/space separated strategy names)
    # Supported names: official_cli, structured_sync, file_cache, local_config, probe, interactive
    # Empty means "use built-in conservative defaults".
    ttadk_models_strategy_order: str = ""

    # ------------------------------------------------------------------
    # TTADK model cache (service-side disk cache, project-scoped)
    # ------------------------------------------------------------------
    # Cache file path template. If empty, defaults to "{cwd}/.ghostap/ttadk/models_cache.json".
    # Supports "{cwd}" placeholder. If cwd is empty/None, disk cache is disabled.
    ttadk_models_cache_path: str = ""
    # Backward-compat: read legacy "~/.ttadk/models_cache.json" when project cache missing.
    ttadk_models_cache_read_legacy_home: bool = True
    # Whether to auto-migrate legacy cache content into project cache on first load.
    ttadk_models_cache_migrate_from_legacy_home: bool = True

    # TTADK cwd normalization diagnostics
    # - enabled: emit debug logs for raw/normalized cwd at key call sites
    ttadk_cwd_debug_enabled: bool = False

    # ------------------------------------------------------------------
    # TTADK subprocess env sandbox (avoid writing to real ~/.ttadk)
    # ------------------------------------------------------------------
    # Enable sandboxed HOME/XDG_* for all ttadk-related subprocess/PTY calls.
    # Default: enabled to avoid test/runtime polluting user's real HOME.
    ttadk_sandbox_home_enabled: bool = True
    # Sandbox root directory. If empty, defaults to "<cwd>/.ttadk_sandbox".
    # Supports "{cwd}" placeholder.
    ttadk_sandbox_home_root: str = ""
    # Whether to also override XDG_CACHE_HOME under the sandbox root.
    ttadk_sandbox_cover_cache_home: bool = False

    # TTADK runtime invalid-model self-healing (execution-time)
    # - enabled: master switch
    # - allow_autoswitch: when available models are known, allow selecting a best-match real model for one retry
    # - cooldown: per-tool cooldown to avoid repeated retries (seconds)
    # - max_retries: hard cap (kept as 1 for safety)
    ttadk_runtime_retry_enabled: bool = True
    ttadk_runtime_retry_allow_autoswitch: bool = True
    ttadk_runtime_retry_cooldown_s: float = 120.0
    ttadk_runtime_max_retries: int = 1

    # TTADK runtime invalid-model stub cooldown store limits (service-side)
    # Used only for non-TTADKManager manager stubs (tests/legacy path).
    # - ttl_s: cleanup entries older than ttl seconds; 0 disables TTL cleanup
    # - max_keys: hard cap for number of keys kept; 0 disables cap
    # - gc_interval_s: minimum seconds between GC runs; 0 runs GC on every write
    ttadk_runtime_stub_cooldown_ttl_s: float = 3600.0
    ttadk_runtime_stub_cooldown_max_keys: int = 1024
    ttadk_runtime_stub_cooldown_gc_interval_s: float = 60.0

    # TTADK startup: auto PTY retry when downstream requires a real TTY
    # - enabled: master switch
    # - retry_once: whether to retry exactly once with PTY on stdin-not-tty errors
    ttadk_pty_enabled: bool = True
    ttadk_pty_retry_once: bool = True
    # Cooldown for repeated PTY retries per tool (seconds)
    ttadk_pty_retry_cooldown_s: float = 60.0

    # Loop Engine settings
    loop_max_iterations: int = 100
    loop_execution_timeout: int = 7200
    loop_convergence_window: int = 3
    loop_max_context_tokens: int = 8000
    loop_default_max_retries: int = 2

    # Loop Engine multi-perspective review (Ralph Loop)
    loop_review_enabled: bool = True
    loop_review_extra_iterations: int = 3
    loop_review_timeout: int = 120

    # Loop Engine review failure circuit breaker
    # - enabled: master switch
    # - max_consecutive: open circuit after N consecutive review failures
    # - cooldown_iterations: keep circuit open for next K iterations (skip review)
    loop_review_failure_circuit_enabled: bool = True
    loop_review_failure_max_consecutive: int = 3
    loop_review_failure_cooldown_iterations: int = 3
    loop_review_failure_max_cooldown_iterations: int = 12
    loop_review_min_timeout: int = 30
    loop_review_hard_floor: int = 15

    # Loop Watchdog
    loop_watchdog_timeout: float = 300.0

    # Spec Engine settings
    spec_max_cycles: int = 500
    # Hard upper bound for long-range spec cycles (configurable via env).
    # Engine will clamp spec_max_cycles to this limit.
    spec_max_cycles_limit: int = 5000
    spec_execution_timeout: int = 7200
    spec_convergence_window: int = 2
    spec_min_cycles: int = 2
    spec_review_enabled: bool = True
    spec_review_timeout: int = 120
    spec_review_max_parallel: int = 2

    # Spec Engine review failure circuit breaker
    # - enabled: master switch
    # - max_consecutive: open circuit after N consecutive review failures
    # - cooldown_cycles: keep circuit open for next K cycles (skip review)
    spec_review_failure_circuit_enabled: bool = True
    spec_review_failure_max_consecutive: int = 3
    spec_review_failure_cooldown_cycles: int = 3
    spec_review_failure_max_cooldown_cycles: int = 12
    spec_review_min_timeout: int = 30
    spec_review_hard_floor: int = 15

    # Worktree dispatcher pool-level timeout (seconds)
    worktree_pool_timeout: int = 600

    # Review metrics exporter
    # - "logger" (default): output via logging.info (original behaviour)
    # - "jsonl": append JSON Lines to review_metrics_jsonl_path
    review_metrics_exporter_type: str = "logger"
    review_metrics_jsonl_path: str = "review_metrics.jsonl"

    # Sliding window dynamic circuit breaker (shared by Spec & Loop)
    # - window_size: number of recent review outcomes to track (min 3)
    # - success_rate_threshold: open circuit if success_rate < threshold
    review_circuit_window_size: int = 10
    review_circuit_success_rate_threshold: float = 0.3

    # Review circuit-breaker lint fallback (run local lint when circuit is open)
    review_circuit_lint_fallback_enabled: bool = True
    review_circuit_lint_timeout: int = 10

    # Spec long-range persistence / monitoring
    spec_state_filename: str = ".spec_engine_state.json"
    spec_artifacts_dirname: str = ".spec_engine"
    # Keep in-memory phase outputs bounded for 5k+ cycles
    spec_cycle_output_max_chars: int = 4000
    spec_cycle_tasks_max: int = 50
    # Persisted artifact bounds / retention (avoid 5k cycles generating huge disk usage)
    spec_phase_output_persist_max_chars: int = 20000
    spec_cycle_artifact_retention: int = 50
    # Whether to persist phase raw outputs (spec/plan/tasks/build/review) to disk.
    # Metrics/state/spec files are still persisted for long-range monitoring/resume.
    spec_persist_phase_artifacts: bool = True
    # Post-cycle self-questioning (problem discovery) + spec generation
    spec_discovery_enabled: bool = True
    spec_discovery_max_questions: int = 5
    spec_discovery_force_nonempty: bool = True
    spec_generated_specs_per_cycle: int = 3
    # Persistence cadence
    spec_persist_every_phase: bool = True
    spec_allow_resume_from_disk: bool = True
    # Continuation policy
    # - infinite_mode: never stop due to convergence/early-stop; only stop on success/user stop/max_cycles
    spec_infinite_mode: bool = False
    spec_disable_convergence: bool = False
    spec_disable_early_stop: bool = False
    spec_rebuild_session_between_cycles: bool = True
    # State file compaction (avoid O(n^2) rewrite cost for 5k cycles)
    spec_state_cycles_tail: int = 50
    spec_state_work_items_tail: int = 200
    spec_state_metrics_tail: int = 200

    # History / retention
    spec_history_log_filename: str = "history.jsonl"
    spec_max_retries: int = 3
    spec_model_switch_enabled: bool = True
    spec_generated_specs_retention: int = 1000
    # Override hint: when set (non-empty), mask BUILD phase errors to "Internal error"
    spec_failed_task_id_override: str = ""

    streaming_enabled: bool = True

    # Feishu WebSocket reconnect delay (seconds) when underlying client exits unexpectedly
    feishu_ws_reconnect_delay_s: float = 5.0

    # Feishu WebSocket watchdog interval (seconds)
    feishu_ws_watchdog_interval: float = 60.0

    # Streaming flow control (Adaptive interval)
    streaming_adaptive_interval_base: float = 0.3  # Base interval (seconds) for low rate
    streaming_adaptive_interval_max: float = 2.0  # Max interval (seconds) for high rate
    streaming_adaptive_rate_low: float = 20.0  # Low rate threshold (chars/sec)
    streaming_adaptive_rate_high: float = 150.0  # High rate threshold (chars/sec)

    # ------------------------------------------------------------------
    # IM API / Deep Streaming Control
    # ------------------------------------------------------------------
    # Maximum retries for IM API patch operations (default: 3)
    im_api_max_retries: int = 3

    # Deep engine streaming update throttling
    # - interval: minimum seconds between updates (unless forced)
    # - min_chars: minimum new characters accumulated before updating (unless forced/interval passed)
    deep_stream_interval: float = 2.5
    deep_stream_min_chars: int = 350
    
    # Deep engine memory monitoring (percentage)
    deep_memory_threshold: float = 80.0

    # Rate limiting handling (auto-pause and retry on API throttling)
    rate_limit_retry_enabled: bool = True
    rate_limit_max_wait: int = 300  # Max seconds to wait for rate limit cooldown
    rate_limit_base_wait: int = 30  # Default wait if no retry-after header
    rate_limit_max_retries: int = 5  # Max consecutive rate limit retries

    # Engine timeout warning threshold (seconds) for long-running tasks
    engine_timeout_warning_seconds: int = 600

    # ------------------------------------------------------------------
    # Model failure self-healing (send_prompt-time)
    # ------------------------------------------------------------------
    # need compaction / loop detected 防抖与 failover 参数
    model_failure_compaction_enabled: bool = True
    model_failure_compaction_loop_window_s: float = 180.0
    model_failure_compaction_loop_max: int = 2
    # failover mapping (comma/space separated: "from:to")
    # default: gpt-5.2 -> gpt-5.1
    model_failure_failover_map: str = "gpt-5.2:gpt-5.1"

    # Task scheduler (thread-based) settings
    task_scheduler_max_concurrent: int = 20
    task_scheduler_per_key_concurrency: int = 1

    # 卡片按钮布局策略：
    # - desktop: 使用飞书 action 原生布局（更贴近桌面端观感）
    # - mobile: 强制两列 column_set（手机端更稳定，一行两个按钮）
    # - responsive: 默认值；<=2 个按钮用 action，>2 个按钮用两列 column_set
    card_button_layout: str = "responsive"

    # 卡片按钮尺寸 (medium/small/large)
    card_button_size: str = "medium"

    # 移动端强制垂直布局 (true: 移动端忽略 layout 设置，强制垂直堆叠; false: 遵循 layout 设置)
    card_mobile_force_vertical: bool = True

    # 移动端布局模式 (vertical: 垂直堆叠; flow: 流式自动换行)
    # 当 card_mobile_force_vertical=True 且按钮数 > 2 时，此配置生效
    card_mobile_layout_mode: str = "vertical"

    # Deep Card Compact Mode
    # If True, deep progress cards will default to a compact view (status line + progress bar + truncated content).
    # Default is False so users see full content by default — the expand/collapse
    # button still exists for manual control when content exceeds the full threshold.
    card_deep_compact_default: bool = False

    # 卡片流式更新最大字符数（避免 PATCH 载荷过大）
    card_max_chars: int = 28000

    # UI Optimization Settings
    # Pagination size for project board and other lists
    ui_page_size: int = 5
    # Max output length for logs/shell/details before truncation
    ui_max_output_len: int = 2000

    # 消息回复模式配置
    # - direct: 直接回复（消息显示在被回复消息下方）
    # - thread: 话题回复（使用 reply_in_thread=True，消息会显示在独立话题区域，更整洁）
    #
    # smart_reply_mode: 智能模式下的回复方式（默认 direct，群内直接引用消息回复）
    # default_reply_mode: 其他模式（Coco/Claude/Shell/Deep等）的回复方式（默认 thread，话题回复更整洁）
    smart_reply_mode: str = "direct"
    default_reply_mode: str = "thread"

    thread_programming_enabled: bool = True
    thread_context_ttl: int = 86400 * 7

    # ref-note 关联信息开关（默认关闭，调试时可通过 .env 设置 REF_NOTE_ENABLED=true）
    ref_note_enabled: bool = False

    @property
    def command_blacklist(self) -> list[str]:
        return [cmd.strip() for cmd in self.sandbox_command_blacklist.split(",") if cmd.strip()]
    
    @property
    def command_whitelist(self) -> list[str]:
        return [cmd.strip() for cmd in self.sandbox_command_whitelist.split(",") if cmd.strip()]

    def validate_feishu_config(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def validate_ark_config(self) -> bool:
        return bool(self.ark_api_key and self.ark_model)

    def get_acp_command(self, agent_type: str) -> tuple[str, list[str]]:
        """Return (cmd, args) override for an ACP agent, if configured."""
        agent_type = (agent_type or "").lower()
        if agent_type == "coco" and self.coco_acp_cmd:
            return self.coco_acp_cmd, shlex.split(self.coco_acp_args or "")
        if agent_type == "claude" and self.claude_acp_cmd:
            return self.claude_acp_cmd, shlex.split(self.claude_acp_args or "")
        return "", []


_settings: Optional[Settings] = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = Settings()
    return _settings


def set_settings(
    settings: Settings, 
    *, 
    is_test_env_check: Optional[Callable[[], bool]] = None
) -> None:
    """Set the global settings singleton. For dependency injection/testing.
    
    Args:
        settings: The Settings instance to use globally
        is_test_env_check: Optional custom function to check if we're in a test environment.
                           If not provided, uses the default `is_test_environment()` function.
    
    Raises:
        RuntimeError: If called in a production (non-test) environment
    """
    check_fn = is_test_env_check if is_test_env_check is not None else is_test_environment
    if not check_fn():
        raise RuntimeError(
            "set_settings() is only allowed in test environments. "
            "Modifying global singletons in production can cause race conditions."
        )
    global _settings
    with _settings_lock:
        _settings = settings


def _reset_settings_for_testing(
    *, 
    is_test_env_check: Optional[Callable[[], bool]] = None
) -> None:
    """Reset the global settings singleton. **Test-only.**
    
    Args:
        is_test_env_check: Optional custom function to check if we're in a test environment.
                           If not provided, uses the default `is_test_environment()` function.
    
    Raises:
        RuntimeError: If called in a production (non-test) environment
    """
    check_fn = is_test_env_check if is_test_env_check is not None else is_test_environment
    if not check_fn():
        raise RuntimeError(
            "_reset_settings_for_testing() is only allowed in test environments. "
            "Modifying global singletons in production can cause race conditions."
        )
    global _settings
    with _settings_lock:
        _settings = None
