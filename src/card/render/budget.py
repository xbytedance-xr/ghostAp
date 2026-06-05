"""Render budget constraints for card output."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderBudget:
    """Budget limits for card rendering."""

    # Class-level constants for pre-check scenarios
    NODE_BUDGET: int = 180
    VISIBLE_CHARS_LIMIT: int = 25000

    byte_budget: int = 27 * 1024
    node_budget: int = 180
    visible_chars: int = 25000
    reasoning_tail_chars: int = 500
    engine_cmd: str = "对应命令"
    # Button rendering parameters (injected from settings at budget creation)
    button_size: str = "medium"
    mobile_force_vertical: bool = True
    button_horizontal_spacing: str | None = None
    button_vertical_spacing: str | None = None
    # Platform-aware rendering
    mobile: bool = True
    # Plan panel truncation
    plan_max_chars: int = 2000
