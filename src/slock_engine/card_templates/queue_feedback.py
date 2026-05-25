"""Queue / activation / timeout visual feedback cards for Slock engine."""

from __future__ import annotations

from .common import (
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_responsive_layout,
    redact_sensitive,
)


def _sanitize_preview(text: str, max_len: int = 80) -> str:
    """Sanitize text for card preview: redact sensitive info first, then truncate.

    Args:
        text: The original text to sanitize.
        max_len: Maximum length after truncation.

    Returns:
        Sanitized and truncated text.
    """
    if not text:
        return ""
    sanitized = redact_sensitive(text)
    if len(sanitized) > max_len:
        return sanitized[:max_len] + "..."
    return sanitized


def build_queue_wait_card(
    *,
    position: int,
    busy_count: int,
    message_preview: str = "",
) -> dict:
    """Card shown when a task enters the queue because all agents are busy.

    Args:
        position: 1-based queue position.
        busy_count: Number of currently busy agents.
        message_preview: Truncated task text for user context.
    """
    preview = _sanitize_preview(message_preview)
    elements = [
        {
            "tag": "markdown",
            "content": (
                f"⏳ 所有 **{busy_count}** 个 Agent 正在忙碌，"
                f"你的任务排在第 **{position}** 位\n\n"
                f"> {preview}"
            ),
        },
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "Agent 空闲后将自动分配，无需重复发送"},
            ],
        },
    ]
    return build_card_wrapper(
        header_title="🚦 排队等待中",
        header_template="orange",
        elements=elements,
    )


def build_activation_confirm_card(
    *,
    team_name: str = "Team",
    agent_count: int = 0,
    first_task_preview: str = "",
) -> dict:
    """Card shown after passive auto-activation confirms slock is ready.

    Args:
        team_name: The team/channel name.
        agent_count: Number of agents bootstrapped.
        first_task_preview: Preview of the first queued task.
    """
    preview = _sanitize_preview(first_task_preview)
    body_parts = [
        f"**{team_name}** 协作模式已自动激活",
    ]
    if agent_count:
        body_parts.append(f"已创建 **{agent_count}** 个预置角色")
    if preview:
        body_parts.append(f"首条任务已入队: > {preview}")

    elements = [
        {"tag": "markdown", "content": "\n".join(body_parts)},
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "直接在群里发任务即可，Agent 自动处理。无需任何前置命令。"},
            ],
        },
    ]
    return build_card_wrapper(
        header_title="🎭 Slock 已激活",
        header_template="green",
        elements=elements,
    )


def build_timeout_notify_card(
    *,
    task_id: str,
    message_preview: str = "",
    waited_seconds: float = 60.0,
) -> dict:
    """Card shown when a queued task times out waiting for an agent.

    Args:
        task_id: The task identifier.
        message_preview: Truncated task text.
        waited_seconds: How long the task waited before timing out.
    """
    preview = _sanitize_preview(message_preview)
    elements = [
        {
            "tag": "markdown",
            "content": (
                f"⏰ 任务排队 **{waited_seconds:.0f}s** 后暂未分配\n\n"
                f"> {preview}\n\n"
                "系统正在自动恢复并尝试重新分配，任务已保留在队列中"
            ),
        },
    ]
    return build_card_wrapper(
        header_title="⏰ 排队超时",
        header_template="red",
        elements=elements,
    )


def build_retry_swap_card(
    *,
    failed_agent_name: str,
    new_agent_name: str,
    error_hint: str = "",
) -> dict:
    """Card shown when primary agent fails and we retry with an alternative."""
    from ..safe_error import safe_error_message as _safe_msg

    # Sanitize error hint for user display
    hint = ""
    if error_hint:
        safe_hint = _safe_msg(RuntimeError(error_hint)) if error_hint else ""
        hint = f"\n> 原因: {safe_hint[:120]}"
    elements = [
        {
            "tag": "markdown",
            "content": (
                f"🔄 **{failed_agent_name}** 执行失败，"
                f"已自动切换到 **{new_agent_name}** 重试{hint}"
            ),
        },
    ]
    return build_card_wrapper(
        header_title="🔄 Agent 切换重试",
        header_template="wathet",
        elements=elements,
    )


# Reason code to user-friendly Chinese message mapping
_REASON_TO_MESSAGE: dict[str, str] = {
    "rate_limit": "触发频率过高，请稍后再试",
    "admin_required": "需要管理员权限，请联系群管理员",
    "not_whitelisted": "你不在白名单中，请联系管理员添加",
}


def _get_reason_message(reason: str) -> str:
    """Convert a reason code to a user-friendly Chinese message.

    Args:
        reason: The reason code from ActivationGuard.

    Returns:
        A user-friendly Chinese message, or a default message if the reason is unknown.
    """
    return _REASON_TO_MESSAGE.get(reason, "权限或频率限制")


def build_activation_denied_card(
    *,
    reason: str = "权限不足",
    hint: str = "",
) -> dict:
    """Card shown when auto-activation is denied by the guard.

    Args:
        reason: Why the activation was denied (e.g., "rate_limit", "not_whitelisted").
        hint: Additional guidance for the user.
    """
    reason_msg = _get_reason_message(reason)
    body_parts = [f"你的消息暂时无法被自动处理。\n\n**原因**: {reason_msg}"]
    if hint:
        body_parts.append(f"\n{hint}")

    # Customize suggested actions based on reason
    if reason == "rate_limit":
        body_parts.append(
            "\n**建议操作:**\n"
            "- 请稍后再试\n"
            "- 或直接描述你的需求，让系统尝试其他方式处理"
        )
    else:
        body_parts.append(
            "\n**建议操作:**\n"
            "- 请联系群管理员\n"
            "- 或直接描述你的需求，让系统尝试其他方式处理"
        )

    elements = [
        {"tag": "markdown", "content": "\n".join(body_parts)},
    ]
    return build_card_wrapper(
        header_title="暂时无法处理",
        header_template="red",
        elements=elements,
    )


