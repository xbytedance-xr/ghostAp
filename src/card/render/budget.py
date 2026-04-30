"""Render budget constraints for card output."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderBudget:
    """Budget limits for card rendering."""
    byte_budget: int = 27 * 1024
    node_budget: int = 180
    visible_chars: int = 25000
    tool_history_fold_threshold: int = 3
    reasoning_tail_chars: int = 500
