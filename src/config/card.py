"""CardSessionConfig — nested configuration for card session / delivery / UI parameters."""

import logging as _logging
import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CardSessionConfig(BaseModel):
    """Nested configuration for card session / delivery / UI parameters.

    All fields map 1:1 to CARD_* environment variables. The parent Settings
    class uses a model_validator to hoist flat card_* fields into this nested
    model while keeping .env backward compatibility.
    """

    collapsible_enabled: bool = True
    continuation_enabled: bool = True
    button_layout: Literal["desktop", "mobile", "responsive"] = "responsive"
    button_size: Literal["small", "medium", "large"] = "medium"
    mobile_force_vertical: bool = True
    deep_compact_default: bool = False
    max_chars: int = 28000
    session_lock_max: int = 10_000
    session_lock_ttl: float = 600.0
    session_idle_timeout: int = 1800
    session_idle_warn_at_remaining: int = 300
    session_max_rotations: int = 20
    action_dedup_ttl: int = 1
    action_dedup_max_size: int = 5000
    action_dedup_cleanup_interval: int = 30
    delivery_pool_max_workers: int = 16
    delivery_api_timeout: float = Field(
        default=35.0,
        description="Feishu card API hard timeout in seconds; should exceed the lark SDK timeout slightly.",
    )
    ticker_interval: float = Field(
        default=1.2,
        gt=0,
        description="Live ticker 帧切换间隔（秒），对应 v2 设计中绿点动画节奏",
    )
    task_level_cards_enabled: bool = Field(
        default=True,
        description="启用后多步任务使用独立飞书卡片展示每个子任务，关闭则退化为单卡模式",
    )
    max_task_cards: int = Field(
        default=8,
        description="单次执行中任务级卡片数量上限，超出后合并到最后一张卡片",
    )

    @field_validator("max_task_cards", mode="before")
    @classmethod
    def _max_task_cards_in_range(cls, v: int, info) -> int:
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(
                f"card_max_task_cards 必须为有效整数（当前值: {v!r}）。"
                f"请检查环境变量 CARD_MAX_TASK_CARDS 的拼写"
            )
        if val < 1 or val > 20:
            raise ValueError(
                f"card_max_task_cards 必须在 1~20 范围内（当前值: {v}）"
            )
        return val

    @field_validator("session_lock_max", mode="before")
    @classmethod
    def _session_lock_max_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 1000 or val > 100_000:
            raise ValueError(
                f"card_session_lock_max 必须在 [1000, 100000] 范围内（当前值: {v}）"
            )
        return val

    @field_validator("max_chars", mode="before")
    @classmethod
    def _max_chars_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 1000 or val > 50000:
            raise ValueError(
                f"card_max_chars 必须在 [1000, 50000] 范围内（当前值: {v}）"
            )
        return val

    @field_validator("session_lock_ttl", mode="before")
    @classmethod
    def _session_lock_ttl_in_range(cls, v: float, info) -> float:
        val = float(v)
        if val < 60 or val > 3600:
            raise ValueError(
                f"card_session_lock_ttl 必须在 [60, 3600] 范围内（秒）（当前值: {v}）"
            )
        # Auto-ceil to nearest multiple of 60
        if val % 60 != 0:
            new_val = math.ceil(val / 60) * 60
            _logging.getLogger(__name__).info(
                "CARD_SESSION_LOCK_TTL rounded up to %ds (from %s)", new_val, v
            )
            val = float(new_val)
        return val

    @field_validator("delivery_api_timeout", mode="before")
    @classmethod
    def _delivery_api_timeout_in_range(cls, v: float, info) -> float:
        val = float(v)
        if val < 1 or val > 300:
            raise ValueError(
                f"card_delivery_api_timeout 必须在 [1, 300] 范围内（秒）（当前值: {v}）"
            )
        return val

    @field_validator("session_idle_timeout", mode="before")
    @classmethod
    def _session_idle_timeout_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 300 or val > 7200:
            raise ValueError(
                f"card_session_idle_timeout 必须在 [300, 7200] 范围内（秒）（当前值: {v}）"
            )
        return val

    @field_validator("session_idle_warn_at_remaining", mode="before")
    @classmethod
    def _session_idle_warn_at_remaining_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 60 or val > 3600:
            raise ValueError(
                f"card_session_idle_warn_at_remaining 必须在 [60, 3600] 范围内（秒）（当前值: {v}）"
            )
        return val

    @field_validator("session_max_rotations", mode="before")
    @classmethod
    def _session_max_rotations_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 1 or val > 100:
            raise ValueError(
                f"card_session_max_rotations 必须在 [1, 100] 范围内（当前值: {v}）"
            )
        return val

    @field_validator("delivery_pool_max_workers", mode="before")
    @classmethod
    def _delivery_pool_max_workers_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 1 or val > 32:
            raise ValueError(
                f"card_delivery_pool_max_workers 必须在 [1, 32] 范围内（当前值: {v}）"
            )
        return val

    @field_validator("action_dedup_ttl", mode="before")
    @classmethod
    def _action_dedup_ttl_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 0 or val > 10:
            raise ValueError(
                f"card_action_dedup_ttl 必须在 [0, 10] 范围内（当前值: {v}）"
            )
        return val

    @field_validator("action_dedup_max_size", mode="before")
    @classmethod
    def _action_dedup_max_size_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 100 or val > 50_000:
            raise ValueError(
                f"card_action_dedup_max_size 必须在 [100, 50000] 范围内（当前值: {v}）"
            )
        return val

    @field_validator("action_dedup_cleanup_interval", mode="before")
    @classmethod
    def _action_dedup_cleanup_interval_in_range(cls, v: int, info) -> int:
        val = int(v)
        if val < 1 or val > 3600:
            raise ValueError(
                f"card_action_dedup_cleanup_interval 必须在 [1, 3600] 范围内（秒）（当前值: {v}）"
            )
        return val

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "CardSessionConfig":
        """Cross-field: session_lock_ttl must not exceed session_idle_timeout.
        session_idle_warn_at_remaining must be less than session_idle_timeout."""
        if self.session_lock_ttl > self.session_idle_timeout:
            raise ValueError(
                f"card_session_lock_ttl 必须 ≤ card_session_idle_timeout（秒），"
                f"当前分别为 {self.session_lock_ttl} 和 {self.session_idle_timeout}"
            )
        if self.session_idle_warn_at_remaining >= self.session_idle_timeout:
            raise ValueError(
                f"card_session_idle_warn_at_remaining 必须 < card_session_idle_timeout（秒），"
                f"当前分别为 {self.session_idle_warn_at_remaining} 和 {self.session_idle_timeout}"
            )
        return self
