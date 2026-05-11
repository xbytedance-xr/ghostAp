"""Settings — main application configuration model backed by pydantic-settings."""

import logging as _logging
import shlex
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .card import CardSessionConfig
from .spec import SpecReviewConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_id: str = ""
    app_secret: str = ""

    # Default ACP tool for SMART mode (e.g., "coco", "claude", "aiden", "codex", "gemini")
    # When set, unmatched messages in SMART mode are forwarded to this tool.
    # When empty, all unmatched messages are treated as shell commands.
    default_acp_tool: str = ""

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

    claude_execution_timeout: int = 7200
    claude_session_timeout: int = 86400
    claude_max_output_length: int = 30000

    # ACP session history directory (empty = default ~/.ghostap/acp_history)
    acp_history_dir: str = ""

    # ACP agent process startup timeout (seconds)
    acp_startup_timeout: int = 20

    # ACP agent startup retries (1 means no retry)
    acp_startup_retries: int = 2

    # ACP health check timeout (seconds)
    acp_healthcheck_timeout: float = 2.0

    # ACP model list probe timeout (seconds). Much larger than healthcheck:
    # cold-spawning `coco acp serve` + initialize + new_session round-trip is
    # highly variable and routinely takes 5-12s on first use (observed range
    # 4-12s). A tight 6s window times out often, and falling back to the static
    # DEFAULT_MODELS hides the real model list (GPT-5.x, GLM-5, Kimi, openrouter
    # pools, Gemini previews, …), so we give the probe a generous window before
    # degrading. The startup preheat keeps the 5min cache warm so /coco normally
    # never pays this cost interactively.
    acp_model_probe_timeout: float = 15.0

    # Warm up the coco ACP model list in the background at startup so the
    # interactive /coco model picker reads a fresh 5min-cached list instead of
    # paying the cold-spawn probe cost (and risking a timeout → stale defaults).
    acp_model_preheat_on_startup: bool = True

    # ACP permission auto-approve (True = agent actions auto-approved, False = denied by default)
    acp_permission_auto_approve: bool = True

    # Auto-update agent CLI when ACP server mode is not supported
    acp_auto_update: bool = True
    # Timeout for agent CLI auto-update subprocess (seconds)
    acp_auto_update_timeout: int = 120

    # Engine eval prompt timeout (seconds) — used by Spec engine
    engine_eval_prompt_timeout: int = 60

    # Engine auxiliary prompt timeout (seconds) — used by disposable ACP
    # sub-sessions for non-critical tasks such as Spec criteria decomposition.
    # Keep this aligned with SyncACPSession's default prompt timeout to avoid
    # cold-start/model-selection latency causing noisy 60s timeout errors before
    # the main Spec cycle even begins.
    engine_aux_prompt_timeout: int = 600

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

    # Spec Engine settings
    spec_max_cycles: int = 500
    # Hard upper bound for long-range spec cycles (configurable via env).
    # Engine will clamp spec_max_cycles to this limit.
    spec_max_cycles_limit: int = 5000
    spec_execution_timeout: int = 7200
    spec_convergence_window: int = 2
    spec_min_cycles: int = 2
    spec_review_enabled: bool = True
    spec_review_timeout: int = 240
    spec_review_max_parallel: int = 3

    # Spec Engine review failure circuit breaker
    # - enabled: master switch
    # - max_consecutive: open circuit after N consecutive review failures
    # - cooldown_cycles: keep circuit open for next K cycles (skip review)
    spec_review_failure_circuit_enabled: bool = True
    spec_review_failure_max_consecutive: int = 4
    spec_review_failure_cooldown_cycles: int = 2
    spec_review_failure_max_cooldown_cycles: int = 12
    spec_review_min_timeout: int = 60
    spec_review_hard_floor: int = 20

    # Spec Engine review in-cycle auto-retry (max_attempts=0 disables retry)
    spec_review_retry_max_delay: int = 30
    spec_review_retry_max_attempts: int = 2
    spec_review_retry_base_delay: float = 8.0
    spec_review_retry_decay_factor: float = 1.5

    # 审查解析失败时的默认判定 ("fail" = 视为未通过, "pass" = 视为通过)
    spec_review_parse_failure_default: Literal["pass", "fail"] = "fail"

    @property
    def spec_review(self) -> "SpecReviewConfig":
        """Structured view of spec review / retry / circuit-breaker settings."""
        return SpecReviewConfig(
            enabled=self.spec_review_enabled,
            timeout=self.spec_review_timeout,
            max_parallel=self.spec_review_max_parallel,
            min_timeout=self.spec_review_min_timeout,
            hard_floor=self.spec_review_hard_floor,
            retry_max_delay=self.spec_review_retry_max_delay,
            retry_max_attempts=self.spec_review_retry_max_attempts,
            retry_base_delay=self.spec_review_retry_base_delay,
            retry_decay_factor=self.spec_review_retry_decay_factor,
            failure_circuit_enabled=self.spec_review_failure_circuit_enabled,
            failure_max_consecutive=self.spec_review_failure_max_consecutive,
            failure_cooldown_cycles=self.spec_review_failure_cooldown_cycles,
            failure_max_cooldown_cycles=self.spec_review_failure_max_cooldown_cycles,
            parse_failure_default=self.spec_review_parse_failure_default,
        )

    # Worktree dispatcher pool-level timeout (seconds)
    worktree_pool_timeout: int = 600

    # Streaming card collapsible panels (tool calls / thoughts folded by default)
    # Engine card collapsible panels (Deep/Spec/Worktree: structured content with collapsible panels)
    engine_collapsible_enabled: bool = True

    # Streaming card auto-continuation (create new card when content exceeds threshold)
    # Card session / delivery / UI configuration (nested model)
    card: CardSessionConfig = CardSessionConfig()

    # Review metrics exporter
    # - "logger" (default): output via logging.info (original behaviour)
    # - "jsonl": append JSON Lines to review_metrics_jsonl_path
    review_metrics_exporter_type: str = "logger"
    review_metrics_jsonl_path: str = "review_metrics.jsonl"

    # Sliding window dynamic circuit breaker (used by Spec engine)
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
    # Discovery 门控（防空转）
    spec_discovery_gate_on_satisfied: bool = True  # AC 全满足后关闭 discovery
    spec_discovery_max_pending: int = 5  # backlog 达上限时跳过 discovery
    spec_discovery_cooldown_cycles: int = 3  # 无进展时每 N 轮才触发一次
    # Termination 增强
    spec_backlog_stuck_window: int = 0  # backlog_stuck 检测窗口 (0=禁用，要求全部消化)
    spec_success_ignore_backlog: bool = False  # success 判定时要求 backlog 清零
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
    # Max consecutive cycle failures before aborting (prevents infinite empty loops).
    spec_max_consecutive_failures: int = 3
    spec_model_switch_enabled: bool = True
    spec_generated_specs_retention: int = 1000
    # Override hint: when set (non-empty), mask BUILD phase errors to "Internal error"
    spec_failed_task_id_override: str = ""

    streaming_enabled: bool = True

    # Feishu WebSocket reconnect delay (seconds) when underlying client exits unexpectedly
    feishu_ws_reconnect_delay_s: float = 5.0

    # Feishu WebSocket watchdog interval (seconds)
    feishu_ws_watchdog_interval: float = 60.0

    # ------------------------------------------------------------------
    # Feishu WebSocket client runtime parameters
    # ------------------------------------------------------------------
    # 消息过期时间（秒），超时的历史消息不再处理
    message_expire_seconds: int = 30
    # 消息去重缓存 TTL（秒）
    message_cache_ttl: int = 300
    # 消息去重缓存最大容量
    message_cache_max_size: int = 1000
    # 消息去重缓存清理间隔（秒）
    message_cache_cleanup_interval: int = 60
    # 系统命令并发数
    system_command_concurrency: int = 10
    # Spec 引擎任务限流容量
    spec_rate_limit_capacity: int = 100
    # Spec 引擎任务限流填充速率（tokens/sec）
    spec_rate_limit_fill_rate: float = 50.0
    # Spec 引擎任务熔断阈值（连续失败次数）
    spec_circuit_breaker_threshold: int = 10
    # Spec 引擎任务熔断恢复超时（秒）
    spec_circuit_breaker_recovery: float = 5.0

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
    deep_stream_interval: float = 1.5
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

    # ------------------------------------------------------------------
    # RepoLockManager — 仓库操作锁
    # ------------------------------------------------------------------
    repo_lock_idle_timeout: int = 300  # 锁空闲超时（秒），超时自动释放（仅 refcount=0 时生效）
    repo_lock_cleanup_interval: int = 60  # 清理线程扫描间隔（秒）
    repo_lock_hard_timeout: int = 3600  # 锁绝对持有上限（秒），refcount>0 超此时长强制回收

    # ChatLockManager — 群锁 TTL
    chat_lock_max_duration: int = 86400  # 群锁最大持续时间（秒，默认 24h），超时自动释放
    chat_lock_cleanup_interval: int = 60  # 群锁清理线程扫描间隔（秒）

    # /lock 撤销窗口时长（秒），用户锁定后可在此窗口内撤销
    lock_undo_window_seconds: int = 300

    # /lock 确认卡片有效期（秒），超时后确认按钮失效
    lock_confirm_timeout: int = 120

    # SandboxExecutor 严格锁模式 — True 时检测到锁冲突 raise LockConflictError，False 仅 warning
    sandbox_strict_lock_mode: bool = False

    # ------------------------------------------------------------------
    # 签名回退兼容窗口 — 升级后旧按钮的 plain SHA-256 签名过渡期
    # ------------------------------------------------------------------
    sig_compat_deploy_date: str = ""  # ISO 格式部署日期，回退窗口起点；空值时以进程启动日期为起点
    sig_compat_window_days: int = 7  # 回退兼容天数，超过后仅接受 HMAC 签名

    # ------------------------------------------------------------------
    # 管理员用户列表（用于群级锁权限判定）
    # Stored as frozenset for O(1) membership checks on hot paths.
    # Declared as str to prevent pydantic-settings from attempting JSON parse
    # on plain comma-separated values; converted to frozenset in model_validator.
    # ------------------------------------------------------------------
    admin_user_ids: str = ""

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _normalize_admin_user_ids_input(cls, v):
        """Normalize list/set/frozenset input to comma-separated string."""
        if isinstance(v, (list, tuple, set, frozenset)):
            return ",".join(v)
        return v if v is not None else ""

    @model_validator(mode="after")
    def _coerce_admin_user_ids(self) -> "Settings":
        """Convert comma-separated admin_user_ids string to frozenset for O(1) lookup."""
        raw = self.admin_user_ids
        if not raw or not isinstance(raw, str):
            parsed = frozenset()
        else:
            parsed = frozenset(s.strip() for s in raw.split(",") if s.strip())
        object.__setattr__(self, "admin_user_ids", parsed)
        return self

    # ------------------------------------------------------------------
    # 项目 chat 隔离 — allowed_chat_ids 上限
    # ------------------------------------------------------------------
    max_allowed_chat_ids: int = 50  # 每个 project 最多关联的 chat_id 数量
    max_evicted_cache: int = 200  # evicted_chat_ids 有界 LRU 上限
    project_chat_suffix: str = "dev"  # 项目专属群名称后缀

    @field_validator("max_allowed_chat_ids", mode="before")
    @classmethod
    def _max_allowed_chat_ids_must_be_positive(cls, v: int, info) -> int:
        if int(v) < 1:
            raise ValueError(f"{info.field_name.upper()} 必须 ≥ 1（当前值: {v}）")
        return int(v)

    @field_validator("lock_confirm_timeout", "max_evicted_cache", mode="before")
    @classmethod
    def _lock_confirm_and_evicted_cache_must_be_positive(cls, v: int, info) -> int:
        if int(v) < 1:
            raise ValueError(f"{info.field_name.upper()} 必须 > 0（当前值: {v}）")
        return int(v)

    @field_validator("repo_lock_idle_timeout", "repo_lock_cleanup_interval", "repo_lock_hard_timeout", mode="before")
    @classmethod
    def _repo_lock_timers_must_be_positive(cls, v: int, info) -> int:
        if int(v) < 1:
            raise ValueError(f"{info.field_name.upper()} 必须 > 0（当前值: {v}）")
        return int(v)

    @field_validator("chat_lock_max_duration", "chat_lock_cleanup_interval", mode="before")
    @classmethod
    def _chat_lock_timers_must_be_positive(cls, v: int, info) -> int:
        if int(v) < 1:
            raise ValueError(f"{info.field_name.upper()} 必须 > 0（当前值: {v}）")
        return int(v)

    @field_validator("lock_undo_window_seconds", mode="before")
    @classmethod
    def _lock_undo_window_seconds_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 60 or val > 600:
            raise ValueError(
                f"LOCK_UNDO_WINDOW_SECONDS 必须在 [60, 600] 范围内（秒）（当前值: {v}）"
            )
        if val % 60 != 0:
            raise ValueError(
                f"LOCK_UNDO_WINDOW_SECONDS 必须为 60 的整数倍（当前值: {val}），"
                "可选值如 60, 120, 180, 240, 300, …"
            )
        return val

    @field_validator(
        "spec_review_timeout", "spec_review_min_timeout", "spec_review_hard_floor",
        mode="before",
    )
    @classmethod
    def _spec_review_timeout_fields_must_be_positive(cls, v: int, info) -> int:
        val = int(v)
        if val < 1:
            raise ValueError(f"{info.field_name} 必须 > 0，当前值为 {v}")
        return val

    @field_validator("spec_review_max_parallel", mode="before")
    @classmethod
    def _spec_review_max_parallel_must_be_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 1:
            raise ValueError(f"{info.field_name} 必须 ≥ 1，当前值为 {v}")
        if val > 20:
            raise ValueError(f"{info.field_name} 必须 ≤ 20，当前值为 {v}")
        return val

    @field_validator("spec_review_retry_max_delay", mode="before")
    @classmethod
    def _spec_retry_max_delay_must_be_positive(cls, v: int, info) -> int:
        if int(v) < 1:
            raise ValueError(f"{info.field_name} 必须 > 0，当前值为 {v}")
        return int(v)

    @field_validator("spec_review_retry_max_attempts", mode="before")
    @classmethod
    def _spec_retry_max_attempts_must_be_non_negative(cls, v: int, info) -> int:
        val = int(v)
        if val < 0:
            raise ValueError(
                f"{info.field_name} 必须 ≥ 0（设为 0 可禁用重试），当前值为 {v}"
            )
        # 上限 10：单次 retry 耗时 ≈ max_delay + adaptive_timeout * multiplier，
        # 10 次重试可能导致单 cycle 总耗时超过 cycle budget，不建议生产使用（推荐 1-3）。
        if val > 10:
            raise ValueError(
                f"{info.field_name} 必须 ≤ 10（推荐 1-3），当前值为 {v}"
            )
        return val

    @model_validator(mode="before")
    @classmethod
    def _hoist_card_fields(cls, data: dict) -> dict:
        """Collect flat card_* env keys into nested 'card' sub-dict for CardSessionConfig."""
        if not isinstance(data, dict):
            return data
        # If 'card' is already a dict/model, skip hoisting (e.g. programmatic construction)
        if "card" in data and isinstance(data["card"], (dict, CardSessionConfig)):
            return data
        # Map from flat Settings field name (card_xxx) to CardSessionConfig field name (xxx)
        _CARD_FIELD_MAP = {
            "card_collapsible_enabled": "collapsible_enabled",
            "card_continuation_enabled": "continuation_enabled",
            "card_button_layout": "button_layout",
            "card_button_size": "button_size",
            "card_mobile_force_vertical": "mobile_force_vertical",
            "card_mobile_layout_mode": "mobile_layout_mode",
            "card_deep_compact_default": "deep_compact_default",
            "card_max_chars": "max_chars",
            "card_session_lock_max": "session_lock_max",
            "card_session_lock_ttl": "session_lock_ttl",
            "card_session_idle_timeout": "session_idle_timeout",
            "card_session_idle_warn_before": "session_idle_warn_at_remaining",
            "card_session_idle_warn_at_remaining": "session_idle_warn_at_remaining",
            "card_session_max_rotations": "session_max_rotations",
            "card_delivery_pool_max_workers": "delivery_pool_max_workers",
            "card_action_dedup_ttl": "action_dedup_ttl",
            "card_action_dedup_max_size": "action_dedup_max_size",
            "card_action_dedup_cleanup_interval": "action_dedup_cleanup_interval",
            "card_ticker_interval": "ticker_interval",
            "card_task_level_cards_enabled": "task_level_cards_enabled",
            "card_max_task_cards": "max_task_cards",
        }
        card_data: dict = {}
        for flat_key, nested_key in _CARD_FIELD_MAP.items():
            if flat_key in data:
                card_data[nested_key] = data.pop(flat_key)
        if card_data:
            data["card"] = card_data
        return data

    @model_validator(mode="after")
    def _validate_spec_review_cross_fields(self) -> "Settings":
        """Cross-field validation for spec review timing parameters."""
        # 排序约束: hard_floor <= min_timeout <= timeout
        if self.spec_review_hard_floor > self.spec_review_min_timeout:
            raise ValueError(
                f"spec_review_hard_floor 必须 ≤ spec_review_min_timeout，"
                f"当前分别为 {self.spec_review_hard_floor} 和 {self.spec_review_min_timeout}"
            )
        if self.spec_review_min_timeout > self.spec_review_timeout:
            raise ValueError(
                f"spec_review_min_timeout 必须 ≤ spec_review_timeout，"
                f"当前分别为 {self.spec_review_min_timeout} 和 {self.spec_review_timeout}"
            )
        # 重试最大延迟不能超过审查超时
        if self.spec_review_retry_max_delay > self.spec_review_timeout:
            raise ValueError(
                f"spec_review_retry_max_delay 必须 ≤ spec_review_timeout，"
                f"当前分别为 {self.spec_review_retry_max_delay} 和 {self.spec_review_timeout}"
            )
        # 下界估算：实际每次 retry 耗时由 compute_adaptive_timeout 动态决定，
        # 可能大于 min_timeout；此处使用 min_timeout 作为保守下界验证总预算合理性。
        total_retry_budget = (
            self.spec_review_retry_max_delay + self.spec_review_min_timeout
        ) * self.spec_review_retry_max_attempts
        budget_limit = self.spec_review_timeout * 2
        if total_retry_budget > budget_limit:
            raise ValueError(
                "请减小 SPEC_REVIEW_RETRY_MAX_ATTEMPTS 或 SPEC_REVIEW_RETRY_MAX_DELAY"
                "（当前组合超出允许范围）"
            )
        # NOTE: realistic budget check moved to _post_validate_warnings()
        return self

    @model_validator(mode="after")
    def _validate_lock_timing_cross_fields(self) -> "Settings":
        """Cross-field: lock_undo_window_seconds should be >= lock_confirm_timeout."""
        if self.lock_undo_window_seconds < self.lock_confirm_timeout:
            _logging.getLogger(__name__).warning(
                "lock_undo_window_seconds (%d) < lock_confirm_timeout (%d): "
                "confirmation timeout exceeds undo window, which may confuse users. "
                "Consider increasing lock_undo_window_seconds or decreasing lock_confirm_timeout.",
                self.lock_undo_window_seconds, self.lock_confirm_timeout,
            )
        return self

    @property
    def command_blacklist(self) -> list[str]:
        return [cmd.strip() for cmd in self.sandbox_command_blacklist.split(",") if cmd.strip()]
    
    @property
    def command_whitelist(self) -> list[str]:
        return [cmd.strip() for cmd in self.sandbox_command_whitelist.split(",") if cmd.strip()]

    def validate_feishu_config(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def get_acp_command(self, agent_type: str) -> tuple[str, list[str]]:
        """Return (cmd, args) override for an ACP agent, if configured."""
        agent_type = (agent_type or "").lower()
        if agent_type == "coco" and self.coco_acp_cmd:
            return self.coco_acp_cmd, shlex.split(self.coco_acp_args or "")
        if agent_type == "claude" and self.claude_acp_cmd:
            return self.claude_acp_cmd, shlex.split(self.claude_acp_args or "")
        return "", []
