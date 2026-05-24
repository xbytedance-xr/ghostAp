"""Memory card templates for Slock agents.

Displays grouped memory items with collapsible category panels,
following the Feishu card 2.0 structure.
"""

from __future__ import annotations

from collections import defaultdict

from .common import (
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_mobile_card_row,
    build_responsive_layout,
    truncate_dynamic_label,
)

# Human-readable labels and icons for known memory categories
CATEGORY_DISPLAY: dict[str, str] = {
    "key_knowledge": "🔑 关键知识",
    "experience": "💡 经验",
    "context": "💭 上下文",
    "role": "📋 角色定义",
    "archived": "📦 归档",
}


def _category_label(category: str) -> str:
    """Return display label for a memory category."""
    return CATEGORY_DISPLAY.get(category, f"📂 {category}")


def build_memory_group_card(
    agent_name: str,
    agent_emoji: str,
    memory_items: list[dict],  # Each has: category, content, timestamp (optional)
    *,
    channel_id: str = "",
    agent_id: str = "",
) -> dict:
    """Build a grouped memory display card for an agent.

    Groups memory items by category and renders each group as a
    collapsible panel. Each item is shown as a mobile-friendly row
    with truncated content.

    Args:
        agent_name: Display name of the agent.
        agent_emoji: Emoji icon for the agent.
        memory_items: List of memory dicts, each containing:
            - category (str): Grouping key (e.g. "key_knowledge", "experience").
            - content (str): The memory content text.
            - timestamp (str, optional): ISO timestamp of when it was stored.
        channel_id: Feishu channel ID for callback routing.
        agent_id: Agent identifier for callback routing.

    Returns:
        A complete Feishu Interactive Card 2.0 dict.
    """
    # Group items by category, preserving insertion order
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in memory_items:
        category = item.get("category", "context")
        grouped[category].append(item)

    # Build collapsible panels per category
    elements: list[dict] = []
    for category, items in grouped.items():
        panel_rows: list[dict] = []
        for item in items:
            content = item.get("content", "")
            truncated = truncate_dynamic_label(content, max_len=100)
            timestamp = item.get("timestamp", "")

            # Title elements: the truncated content as markdown
            title_elements: list[dict] = [
                {"tag": "markdown", "content": truncated},
            ]

            # Content elements: timestamp if available
            content_elements: list[dict] | None = None
            if timestamp:
                content_elements = [
                    {
                        "tag": "markdown",
                        "content": f"<font color='grey'>{timestamp}</font>",
                    },
                ]

            row = build_mobile_card_row(
                title_elements=title_elements,
                content_elements=content_elements,
                background_style="grey",
                margin="4px 0px",
            )
            panel_rows.append(row)

        # First category panel starts expanded, rest collapsed
        expanded = len(elements) == 0
        label = _category_label(category)
        panel = build_collapsible_panel(
            f"**{label}** ({len(items)})",
            panel_rows,
            expanded=expanded,
        )
        elements.append(panel)

    # Empty state
    if not elements:
        elements.append(
            {"tag": "markdown", "content": "_暂无记忆内容_"}
        )

    # Separator + refresh button
    elements.append({"tag": "hr"})
    refresh_btn = build_callback_button(
        "🔄 刷新",
        "slock_refresh_memory",
        channel_id=channel_id,
        extra_value={"agent_id": agent_id},
    )
    elements.append(build_responsive_layout([refresh_btn]))

    header_title = f"{agent_emoji} {agent_name} 的记忆"
    return build_card_wrapper(
        header_title=header_title,
        header_template="turquoise",
        elements=elements,
    )
