from __future__ import annotations

import json
from typing import Optional

from .core import CoreBuilder


class WorktreeBuilder:
    """Card builders for the worktree multi-tool orchestration flow."""

    # ------------------------------------------------------------------
    # Selection phase cards
    # ------------------------------------------------------------------

    @staticmethod
    def build_worktree_tool_select_card(
        tools: list[dict],
        selected_items: list[dict],
        project_id: Optional[str] = None,
        message: str = "",
    ) -> tuple[str, str]:
        """Render tool selection card with buttons for each available tool.

        *tools*: ``[{"tool_name": ..., "display_name": ..., "provider": ..., "supports_model": bool, "description": ...}]``
        *selected_items*: already-selected ``[{"display_label": ...}]`` for display.
        """
        lines = []
        lines.append("**请选择一个工具加入 Worktree 组合：**\n")
        for t in tools:
            desc = t.get("description") or ""
            lines.append(f"- **{t['display_name']}** {f'— {desc}' if desc else ''}")

        elements: list[dict] = []
        if message:
            elements.append(CoreBuilder._build_banner_element(message, type="success"))

        elements.append(CoreBuilder._build_content_element("\n".join(lines)))

        # Already-selected list
        if selected_items:
            sel_lines = "\n".join(
                f"{i}. {item.get('display_label', item.get('display_name', ''))}"
                for i, item in enumerate(selected_items, 1)
            )
            elements.append(CoreBuilder._build_content_element(f"**已选组合：**\n{sel_lines}"))

        # Tool buttons
        buttons: list[dict] = []
        for t in tools:
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": t["display_name"]},
                    "type": "default",
                    "value": {
                        "action": "worktree_select_tool",
                        "tool_name": t["tool_name"],
                        "provider": t.get("provider", ""),
                        "supports_model": t.get("supports_model", False),
                        "project_id": project_id or "",
                    },
                }
            )
        if buttons:
            elements.append({"tag": "action", "actions": buttons})

        # Finish button (resident)
        if selected_items:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "完成选择"},
                            "type": "primary",
                            "value": {
                                "action": "worktree_finish_selection",
                                "project_id": project_id or "",
                            },
                        }
                    ],
                }
            )

        card = CoreBuilder._wrap_card("🌳 Worktree — 选择工具", "turquoise", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_model_select_card(
        models: list[dict],
        tool_display_name: str,
        selected_items: list[dict],
        project_id: Optional[str] = None,
        message: str = "",
    ) -> tuple[str, str]:
        """Render model selection card for a TTADK tool.

        *models*: ``[{"name": ..., "display_name": ..., "is_default": bool}]``
        """
        elements: list[dict] = []
        if message:
            elements.append(CoreBuilder._build_banner_element(message, type="success"))

        elements.append(
            CoreBuilder._build_content_element(
                f"**为 {tool_display_name} 选择模型：**"
            )
        )

        # Already-selected list
        if selected_items:
            sel_lines = "\n".join(
                f"{i}. {item.get('display_label', item.get('display_name', ''))}"
                for i, item in enumerate(selected_items, 1)
            )
            elements.append(CoreBuilder._build_content_element(f"**已选组合：**\n{sel_lines}"))

        # Model buttons
        buttons: list[dict] = []
        for m in models:
            label = m.get("display_name") or m["name"]
            if m.get("is_default"):
                label += " (默认)"
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "default",
                    "value": {
                        "action": "worktree_select_model",
                        "model_name": m["name"],
                        "model_display_name": m.get("display_name") or m["name"],
                        "project_id": project_id or "",
                    },
                }
            )
        # Skip model button
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "跳过（使用默认模型）"},
                "type": "default",
                "value": {
                    "action": "worktree_select_model",
                    "model_name": "",
                    "model_display_name": "",
                    "project_id": project_id or "",
                },
            }
        )
        # Finish button
        if selected_items:
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "完成选择"},
                    "type": "primary",
                    "value": {
                        "action": "worktree_finish_selection",
                        "project_id": project_id or "",
                    },
                }
            )

        if buttons:
            elements.append({"tag": "action", "actions": buttons})

        card = CoreBuilder._wrap_card("🌳 Worktree — 选择模型", "turquoise", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_confirm_card(
        selected_items: list[dict],
        project_id: Optional[str] = None,
    ) -> tuple[str, str]:
        """Show final selection list and ask for confirmation to start."""
        parts = ["**即将启动以下工具-模型组合：**\n"]
        for i, item in enumerate(selected_items, 1):
            parts.append(f"{i}. {item.get('display_label', item.get('display_name', ''))}")
        parts.append("\n输入任务需求并启动。")

        elements: list[dict] = [CoreBuilder._build_content_element("\n".join(parts))]

        # Add input component for immediate goal entry
        elements.append(
            {
                "tag": "input",
                "name": "worktree_goal",
                "placeholder": {"tag": "plain_text", "content": "任务需求"},
                "max_length": 500,
            }
        )

        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认并开始执行"},
                        "type": "primary",
                        "value": {
                            "action": "worktree_confirm_start",
                            "project_id": project_id or "",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "重新选择"},
                        "type": "default",
                        "value": {
                            "action": "show_worktree_menu",
                            "project_id": project_id or "",
                        },
                    },
                ],
            }
        )

        card = CoreBuilder._wrap_card("🌳 Worktree — 确认组合", "green", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Execution phase cards
    # ------------------------------------------------------------------

    @staticmethod
    def build_worktree_progress_card(
        units: list[dict],
        project_id: Optional[str] = None,
        message: str = "",
    ) -> tuple[str, str]:
        """Render execution progress for all worktree units."""
        status_icons = {
            "pending": "⏳",
            "planned": "📋",
            "ready": "🟡",
            "running": "🔄",
            "completed": "✅",
            "failed": "❌",
        }
        parts: list[str] = []
        parts.append("**执行进度：**\n")
        for unit in units:
            icon = status_icons.get(unit.get("status", "pending"), "⏳")
            name = unit.get("display_name", unit.get("tool_name", ""))
            title = unit.get("task_title", "")
            status = unit.get("status", "pending")
            parts.append(f"{icon} **{name}** · `{status}` · {title}")

        elements: list[dict] = []
        if message:
            elements.append(CoreBuilder._build_banner_element(message, type="info"))

        elements.append(CoreBuilder._build_content_element("\n".join(parts)))
        
        # If all units are ready, add a clear input box and an explicit 'Execute' button
        is_ready = any(u.get("status") == "ready" for u in units)
        if is_ready:
            elements.append(
                {
                    "tag": "input",
                    "name": "worktree_goal",
                    "placeholder": {"tag": "plain_text", "content": "任务需求..."},
                    "max_length": 500,
                }
            )
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "🚀 开始并行执行"},
                            "type": "primary",
                            "value": {
                                "action": "worktree_execute_action",
                                "project_id": project_id or "",
                            },
                        }
                    ],
                }
            )

        card = CoreBuilder._wrap_card("🌳 Worktree — 执行中", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Merge / cleanup cards
    # ------------------------------------------------------------------

    @staticmethod
    def build_worktree_cleanup_card(
        merge_notes: list[str],
        project_id: Optional[str] = None,
        base_branch: str = "main",
        merge_results: list[dict] | None = None,
    ) -> tuple[str, str]:
        """Card with merge + cleanup action buttons."""
        parts = [f"**目标分支：** `{base_branch}`\n"]

        if merge_results:
            parts.append("**合并结果：**")
            for r in merge_results:
                icon = "✅" if r.get("success") else "❌"
                name = r.get("display_name", r.get("branch_name", ""))
                detail = r.get("detail", "")
                parts.append(f"{icon} {name} — {detail}")
            parts.append("")

        if merge_notes:
            parts.append("**待集成项：**")
            parts.extend(merge_notes)

        elements: list[dict] = [CoreBuilder._build_content_element("\n".join(parts))]
        actions: list[dict] = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "合并所有分支"},
                "type": "primary",
                "value": {
                    "action": "worktree_merge",
                    "project_id": project_id or "",
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "清理 Worktree"},
                "type": "danger",
                "value": {
                    "action": "worktree_cleanup",
                    "project_id": project_id or "",
                },
            },
        ]
        elements.append({"tag": "action", "actions": actions})

        card = CoreBuilder._wrap_card("🌳 Worktree — 集成与清理", "purple", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
