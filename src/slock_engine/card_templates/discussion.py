"""Discussion card templates for Slock Engine (mobile-optimized).

Provides modernized versions of discussion cards using build_card_wrapper
and build_mobile_card_row for optimal mobile display.

Cards:
- build_discussion_live_card: Live discussion thread with messages
- build_discussion_conclusion_card: Final conclusion/summary
- build_discussion_history_list_card: History list for browsing past discussions
"""

from __future__ import annotations

from .common import (
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_mobile_card_row,
    build_responsive_layout,
    redact_sensitive,
)


def build_discussion_live_card(
    *,
    thread_id: str,
    participants: list[str],
    messages: list[dict],
    current_round: int,
    max_rounds: int,
    trigger_reason: str = "",
    channel_id: str = "",
) -> dict:
    """Build a mobile-friendly live discussion card.

    Args:
        thread_id: Unique discussion thread identifier.
        participants: List of agent display names in the discussion.
        messages: List of dicts with keys: sender, content, round_num.
        current_round: Current round number.
        max_rounds: Maximum allowed rounds.
        trigger_reason: Why the discussion was triggered.
        channel_id: Channel ID for action buttons.
    """
    header_title = f"💬 讨论 R{current_round}/{max_rounds}"

    elements: list[dict] = []

    # Participants
    if len(participants) > 3:
        parts_text = f"👥 {' · '.join(participants[:3])} 等 {len(participants)} 人"
    else:
        parts_text = f"👥 {' · '.join(participants)}"
    elements.append({"tag": "markdown", "content": parts_text})

    # Progress indicator (compact for mobile)
    pct = round(current_round / max_rounds * 100) if max_rounds > 0 else 0
    elements.append({
        "tag": "progress",
        "percent": pct,
        "status": "green" if pct == 100 else "blue",
        "show_text": True,
    })

    # Trigger reason tag
    if trigger_reason:
        if "manual" in trigger_reason:
            tag_color, tag_label = "blue", "人工触发"
        elif "uncertainty" in trigger_reason:
            tag_color, tag_label = "orange", "系统检测"
        elif "chain" in trigger_reason:
            tag_color, tag_label = "green", "链式触发"
        else:
            tag_color, tag_label = "neutral", trigger_reason
        elements.append({
            "tag": "markdown",
            "content": f"<font color='{tag_color}'>{tag_label}</font>",
            "text_size": "notation",
        })

    elements.append({"tag": "hr"})

    # Messages (last 6 for mobile compactness)
    display_messages = messages[-6:] if len(messages) > 6 else messages
    if len(messages) > 6:
        elements.append({
            "tag": "markdown",
            "content": f"*... 省略 {len(messages) - 6} 条早期消息*",
            "text_size": "notation",
        })

    _MSG_THRESHOLD = 120

    for msg in display_messages:
        sender = msg.get("sender", "Agent")
        raw_content = redact_sensitive(msg.get("content", ""))
        round_num = msg.get("round_num", "?")

        if len(raw_content) > _MSG_THRESHOLD:
            # Long message: collapsible
            preview = raw_content[:_MSG_THRESHOLD] + "…"
            full_el = {"tag": "markdown", "content": f"💬 **{sender}** (R{round_num}):\n{raw_content}"}
            elements.append(
                build_collapsible_panel(
                    f"💬 {sender} (R{round_num}): {preview}",
                    [full_el],
                    expanded=False,
                )
            )
        else:
            elements.append({
                "tag": "note",
                "icon": {"tag": "standard_icon", "token": "chat_outlined"},
                "elements": [
                    {"tag": "markdown", "content": f"**{sender}** (R{round_num}): {raw_content}"},
                ],
            })

    # Action buttons
    elements.append({"tag": "hr"})
    buttons = [
        build_callback_button(
            "📖 展开全部",
            "slock_discussion_expand",
            channel_id=channel_id,
            extra_value={"thread_id": thread_id},
        ),
        build_callback_button(
            "💡 人工干预",
            "inject_discussion_hint",
            channel_id=channel_id,
            button_type="primary",
            extra_value={"thread_id": thread_id},
        ),
        build_callback_button(
            "⏹ 停止",
            "slock_discussion_stop",
            channel_id=channel_id,
            button_type="danger",
            extra_value={"thread_id": thread_id},
        ),
    ]
    elements.extend(build_responsive_layout(buttons))

    return build_card_wrapper(
        header_title=header_title,
        header_template="purple",
        elements=elements,
    )


