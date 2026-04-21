from __future__ import annotations

import json
import re
from typing import Optional

from ..styles import UI_TEXT
from .core import CoreBuilder
from ..models import WorktreeBannerContext

_WHITESPACE_RE = re.compile(r'\s+')


class WorktreeBuilder:
    """Card builders for the worktree multi-tool orchestration flow."""

    # ------------------------------------------------------------------
    # Selection phase cards
    # ------------------------------------------------------------------

    @staticmethod
    def _shorten_goal_for_banner(goal: str, max_len: int = 80) -> str:
        """Return a short, single-line goal summary for banner display.

        使用字符级截断，避免在 Banner 中出现过长文案；
        清洗目标文本中的所有换行符（将其合并为单空格），确保最终是纯粹的单行文本。
        仅在超过 *max_len* 时追加省略号，尽量保持原始语义。
        如果包含 Markdown 标记（如 `**`），则直接剥离，以避免由于截断或连续空格导致飞书卡片渲染崩溃。
        """
        # 剥除所有 ** 标记避免飞书 Markdown 加粗语法解析失败
        text = str(goal or "").replace("**", "")
        text = _WHITESPACE_RE.sub(' ', text).strip()
        
        if len(text) <= max_len:
            return text
            
        # 保留前 max_len-3 个字符，末尾补 "..." 以提示截断，并再次清洗可能的右侧空格
        truncated = text[: max_len - 3]
        if truncated.endswith(' '):
            truncated = truncated.rstrip()
        
        # 如果因为移除右侧空格导致长度不够了，省略号依然只补3个字符以保证总长 <= max_len
        return truncated + "..."

    @staticmethod
    def _build_selection_summary(selected_items: list[dict], max_items: int = 3, max_chars: int = 80) -> str:
        """Build a compact "tool · model" summary line for banner.

        - 仅使用前 *max_items* 个组合，避免移动端 Banner 过长；
        - 基于 ``display_label`` 做轻量转换，将 " / " 替换为 " · "；
        - 超过 *max_chars* 时整体截断并加省略号。
        """
        labels: list[str] = []
        for idx, item in enumerate(selected_items):
            if idx >= max_items:
                break
            raw = str(item.get("display_label") or item.get("display_name") or item.get("tool_name") or "").strip()
            if not raw:
                continue
            # 统一成「工具 · 模型」风格
            label = raw.replace(" / ", " · ")
            labels.append(label)

        if not labels:
            return ""

        summary = ", ".join(labels)
        if len(summary) > max_chars:
            summary = summary[: max_chars - 3] + "..."
        return summary

    @staticmethod
    def _build_auto_execute_banner_text(ctx: WorktreeBannerContext) -> str:
        """基于 ``WorktreeBannerContext`` 组装 Worktree 自动执行/启动 Banner 文案。

        行为约定（单一入口）：
        - 仅依赖 *ctx* 中的字段，不再接受散落的 message/goal/selected_items 入参；
        - 第 1 行：`ctx.message`（通常是 UI_TEXT["worktree_auto_executing_banner"]），为空则跳过；
        - 第 2 行：如有 goal，则显示精简后的「goal 摘要」：`「{short_goal}」`；
        - 第 3 行：如有已选工具/模型，则显示 "使用：Coco · gpt-5.1" 风格摘要；
        - 当 selected_items 为空或 None 时，不输出 "使用：" 行，以保持既有测试约定。

        兼容性说明：
        - 所有字段均有默认值，旧调用方只填充部分字段时行为保持稳定；
        - 当前仍仅使用 selected_items 作为工具/模型展示来源，tool_name/model_name
          与 banner_kind 主要用于后续扩展，不影响现有文案格式。
        """

        # 统一从 ctx 中读取所需字段，并对空值做安全处理
        base_message = str(getattr(ctx, "message", "") or "").strip()
        goal_text = str(getattr(ctx, "goal", "") or "")
        selected_items = list(getattr(ctx, "selected_items", None) or [])

        banner_lines: list[str] = []
        if base_message:
            banner_lines.append(base_message)

        short_goal = WorktreeBuilder._shorten_goal_for_banner(goal_text)
        if short_goal:
            banner_lines.append(f"「{short_goal}」")

        selection_summary = WorktreeBuilder._build_selection_summary(selected_items)
        if selection_summary:
            banner_lines.append(f"使用：{selection_summary}")

        return "\n".join(banner_lines)

    @staticmethod
    def build_worktree_tool_select_card(
        tools: list[dict],
        selected_items: list[dict],
        project_id: Optional[str] = None,
        message: str = "",
        goal: str = "",
        banner_ctx: WorktreeBannerContext | None = None,
    ) -> tuple[str, str]:
        """Render tool selection card with buttons for each available tool.

        *tools*: ``[{"tool_name": ..., "display_name": ..., "provider": ..., "supports_model": bool, "description": ...}]``
        *selected_items*: already-selected ``[{"display_label": ...}]`` for display.
        *goal*: pre-filled task goal; when empty, renders an input box.
        """
        lines = []
        lines.append(UI_TEXT["worktree_select_tool_prompt"])
        for t in tools:
            desc = t.get("description") or ""
            lines.append(f"- **{t['display_name']}** {f'— {desc}' if desc else ''}")

        elements: list[dict] = []
        if message:
            banner_text = message
            if message == UI_TEXT["worktree_auto_executing_banner"]:
                # 统一从 WorktreeBannerContext 构造 Banner，上层仅负责汇总上下文
                ctx = banner_ctx or WorktreeBannerContext()
                ctx = WorktreeBannerContext(
                    message=message,
                    goal=goal or ctx.goal,
                    tool_name=ctx.tool_name,
                    model_name=ctx.model_name,
                    is_auto_execute=ctx.is_auto_execute,
                    selected_items=selected_items or ctx.selected_items,
                    banner_kind=ctx.banner_kind or "auto_execute",
                )
                banner_text = WorktreeBuilder._build_auto_execute_banner_text(ctx)
            elements.append(CoreBuilder._build_banner_element(banner_text, type="success"))

        # Goal area: show read-only display if goal is set, otherwise input box
        if goal:
            elements.append(
                CoreBuilder._build_content_element(UI_TEXT["worktree_goal_label"].format(goal=goal))
            )
        else:
            elements.append(
                {
                    "tag": "input",
                    "name": "worktree_goal",
                    "placeholder": {"tag": "plain_text", "content": UI_TEXT["worktree_goal_placeholder"]},
                    "max_length": 500,
                }
            )

        elements.append(CoreBuilder._build_content_element("\n".join(lines)))

        # Already-selected list
        if selected_items:
            sel_lines = "\n".join(
                f"{i}. {item.get('display_label', item.get('display_name', ''))}"
                for i, item in enumerate(selected_items, 1)
            )
            elements.append(CoreBuilder._build_content_element(UI_TEXT["worktree_selected_header"] + sel_lines))

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
                        "goal": goal,
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
                            "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_finish"]},
                            "type": "primary",
                            "value": {
                                "action": "worktree_finish_selection",
                                "project_id": project_id or "",
                                "goal": goal,
                            },
                        }
                    ],
                }
            )

        card = CoreBuilder._wrap_card(UI_TEXT["worktree_select_tool_title"], "turquoise", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_model_select_card(
        models: list[dict],
        tool_display_name: str,
        selected_items: list[dict],
        project_id: Optional[str] = None,
        message: str = "",
        goal: str = "",
        banner_ctx: WorktreeBannerContext | None = None,
    ) -> tuple[str, str]:
        """Render model selection card for a TTADK tool.

        *models*: ``[{"name": ..., "display_name": ..., "is_default": bool}]``
        """
        elements: list[dict] = []
        if message:
            banner_text = message
            if message == UI_TEXT["worktree_auto_executing_banner"]:
                ctx = banner_ctx or WorktreeBannerContext()
                ctx = WorktreeBannerContext(
                    message=message,
                    goal=goal or ctx.goal,
                    tool_name=ctx.tool_name,
                    model_name=ctx.model_name,
                    is_auto_execute=ctx.is_auto_execute,
                    selected_items=selected_items or ctx.selected_items,
                    banner_kind=ctx.banner_kind or "auto_execute",
                )
                banner_text = WorktreeBuilder._build_auto_execute_banner_text(ctx)
            elements.append(CoreBuilder._build_banner_element(banner_text, type="success"))

        # Show goal if present
        if goal:
            elements.append(
                CoreBuilder._build_content_element(UI_TEXT["worktree_goal_label"].format(goal=goal))
            )

        elements.append(
            CoreBuilder._build_content_element(
                UI_TEXT["system_worktree_select_model_prompt"].format(tool=tool_display_name)
            )
        )

        # Already-selected list
        if selected_items:
            sel_lines = "\n".join(
                f"{i}. {item.get('display_label', item.get('display_name', ''))}"
                for i, item in enumerate(selected_items, 1)
            )
            elements.append(CoreBuilder._build_content_element(UI_TEXT["worktree_selected_header"] + sel_lines))

        # Model buttons
        buttons: list[dict] = []
        for m in models:
            label = m.get("display_name") or m["name"]
            if m.get("is_default"):
                label += f" ({UI_TEXT['system_not_set']})"
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
                        "goal": goal,
                    },
                }
            )
        # Skip model button
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": UI_TEXT["worktree_skip_model_btn"]},
                "type": "default",
                "value": {
                    "action": "worktree_select_model",
                    "model_name": "",
                    "model_display_name": "",
                    "project_id": project_id or "",
                    "goal": goal,
                },
            }
        )
        # Finish button
        if selected_items:
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_finish"]},
                    "type": "primary",
                    "value": {
                        "action": "worktree_finish_selection",
                        "project_id": project_id or "",
                        "goal": goal,
                    },
                }
            )

        if buttons:
            elements.append({"tag": "action", "actions": buttons})

        card = CoreBuilder._wrap_card(UI_TEXT["worktree_select_model_title"], "turquoise", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_confirm_card(
        selected_items: list[dict],
        project_id: Optional[str] = None,
        message: str = "",
        goal: str = "",
        banner_ctx: WorktreeBannerContext | None = None,
    ) -> tuple[str, str]:
        """Show final selection list and ask for confirmation to start."""
        parts = [UI_TEXT["worktree_confirm_header"]]
        for i, item in enumerate(selected_items, 1):
            parts.append(f"{i}. {item.get('display_label', item.get('display_name', ''))}")

        elements: list[dict] = []
        if message:
            # 在 Banner 中同时展示执行状态 + 精简后的 goal 与工具/模型摘要，
            # 让用户在自动执行/跳过确认时也能一眼看到“在用什么帮我做什么”。
            banner_text = message
            if message == UI_TEXT["worktree_auto_executing_banner"]:
                ctx = banner_ctx or WorktreeBannerContext()
                ctx = WorktreeBannerContext(
                    message=message,
                    goal=goal or ctx.goal,
                    tool_name=ctx.tool_name,
                    model_name=ctx.model_name,
                    is_auto_execute=ctx.is_auto_execute,
                    selected_items=selected_items or ctx.selected_items,
                    banner_kind=ctx.banner_kind or "auto_execute",
                )
                banner_text = WorktreeBuilder._build_auto_execute_banner_text(ctx)

            elements.append(CoreBuilder._build_banner_element(banner_text, type="info"))

        elements.append(CoreBuilder._build_content_element("\n".join(parts)))

        # Add guiding banner
        elements.append(
            CoreBuilder._build_banner_element(
                UI_TEXT["worktree_confirm_banner"], type="info"
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
                                "placeholder": {"tag": "plain_text", "content": UI_TEXT["worktree_input_placeholder"]},
                                "max_length": 500,
                            },
                            {
                                "tag": "action",
                                "actions": [
                                    {
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_confirm"]},
                                        "type": "primary",
                                        "value": {
                                            "action": "worktree_confirm_start",
                                            "project_id": project_id or "",
                                        },
                                    },
                                    {
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_reselect"]},
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

        card = CoreBuilder._wrap_card(UI_TEXT["worktree_confirm_title"], "green", elements)
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
            return UI_TEXT["system_status_completed"], "green"
        if has_running:
            return UI_TEXT["system_status_executing"], "blue"
        if has_failed:
            return UI_TEXT["system_status_partial_failed"], "red"
        if all(s == "ready" for s in statuses):
            return UI_TEXT["system_status_ready"], "turquoise"
        return UI_TEXT["system_status_preparing"], "blue"

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
        parts.append(UI_TEXT["worktree_progress_header"])
        for unit in units:
            icon = status_icons.get(unit.get("status", "pending"), "⏳")
            name = unit.get("display_name", unit.get("tool_name", ""))
            title = unit.get("task_title", "")
            status = unit.get("status", "pending")
            parts.append(f"{icon} **{name}** · `{status}` · {title}")
            if status == "failed":
                error_detail = unit.get("error") or UI_TEXT["system_unknown_execution_error"]
                parts.append(UI_TEXT["worktree_fail_reason"].format(error=error_detail))

        elements: list[dict] = []
        if message:
            elements.append(CoreBuilder._build_banner_element(message, type="info"))

        elements.append(CoreBuilder._build_content_element("\n".join(parts)))
        
        # If all units are ready, add a clear input box and an explicit 'Execute' button
        is_ready = any(u.get("status") == "ready" for u in units)
        if is_ready:
            elements.append(
                CoreBuilder._build_banner_element(
                    UI_TEXT["worktree_ready_banner"], type="info"
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
                                    "placeholder": {"tag": "plain_text", "content": UI_TEXT["worktree_input_placeholder"]},
                                    "max_length": 500,
                                },
                                {
                                    "tag": "action",
                                    "actions": [
                                        {
                                            "tag": "button",
                                            "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_execute"]},
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
                            "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_retry"]},
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
        card = CoreBuilder._wrap_card(UI_TEXT["worktree_progress_title"].format(status=title_suffix), header_color, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_result_card(
        selected_items: list,
        unit_summary_lines: list[str],
        project_id: Optional[str] = None,
        merge_entry_ready: bool = False,
        message: str = "",
    ) -> tuple[str, str]:
        """Render a card showing execution results of all worktree units."""
        elements = [
            CoreBuilder._build_content_element(
                UI_TEXT["worktree_result_header"].format(message=message) + "\n".join(unit_summary_lines)
            ),
        ]
        if merge_entry_ready:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_view_merge"]},
                            "type": "primary",
                            "value": {
                                "action": "show_worktree_merge_entry",
                                "project_id": project_id or "",
                            },
                        }
                    ],
                }
            )
        
        card = CoreBuilder._wrap_card(UI_TEXT["worktree_result_title"], "turquoise", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_merge_entry_card(
        merge_notes: list[str],
        project_id: Optional[str] = None,
        base_branch: str = "main",
    ) -> tuple[str, str]:
        """Render a card listing all pending integration items."""
        content = UI_TEXT["worktree_merge_entry_header"].format(base=base_branch) + "\n".join(merge_notes)
        elements = [CoreBuilder._build_content_element(content)]
        
        card = CoreBuilder._wrap_card(UI_TEXT["worktree_merge_entry_title"], "purple", elements)
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
        parts = [UI_TEXT["worktree_cleanup_header"].format(base=base_branch)]

        if merge_results:
            parts.append(UI_TEXT["worktree_merge_result_header"])
            for r in merge_results:
                icon = "✅" if r.get("success") else "❌"
                name = r.get("display_name", r.get("branch_name", ""))
                detail = r.get("detail", "")
                parts.append(UI_TEXT["worktree_merge_item"].format(icon=icon, name=name, detail=detail))
            parts.append("")

        if merge_notes:
            parts.append(UI_TEXT["worktree_pending_merge_header"])
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
            summary_lines: list[str] = [UI_TEXT["worktree_failed_units_header"]]
            for u in failed_units[:max_display]:
                name = u.get("display_name") or u.get("tool_name") or UI_TEXT["system_unknown_unit"]
                task_title = (u.get("task_title") or "").strip()
                error = (u.get("error") or UI_TEXT["system_unknown_execution_error"]).strip() or UI_TEXT["system_unknown_execution_error"]
                if len(error) > 80:
                    error = error[:77] + "..."
                if task_title:
                    summary_lines.append(UI_TEXT["worktree_failed_unit_item"].format(name=name, title=task_title, error=error))
                else:
                    summary_lines.append(UI_TEXT["worktree_failed_unit_item_no_title"].format(name=name, error=error))
            overflow = len(failed_units) - max_display
            if overflow > 0:
                summary_lines.append(UI_TEXT["worktree_failed_overflow"].format(count=overflow))
            elements.append(CoreBuilder._build_content_element("\n".join(summary_lines)))

        merge_label = UI_TEXT["worktree_btn_merge_partial"] if show_retry else UI_TEXT["worktree_btn_merge_all"]
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
                    "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_retry"]},
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
                "text": {"tag": "plain_text", "content": UI_TEXT["worktree_btn_cleanup"]},
                "type": "danger",
                "value": {
                    "action": "worktree_cleanup",
                    "project_id": project_id or "",
                },
            },
        )
        elements.append({"tag": "action", "actions": actions})

        card = CoreBuilder._wrap_card(UI_TEXT["worktree_cleanup_card_title"], "purple", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
