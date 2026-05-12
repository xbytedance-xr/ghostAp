"""TTL activity classification for CardSession.

Idle timeout should protect abandoned cards, not interrupt active long-running
engine work. This module keeps that distinction out of the timer handler.
"""

from __future__ import annotations

from src.card.state.models import CardState

_ACTIVE_FOOTER_STATUSES = frozenset({"thinking", "tool_running"})


def has_active_card_work(state: CardState | None) -> bool:
    """Return True when a running card still represents active engine work."""
    if state is None or state.terminal != "running":
        return False

    if state.footer.progress_pct is not None and state.footer.progress_pct < 100:
        return True

    for block in state.blocks:
        if getattr(block, "kind", "") == "tool_call" and getattr(block, "status", "") == "active":
            return True
        if (
            state.footer.status in _ACTIVE_FOOTER_STATUSES
            and getattr(block, "kind", "") in {"reasoning", "plan"}
            and getattr(block, "status", "") == "active"
        ):
            return True

    return False
