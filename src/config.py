from typing import Optional
import shlex
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    sandbox_command_blacklist: str = "rm -rf /,rm -rf /*,mkfs,dd if=,shutdown,reboot,halt,poweroff,init 0,init 6,:(){ :|:& };:"

    coco_execution_timeout: int = 7200
    coco_session_timeout: int = 86400
    coco_max_output_length: int = 30000

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

    # ACP stdio stream buffer limit (bytes). Default asyncio limit is 64KB which
    # is too small for large agent responses (code generation, file contents).
    # Set to 0 to use the asyncio default (64KB). 10MB should be generous enough.
    acp_stream_buffer_limit: int = 10 * 1024 * 1024

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

    # Loop Engine settings
    loop_max_iterations: int = 100
    loop_execution_timeout: int = 7200
    loop_convergence_window: int = 3
    loop_max_context_tokens: int = 8000
    loop_default_max_retries: int = 2

    # Loop Engine multi-perspective review (Ralph Loop)
    loop_review_enabled: bool = True
    loop_review_extra_iterations: int = 3

    streaming_enabled: bool = True

    # Task scheduler (thread-based) settings
    task_scheduler_max_concurrent: int = 10
    task_scheduler_per_key_concurrency: int = 1

    # 卡片按钮布局策略：
    # - desktop: 使用飞书 action 原生布局（更贴近桌面端观感）
    # - mobile: 强制两列 column_set（手机端更稳定，一行两个按钮）
    # - responsive: 默认值；<=2 个按钮用 action，>2 个按钮用两列 column_set
    card_button_layout: str = "responsive"

    # 消息回复模式配置
    # - direct: 直接回复（消息显示在被回复消息下方）
    # - thread: 话题回复（使用 reply_in_thread=True，消息会显示在独立话题区域，更整洁）
    #
    # smart_reply_mode: 智能模式下的回复方式（默认 direct，群内直接引用消息回复）
    # default_reply_mode: 其他模式（Coco/Claude/Shell/Deep等）的回复方式（默认 thread，话题回复更整洁）
    smart_reply_mode: str = "direct"
    default_reply_mode: str = "thread"

    @property
    def command_blacklist(self) -> list[str]:
        return [
            cmd.strip()
            for cmd in self.sandbox_command_blacklist.split(",")
            if cmd.strip()
        ]

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


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
