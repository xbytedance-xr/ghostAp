from __future__ import annotations

import json
from typing import Optional

from src.model_selection import DEFAULT_MODEL_OPTION_VALUE

from ..actions import dispatch as action_ids
from ..shared import build_responsive_layout
from ..ui_text import UI_TEXT
from .core import CoreBuilder


class SpecReviewBuilder:
    """Dedicated static cards for Spec review-agent selection."""

    @staticmethod
    def _card(title: str, elements: list[dict], *, template: str = "blue") -> tuple[str, str]:
        return "interactive", json.dumps(CoreBuilder._wrap_card(title, template, elements), ensure_ascii=False)

    @staticmethod
    def _compact(text: object, *, limit: int = 56) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[: max(1, limit - 1)].rstrip() + "…"

    @staticmethod
    def _value(data: dict) -> dict:
        return {k: v for k, v in data.items() if v is not None and v != ""}

    @staticmethod
    def _button(text: str, action: dict, *, button_type: str = "default") -> dict:
        value = SpecReviewBuilder._value(action)
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": SpecReviewBuilder._compact(text, limit=36)},
            "type": button_type,
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }

    @staticmethod
    def _selected_label(item: dict) -> str:
        return str(
            item.get("display_label")
            or item.get("label")
            or item.get("tool")
            or item.get("display_name")
            or item.get("tool_name")
            or ""
        ).strip()

    @staticmethod
    def _selected_elements(
        *,
        selected: list[dict],
        project_id: Optional[str],
        thread_root_id: str,
    ) -> list[dict]:
        if not selected:
            return [
                {
                    "tag": "markdown",
                    "content": "当前未选择额外 review 工具。可以直接使用 Auto，或添加工具后确认。",
                    "text_size": "notation",
                }
            ]

        lines = ["**已选 Review 工具**"]
        remove_buttons: list[dict] = []
        for item in selected:
            label = SpecReviewBuilder._selected_label(item)
            if label:
                lines.append(f"- {label}")
            key = str(item.get("selection_key") or "").strip()
            if key:
                remove_buttons.append(
                    SpecReviewBuilder._button(
                        f"移除 {label or '已选项'}",
                        {
                            "action": action_ids.SPEC_REVIEW_REMOVE_ITEM,
                            "project_id": project_id,
                            "thread_root_id": thread_root_id,
                            "selection_key": key,
                        },
                    )
                )

        elements: list[dict] = [{"tag": "markdown", "content": "\n".join(lines)}]
        if remove_buttons:
            elements.extend(build_responsive_layout(remove_buttons, mobile_force_vertical=False))
        elements.extend(
            build_responsive_layout(
                [
                    SpecReviewBuilder._button(
                        "清空已选",
                        {
                            "action": action_ids.SPEC_REVIEW_CLEAR_ITEMS,
                            "project_id": project_id,
                            "thread_root_id": thread_root_id,
                        },
                    ),
                    SpecReviewBuilder._button(
                        "确认并开始 Spec",
                        {
                            "action": action_ids.SPEC_REVIEW_FINISH_SELECTION,
                            "project_id": project_id,
                            "thread_root_id": thread_root_id,
                        },
                        button_type="primary",
                    ),
                ],
                mobile_force_vertical=False,
            )
        )
        return elements

    @staticmethod
    def _tool_row(
        tool: dict,
        *,
        project_id: Optional[str],
        thread_root_id: str,
        select_action: str,
    ) -> dict:
        tool_name = str(tool.get("tool_name") or tool.get("name") or tool.get("id") or "").strip()
        display = str(tool.get("display_name") or tool_name).strip() or tool_name
        desc = str(tool.get("description") or "").strip()
        label = f"**{display}**"
        if desc:
            label += f" — {desc}"
        value = {
            "action": select_action,
            "project_id": project_id,
            "thread_root_id": thread_root_id,
            "provider": tool.get("provider"),
            "tool_name": tool_name,
            "display_name": display,
            "agent_name": tool.get("agent_name"),
            "supports_model": bool(tool.get("supports_model", False)),
            "skip_model_selection": bool(tool.get("skip_model_selection", False)),
        }
        return {
            "tag": "column_set",
            "flex_mode": "stretch",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "vertical_align": "center",
                    "elements": [{"tag": "markdown", "content": label}],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [
                        SpecReviewBuilder._button(f"+ 添加 {display}", value),
                    ],
                },
            ],
        }

    @staticmethod
    def _model_button(
        model: dict,
        *,
        project_id: Optional[str],
        thread_root_id: str,
    ) -> dict:
        raw_id = str(model.get("id") or model.get("model_name") or model.get("name") or "").strip()
        use_default = bool(model.get("use_default_model")) or raw_id == DEFAULT_MODEL_OPTION_VALUE
        model_name = DEFAULT_MODEL_OPTION_VALUE if use_default else raw_id
        display = str(model.get("display_name") or model.get("name") or model_name).strip() or model_name
        desc = str(model.get("description") or "").strip()
        label = display
        if desc and desc != display:
            label = f"{display} ({desc})"
        value = {
            "action": action_ids.SPEC_REVIEW_SELECT_MODEL,
            "project_id": project_id,
            "thread_root_id": thread_root_id,
            "model_name": model_name,
            "model_display_name": None if use_default else display,
            "use_default_model": use_default or None,
        }
        return SpecReviewBuilder._button(label, value, button_type="primary" if use_default else "default")

    @staticmethod
    def build_agent_select_card(
        *,
        tools: list[dict],
        selected: list[dict] | None = None,
        project_id: Optional[str] = None,
        message: str = "",
        select_action: str = action_ids.SPEC_REVIEW_SELECT_TOOL,
        pending_tool: str = "",
        thread_root_id: str = "",
    ) -> tuple[str, str]:
        selected_items = [dict(item) for item in selected or [] if isinstance(item, dict)]
        if select_action == action_ids.SPEC_REVIEW_SELECT_MODEL:
            return SpecReviewBuilder._build_model_select_card(
                models=tools,
                selected=selected_items,
                project_id=project_id,
                message=message,
                pending_tool=pending_tool,
                thread_root_id=thread_root_id,
            )

        elements: list[dict] = [
            {
                "tag": "markdown",
                "content": (
                    "**选择后续多角色 review 可使用的工具和模型**\n\n"
                    f"{UI_TEXT['spec_review_auto_desc']}"
                ),
            },
        ]
        if message:
            elements.append({"tag": "markdown", "content": message})
        elements.extend(
            build_responsive_layout(
                [
                    SpecReviewBuilder._button(
                        UI_TEXT["spec_review_auto_btn"],
                        {
                            "action": action_ids.SPEC_REVIEW_USE_AUTO,
                            "project_id": project_id,
                            "thread_root_id": thread_root_id,
                        },
                        button_type="primary" if not selected_items else "default",
                    )
                ]
            )
        )
        elements.append({"tag": "hr"})
        if tools:
            for tool in tools:
                elements.append(
                    SpecReviewBuilder._tool_row(
                        dict(tool or {}),
                        project_id=project_id,
                        thread_root_id=thread_root_id,
                        select_action=select_action,
                    )
                )
        else:
            elements.append({"tag": "markdown", "content": "当前没有可用的 review 工具。"})
        elements.append({"tag": "hr"})
        elements.extend(
            SpecReviewBuilder._selected_elements(
                selected=selected_items,
                project_id=project_id,
                thread_root_id=thread_root_id,
            )
        )
        return SpecReviewBuilder._card("📋 Spec Review · 审查工具选择", elements)

    @staticmethod
    def _build_model_select_card(
        *,
        models: list[dict],
        selected: list[dict],
        project_id: Optional[str],
        message: str,
        pending_tool: str,
        thread_root_id: str,
    ) -> tuple[str, str]:
        tool_label = pending_tool or "当前工具"
        elements: list[dict] = [
            {
                "tag": "markdown",
                "content": f"**为 {tool_label} 选择 review 模型**\n\n点击模型后会回到本卡继续选择其他工具。",
            }
        ]
        if message:
            elements.append({"tag": "markdown", "content": message})
        if selected:
            lines = ["**已选 Review 工具**"]
            lines.extend(f"- {SpecReviewBuilder._selected_label(item)}" for item in selected if SpecReviewBuilder._selected_label(item))
            elements.append({"tag": "markdown", "content": "\n".join(lines)})
        buttons = [
            SpecReviewBuilder._model_button(dict(model or {}), project_id=project_id, thread_root_id=thread_root_id)
            for model in models or []
        ]
        elements.extend(build_responsive_layout(buttons))
        elements.append({"tag": "hr"})
        elements.extend(
            build_responsive_layout(
                [
                    SpecReviewBuilder._button(
                        "返回工具选择",
                        {
                            "action": action_ids.SHOW_SPEC_REVIEW_MENU,
                            "project_id": project_id,
                            "thread_root_id": thread_root_id,
                        },
                    )
                ]
            )
        )
        return SpecReviewBuilder._card(f"📋 Spec Review · {tool_label} 模型", elements)

    @staticmethod
    def build_starting_card(
        *,
        selected: list[dict] | None = None,
        auto: bool = False,
    ) -> tuple[str, str]:
        selected_items = [dict(item) for item in selected or [] if isinstance(item, dict)]
        if auto or not selected_items:
            body = (
                "**已选择 Auto**\n\n"
                "后续审查沿用当前主 agent + 模型，正在进入 Spec 执行流程。"
            )
        else:
            lines = ["**已确认 Review 工具**"]
            lines.extend(f"- {SpecReviewBuilder._selected_label(item)}" for item in selected_items if SpecReviewBuilder._selected_label(item))
            lines.append("")
            lines.append("正在进入 Spec 执行流程。")
            body = "\n".join(lines)
        return SpecReviewBuilder._card(
            "📋 Spec Review · 正在启动",
            [{"tag": "markdown", "content": body}],
            template="green",
        )
