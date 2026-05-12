"""SpecReviewConfig — read-only view grouping spec review / retry / circuit-breaker settings."""

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class SpecReviewConfig:
    """Read-only view grouping spec review / retry / circuit-breaker settings.

    Accessed via ``get_settings().spec_review``.  Individual fields remain on
    Settings for backward compatibility; this provides a structured namespace.
    """
    enabled: bool
    timeout: int
    max_parallel: int
    min_timeout: int
    hard_floor: int
    retry_max_delay: int
    retry_max_attempts: int
    retry_base_delay: float
    retry_decay_factor: float
    failure_circuit_enabled: bool
    failure_max_consecutive: int
    failure_cooldown_cycles: int
    failure_max_cooldown_cycles: int
    parse_failure_default: str
    strategy: str
    dynamic_roles_enabled: bool
    dynamic_roles_max: int
    total_roles_max: int
    pass_streak_required: int
