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
                        "skip_model_selection": t.get("skip_model_selection", False),
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

        elements: list[dict] = [CoreBuilder._build_content_element("\n".join(parts))]

        # Add guiding banner
        elements.append(
            CoreBuilder._build_banner_element(
                "请在下方输入您的任务需求，点击按钮一键开启执行", type="info"
            )
        )

        # Hot area: Group input and actions in a column_set with background
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "stretch",
                "background_style": "wathet",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "input",
                                "name": "worktree_goal",
                                "placeholder": {"tag": "plain_text", "content": "任务需求"},
                                "max_length": 500,
                            },
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
                            },
                        ],
                    }
                ],
            }
        )

        card = CoreBuilder._wrap_card("🌳 Worktree — 确认组合", "green", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Execution phase cards
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_progress_title(units: list[dict]) -> tuple[str, str]:
        """Derive card title suffix and header color from unit statuses.

        Returns ``(title_suffix, color)`` – e.g. ``("就绪", "turquoise")``.
        Priority (highest → lowest):
        1. all completed          → ("已完成", "green")
        2. any running            → ("执行中", "blue")
        3. has failed & no running→ ("部分失败", "red")
        4. all ready              → ("就绪", "turquoise")
        5. fallback               → ("准备中", "blue")
        """
        if not units:
            return "执行中", "blue"

        statuses = [u.get("status", "pending") for u in units]
        has_running = "running" in statuses
        has_failed = "failed" in statuses

        if all(s == "completed" for s in statuses):
            return "已完成", "green"
        if has_running:
            return "执行中", "blue"
        if has_failed:
            return "部分失败", "red"
        if all(s == "ready" for s in statuses):
            return "就绪", "turquoise"
        return "准备中", "blue"

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
            if status == "failed":
                error_detail = unit.get("error") or "未知执行异常"
                parts.append(f"> 🔍 **失败原因**：{error_detail}")

        elements: list[dict] = []
        if message:
            elements.append(CoreBuilder._build_banner_element(message, type="info"))

        elements.append(CoreBuilder._build_content_element("\n".join(parts)))
        
        # If all units are ready, add a clear input box and an explicit 'Execute' button
        is_ready = any(u.get("status") == "ready" for u in units)
        if is_ready:
            elements.append(
                CoreBuilder._build_banner_element(
                    "所有单元已就绪，请录入总任务目标并开始执行", type="info"
                )
            )
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "stretch",
                    "background_style": "wathet",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "input",
                                    "name": "worktree_goal",
                                    "placeholder": {"tag": "plain_text", "content": "任务需求"},
                                    "max_length": 500,
                                },
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
                                },
                            ],
                        }
                    ],
                }
            )

        # Show retry button when there are failed units and nothing is running
        has_failed = any(u.get("status") == "failed" for u in units)
        has_running = any(u.get("status") == "running" for u in units)
        if has_failed and not has_running:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "🔄 重试失败单元"},
                            "type": "primary",
                            "value": {
                                "action": "worktree_retry_failed",
                                "project_id": project_id or "",
                            },
                        }
                    ],
                }
            )

        title_suffix, header_color = WorktreeBuilder._resolve_progress_title(units)
        card = CoreBuilder._wrap_card(f"🌳 Worktree — {title_suffix}", header_color, elements)
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
        units: list[dict] | None = None,
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

        # Determine if there are retryable failed units
        has_failed = bool(units) and any(u.get("status") == "failed" for u in units)
        has_running = bool(units) and any(u.get("status") == "running" for u in units)
        show_retry = has_failed and not has_running

        # Insert failed unit summary section before action buttons
        if show_retry:
            failed_units = [u for u in units if u.get("status") == "failed"]
            max_display = 5
            summary_lines: list[str] = ["**失败单元：**"]
            for u in failed_units[:max_display]:
                name = u.get("display_name") or u.get("tool_name") or "未知单元"
                task_title = (u.get("task_title") or "").strip()
                error = (u.get("error") or "未知执行异常").strip() or "未知执行异常"
                if len(error) > 80:
                    error = error[:77] + "..."
                if task_title:
                    summary_lines.append(f"❌ **{name}** · {task_title} — {error}")
                else:
                    summary_lines.append(f"❌ **{name}** — {error}")
            overflow = len(failed_units) - max_display
            if overflow > 0:
                summary_lines.append(f"...及 {overflow} 个其他失败单元")
            elements.append(CoreBuilder._build_content_element("\n".join(summary_lines)))

        merge_label = "✅ 先合并已完成" if show_retry else "合并所有分支"
        actions: list[dict] = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": merge_label},
                "type": "primary",
                "value": {
                    "action": "worktree_merge",
                    "project_id": project_id or "",
                },
            },
        ]
        if show_retry:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 重试失败单元"},
                    "type": "primary",
                    "value": {
                        "action": "worktree_retry_failed",
                        "project_id": project_id or "",
                    },
                }
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "清理 Worktree"},
                "type": "danger",
                "value": {
                    "action": "worktree_cleanup",
                    "project_id": project_id or "",
                },
            },
        )
        elements.append({"tag": "action", "actions": actions})

        card = CoreBuilder._wrap_card("🌳 Worktree — 集成与清理", "purple", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
