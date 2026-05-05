"""Unified progress bar rendering utility."""

from __future__ import annotations

_BAR_LENGTH = 10
_MOBILE_BAR_LENGTH = 6
_FILLED = "▰"
_EMPTY = "▱"

# Default progress bar color (Feishu markdown font color)
_DEFAULT_COLOR = "blue"


def render_progress_bar(
    pct: int | float,
    *,
    total_segments: int = _BAR_LENGTH,
    mobile_segments: int | None = None,
    color: str = _DEFAULT_COLOR,
    is_started: bool = False,
) -> str:
    """Render a text-based progress bar with percentage number.

    Args:
        pct: Percentage value (0-100). Clamped and rounded to integer.
        total_segments: Number of bar segments (default 10 for fine-grained progress).
        mobile_segments: If set, use this segment count instead (for narrower mobile screens).
            Not used directly here — callers can pass mobile_segments=_MOBILE_BAR_LENGTH
            to get a compact bar suitable for mobile rendering.
        color: Feishu font color for filled segments (default 'blue').
        is_started: When True and pct==0, show a wathet-colored first segment
            to indicate the task is running but has no progress yet.

    Returns:
        A string like "<font color='blue'>▰▰▰</font>▱▱▱▱▱▱▱ 30%"
        or empty string if total_segments <= 0.
    """
    segments = mobile_segments if mobile_segments is not None else total_segments
    if segments <= 0:
        return ""
    pct_int = max(0, min(100, round(pct)))
    filled = min(round(pct_int / (100 / segments)), segments)
    # Ensure at least 1 filled segment when pct > 0
    if pct_int > 0 and filled == 0:
        filled = 1
    # When task is started but pct==0, show wathet-colored first segment
    if pct_int == 0 and is_started and segments > 0:
        started_str = f"<font color='wathet'>{_FILLED}</font>"
        empty_str = _EMPTY * (segments - 1)
        return f"{started_str}{empty_str} {pct_int}%"
    filled_str = _FILLED * filled
    empty_str = _EMPTY * (segments - filled)
    if filled > 0 and color:
        bar = f"<font color='{color}'>{filled_str}</font>{empty_str}"
    else:
        bar = filled_str + empty_str
    return f"{bar} {pct_int}%"


# Convenience constant for callers needing mobile-specific segment count
MOBILE_SEGMENTS = _MOBILE_BAR_LENGTH
