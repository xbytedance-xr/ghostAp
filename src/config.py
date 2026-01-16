import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_id: str = ""
    app_secret: str = ""
    verification_token: Optional[str] = None
    encrypt_key: Optional[str] = None

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:latest"

    sandbox_timeout: int = 30
    sandbox_max_output_length: int = 4000
    sandbox_command_blacklist: str = "rm -rf /,rm -rf /*,mkfs,dd if=,shutdown,reboot,halt,poweroff,init 0,init 6,:(){ :|:& };:"

    coco_execution_timeout: int = 7200
    coco_session_timeout: int = 86400

    server_host: str = "0.0.0.0"
    server_port: int = 8000

    @property
    def command_blacklist(self) -> list[str]:
        return [cmd.strip() for cmd in self.sandbox_command_blacklist.split(",") if cmd.strip()]

    def validate_feishu_config(self) -> bool:
        if not self.app_id or not self.app_secret:
            return False
        return True


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