def build_no_agent_available_card(
    team_name: str,
    hint: str = "",
) -> dict:
    """Card shown when bootstrap completed but no agents are available.

    Args:
        team_name: The team/channel name.
        hint: Optional additional guidance for the admin.
    """
    body_parts = [
        f"**{team_name}** 正在重建团队角色，任务已保留在队列中。",
        "系统将在角色恢复后自动分配任务。",
    ]
    if hint:
        body_parts.append(f"\n> {hint}")

    elements = [
        {"tag": "markdown", "content": "\n".join(body_parts)},
    ]
    return build_card_wrapper(
        header_title="⚠️ 暂无可用角色",
        header_template="orange",
        elements=elements,
    )


def build_queue_full_card(
    *,
    message_preview: str = "",
    max_size: int = 8,
) -> dict:
    """Card shown when the task queue is at capacity and cannot accept new tasks.

    Args:
        message_preview: Truncated task text for user context.
        max_size: The queue capacity that was reached.
    """
    preview = _sanitize_preview(message_preview)
    elements = [
        {
            "tag": "markdown",
            "content": (
                f"🚦 系统繁忙，任务队列已满（上限: **{max_size}**）\n\n"
                f"> {preview}\n\n"
                "请稍后重试，或等待现有任务完成后再发送。"
            ),
        },
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "任务未被接收，请稍后重试"},
            ],
        },
    ]
    return build_card_wrapper(
        header_title="🚦 系统繁忙",
        header_template="red",
        elements=elements,
    )


def build_clarification_card(
    *,
    message_preview: str = "",
    channel_id: str = "",
    message_id: str = "",
    sender_id: str = "",
) -> dict:
    """Card shown when task classification is uncertain and needs user clarification.

    Args:
        message_preview: Truncated message text for context.
        channel_id: The channel ID where the message was sent.
        message_id: The original message ID for reference.
        sender_id: The original message sender ID for callback verification.
    """
    preview = _sanitize_preview(message_preview)
    elements = [
        {
            "tag": "markdown",
            "content": (
                "🤔 我不确定这是不是一个任务\n\n"
                f"> {preview}\n\n"
                "请确认：这是需要 Agent 处理的任务吗？"
            ),
        },
    ]

    # Action buttons
    buttons = [
        build_callback_button(
            "是，这是任务",
            "slock_clarify_confirm",
            channel_id=channel_id,
            button_type="primary",
            extra_value={
                "message_preview": message_preview,
                "message_id": message_id,
                "sender_id": sender_id,
            },
        ),
        build_callback_button(
            "不是，只是聊天",
            "slock_clarify_ignore",
            channel_id=channel_id,
            button_type="default",
            extra_value={
                "message_preview": message_preview,
                "message_id": message_id,
                "sender_id": sender_id,
            },
        ),
    ]
    elements.extend(build_responsive_layout(buttons))

    return build_card_wrapper(
        header_title="需要确认",
        header_template="blue",
        elements=elements,
    )


def build_clarification_confirmed_card(
    *,
    message_preview: str = "",
) -> dict:
    """Card shown after user confirms the message is a task.

    Args:
        message_preview: Truncated message text for context.
    """
    preview = _sanitize_preview(message_preview)
    elements = [
        {
            "tag": "markdown",
            "content": (
                "✅ **已确认这是任务**\n\n"
                f"> {preview}\n\n"
                "任务已加入队列，Agent 空闲后将自动处理。"
            ),
        },
    ]
    return build_card_wrapper(
        header_title="任务已入队",
        header_template="green",
        elements=elements,
    )


def build_clarification_ignored_card(
    *,
    message_preview: str = "",
) -> dict:
    """Card shown after user indicates the message is just chat.

    Args:
        message_preview: Truncated message text for context.
    """
    preview = _sanitize_preview(message_preview)
    elements = [
        {
            "tag": "markdown",
            "content": (
                "👋 **已忽略**\n\n"
                f"> {preview}\n\n"
                "此消息已被标记为普通聊天，不会创建任务。"
            ),
        },
    ]
    return build_card_wrapper(
        header_title="已忽略",
        header_template="grey",
        elements=elements,
    )


def build_result_card(
    *,
    task_preview: str = "",
    result: str = "",
) -> dict:
    """Card shown when a queued task completes with final result.

    Args:
        task_preview: Preview of the original task.
        result: The final result from the agent.
    """
    preview = _sanitize_preview(task_preview)

    elements = []
    if preview:
        elements.append({
            "tag": "markdown",
            "content": f"**任务**: {preview}",
        })

    # Short result (<= 500 chars): display directly
    if len(result) <= 500:
        elements.append({
            "tag": "markdown",
            "content": f"**结果**:\n{result}",
        })
    else:
        # Long result (> 500 chars): show preview + collapsible panel for full content
        result_preview = result[:200] + "..."
        elements.append({
            "tag": "markdown",
            "content": f"**结果**:\n{result_preview}",
        })
        elements.append(build_collapsible_panel(
            title="查看完整结果",
            elements=[{"tag": "markdown", "content": result[:2000]}],
            expanded=False,
        ))

    return build_card_wrapper(
        header_title="✅ 任务完成",
        header_template="green",
        elements=elements,
    )