def build_discussion_conclusion_card(
    *,
    thread_id: str,
    participants: list[str],
    conclusion: str,
    total_rounds: int,
    total_tokens: int = 0,
    status: str = "converged",
    channel_id: str = "",
) -> dict:
    """Build a mobile-friendly discussion conclusion card.

    Args:
        thread_id: Unique discussion thread identifier.
        participants: List of agent display names involved.
        conclusion: The final summarized conclusion.
        total_rounds: Total number of rounds completed.
        total_tokens: Total tokens consumed by the discussion.
        status: Final status (converged, timeout, budget_exhausted, manually_stopped).
        channel_id: Channel ID for action buttons.
    """
    status_labels = {
        "converged": ("✅ 达成共识", "green"),
        "timeout": ("⏰ 超时结束", "grey"),
        "max_rounds_reached": ("🔄 已达最大轮次", "grey"),
        "budget_exhausted": ("💰 预算耗尽", "orange"),
        "manually_stopped": ("⏹ 手动终止", "grey"),
    }
    status_display, header_color = status_labels.get(status, (status, "grey"))
    header_title = f"💬 讨论结论 — {status_display}"

    elements: list[dict] = []

    # Stats row
    stats_parts = [f"**参与者:** {' ↔ '.join(participants)}", f"**总轮次:** {total_rounds}"]
    if total_tokens:
        stats_parts.append(f"**Token:** {total_tokens:,}")
    elements.append({"tag": "markdown", "content": "\n".join(stats_parts)})

    elements.append({"tag": "hr"})

    # Conclusion (redacted, truncated)
    safe_conclusion = redact_sensitive(conclusion)
    if len(safe_conclusion) > 300:
        elements.append(
            build_collapsible_panel(
                "**📝 结论**",
                [{"tag": "markdown", "content": safe_conclusion[:500]}],
                expanded=True,
            )
        )
    else:
        elements.append({"tag": "markdown", "content": f"**📝 结论:**\n{safe_conclusion}"})

    # Thread ID footer
    elements.append({
        "tag": "markdown",
        "content": f"`thread: {thread_id[:12]}…`",
        "text_size": "notation",
    })

    return build_card_wrapper(
        header_title=header_title,
        header_template=header_color,
        elements=elements,
    )


def build_discussion_history_list_card(
    *,
    history: list[dict],
    channel_id: str = "",
) -> dict:
    """Build a mobile-friendly discussion history list card.

    Args:
        history: List of dicts with keys: topic_hash, title, participants, time, conclusion.
        channel_id: Channel ID for context.
    """
    elements: list[dict] = []

    if not history:
        elements.append({"tag": "markdown", "content": "📭 暂无讨论历史。"})
    else:
        elements.append({
            "tag": "markdown",
            "content": f"共 **{len(history)}** 条讨论记录",
            "text_size": "notation",
        })
        elements.append({"tag": "hr"})

        for entry in history[:10]:
            title = entry.get("title", "Untitled")
            topic_hash = entry.get("topic_hash", "")[:8]
            participants = entry.get("participants", "")
            time_str = entry.get("time", "")
            conclusion = entry.get("conclusion", "")

            # Title row
            title_els = [{"tag": "markdown", "content": f"**{title}** `{topic_hash}`"}]

            # Content row
            content_parts: list[str] = []
            if participants:
                content_parts.append(f"👥 {participants}")
            if time_str:
                content_parts.append(f"🕐 {time_str}")
            if conclusion:
                safe = redact_sensitive(conclusion)
                content_parts.append(safe[:80] + ("…" if len(safe) > 80 else ""))
            content_els = [{"tag": "markdown", "content": "\n".join(content_parts)}] if content_parts else None

            elements.append(build_mobile_card_row(
                title_elements=title_els,
                content_elements=content_els,
            ))

        if len(history) > 10:
            elements.append({
                "tag": "markdown",
                "content": f"*... 还有 {len(history) - 10} 条*",
                "text_size": "notation",
            })

    return build_card_wrapper(
        header_title="📋 讨论历史",
        header_template="purple",
        elements=elements,
    )
