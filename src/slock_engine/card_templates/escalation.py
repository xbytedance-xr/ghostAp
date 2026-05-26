"""Escalation card templates for Slock Engine (mobile-optimized).

Provides modernized versions of escalation cards using build_card_wrapper
with mobile_optimize=True for optimal mobile display.

Cards:
- build_escalation_card: Escalation alert requesting admin intervention
- build_resolved_escalation_card: Resolved escalation status card
"""

from __future__ import annotations

from typing import Optional

from ..models import (
    ABORT_OPTIONS,
    EscalationLevel,
    EscalationRequest,
)
from .common import (
    DISPLAY_TZ,
    apply_compact_style,
    build_card_wrapper,
    build_responsive_layout,
    redact_sensitive,
)

__all__ = [
    "build_escalation_card",
    "build_resolved_escalation_card",
]


def _wrap_text(text: str, width: int = 80) -> str:
    """Wrap lines longer than *width* characters by inserting newlines."""
    lines = text.split("\n")
    wrapped: list[str] = []
    for line in lines:
        while len(line) > width:
            wrapped.append(line[:width])
            line = line[width:]
        wrapped.append(line)
    return "\n".join(wrapped)


def build_escalation_card(
    escalation: EscalationRequest,
    *,
    channel_id: str = "",
    timeout_minutes: Optional[int] = None,
) -> dict:
    """Build an escalation card requesting admin intervention.

    Shows the agent name, severity level, reason, and resolution option buttons.
    """
    level_icons = {
        EscalationLevel.WARNING: "\u26a0\ufe0f",
        EscalationLevel.BLOCKED: "\U0001f6ab",
        EscalationLevel.CRITICAL: "\U0001f534",
    }
    level_colors = {
        EscalationLevel.WARNING: "yellow",
        EscalationLevel.BLOCKED: "orange",
        EscalationLevel.CRITICAL: "red",
    }

    icon = level_icons.get(escalation.level, "\u26a0\ufe0f")
    header_color = level_colors.get(escalation.level, "orange")
    header_title = f"{icon} \u5347\u7ea7\u544a\u8b66: {escalation.agent_name or 'Agent'}"

    elements: list[dict] = []

    # Severity and reason (redact sensitive info before rendering)
    safe_reason = redact_sensitive(escalation.reason)
    elements.append({
        "tag": "markdown",
        "content": (
            f"**\u7ea7\u522b:** {escalation.level.value.upper()}\n"
            f"**\u4ee3\u7406:** {escalation.agent_name} (`{escalation.agent_id}`)\n"
            f"**\u539f\u56e0:** {safe_reason}"
        ),
    })

    # Context details (truncated + redacted)
    if escalation.context:
        context_display = redact_sensitive(escalation.context[:500])
        if len(escalation.context) > 500:
            context_display += "\n..."
        context_display = _wrap_text(context_display)
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**\u4e0a\u4e0b\u6587:**\n{context_display}",
        })

    # Task reference
    if escalation.task_id:
        elements.append({
            "tag": "markdown",
            "content": f"**\u4efb\u52a1:** `{escalation.task_id}`",
        })

    # Timeout hint
    if timeout_minutes is not None:
        elements.append({
            "tag": "markdown",
            "content": f"\u23f0 \u6b64\u5347\u7ea7\u5c06\u5728 {timeout_minutes} \u5206\u949f\u540e\u81ea\u52a8\u4e2d\u6b62",
            "text_size": "notation",
        })

    elements.append({"tag": "hr"})

    # Resolution option buttons
    option_buttons: list[dict] = []
    default_options = escalation.options or ["\u91cd\u8bd5", "\u8df3\u8fc7", "\u4e2d\u6b62"]
    for option in default_options:
        value = {
            "action": "slock_escalation_resolve",
            "escalation_id": escalation.escalation_id,
            "resolution": option,
            "channel_id": channel_id,
        }
        btn_type = "danger" if option in ABORT_OPTIONS else "default"
        option_buttons.append(apply_compact_style({
            "tag": "button",
            "text": {"tag": "plain_text", "content": option},
            "type": btn_type,
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }))

    if option_buttons:
        elements.extend(build_responsive_layout(option_buttons))

    return build_card_wrapper(
        header_title=header_title,
        header_template=header_color,
        elements=elements,
        mobile_optimize=True,
    )


def build_resolved_escalation_card(
    escalation: EscalationRequest,
    *,
    resolved_by: str = "",
    resolution: str = "",
    resolved_at: Optional[float] = None,
    channel_id: str = "",
) -> dict:
    """Build an escalation card in resolved state -- buttons removed, status shown.

    Args:
        escalation: The original EscalationRequest.
        resolved_by: Display name or ID of the operator who resolved it.
        resolution: The chosen resolution (e.g. Retry/Skip/Abort).
        resolved_at: Timestamp of resolution (epoch seconds).
        channel_id: Optional channel identifier.
    """
    import time as _time
    from datetime import datetime

    level_icons = {
        EscalationLevel.WARNING: "\u26a0\ufe0f",
        EscalationLevel.BLOCKED: "\U0001f6ab",
        EscalationLevel.CRITICAL: "\U0001f534",
    }

    icon = level_icons.get(escalation.level, "\u26a0\ufe0f")
    header_title = f"{icon} \u5347\u7ea7\u544a\u8b66: {escalation.agent_name or 'Agent'} [\u5df2\u89e3\u51b3]"

    elements: list[dict] = []

    # Original severity and reason
    elements.append({
        "tag": "markdown",
        "content": (
            f"**\u7ea7\u522b:** {escalation.level.value.upper()}\n"
            f"**\u4ee3\u7406:** {escalation.agent_name} (`{escalation.agent_id}`)\n"
            f"**\u539f\u56e0:** {redact_sensitive(escalation.reason)}"
        ),
    })

    # Context details (truncated) -- keep for reference
    if escalation.context:
        context_display = redact_sensitive(escalation.context[:500])
        if len(escalation.context) > 500:
            context_display += "\n..."
        quote_context = "\n".join(f"> {line}" for line in context_display.splitlines())
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**\u4e0a\u4e0b\u6587:**\n{quote_context}",
        })

    # Task reference
    if escalation.task_id:
        elements.append({
            "tag": "markdown",
            "content": f"**\u4efb\u52a1:** `{escalation.task_id}`",
        })

    elements.append({"tag": "hr"})

    # Resolution status (replaces buttons)
    ts = resolved_at or _time.time()
    time_str = datetime.fromtimestamp(ts, tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    operator_display = resolved_by or "\u672a\u77e5"

    elements.append({
        "tag": "markdown",
        "content": f"\u2705 **\u5df2\u89e3\u51b3:** {resolution}\uff0c\u7531 {operator_display} \u5904\u7406\n\U0001f4c5 {time_str}",
    })

    return build_card_wrapper(
        header_title=header_title,
        header_template="green",
        elements=elements,
        mobile_optimize=True,
    )
