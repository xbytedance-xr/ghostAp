"""统一卡片布局构建器 — 所有编程模式和引擎模式共享。

该模块提供 UnifiedCardLayout.build()，接受 CardLayoutSpec 并输出飞书 Schema 2.0
卡片的 body elements 列表。所有渲染器都委托此构建器来确保卡片布局在视觉上一致。

布局超集结构：
1. 📁 项目路径 + 状态栏（流式） / 元数据行（引擎）
2. 📌 置顶消息 / ⚠️ 警告横幅（可选）
3. ── 分隔线 ──
4. 📊 进度条（可选，引擎）
5. 🖼️ 图片（可选）
6. 📝 主内容（markdown 或 structured elements）
7. ── 分隔线 ──（如有验收标准）
8. 📋 验收标准区域（可选，Spec）
9. 📝 脚注（可选）
10. 📊 Footer 状态行（可选，notation 尺寸）
11. ── 分隔线 ──（如有按钮）
12. 🔘 操作按钮
13. ✅ 终态标记（可选）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NewType, Union

from ..shared import build_responsive_layout
from ..terminal import FOOTER_STATUS, TERMINAL_MARKERS
from ..ui_text import UI_TEXT

# Type-level distinction: FOOTER_STATUS dict keys vs pre-formatted display strings.
# At runtime FooterStatusKey is just ``str`` (NewType is a no-op).
FooterStatusKey = NewType("FooterStatusKey", str)

if TYPE_CHECKING:
    from ..models import CardLayoutSpec


class UnifiedCardLayout:
    """Pure static builder: CardLayoutSpec → list[element_dict]."""

    @staticmethod
    def build(spec: CardLayoutSpec) -> list[dict]:
        """根据 CardLayoutSpec 构建飞书卡片 body elements 列表。"""
        elements: list[dict] = []

        # ---- 1. 项目路径 + 状态/元数据 ----
        elements.append(_build_path_and_meta(spec))

        # ---- 1b. 引擎元数据行（status_line + duration_line） ----
        meta_element = _build_engine_meta(spec)
        has_engine_meta = meta_element is not None
        if meta_element:
            elements.append(meta_element)

        # ---- 2. 置顶消息 / 警告横幅 ----
        if spec.sticky_message:
            elements.append(
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": f"⚠️ {spec.sticky_message}"}],
                }
            )

        if spec.warning_banner:
            from .core import CoreBuilder
            elements.append(CoreBuilder._build_banner_element(spec.warning_banner, type="warning"))

        # ---- 3. 分隔线 ----
        elements.append({"tag": "hr"})

        # ---- 4. 进度条（引擎特有） ----
        if spec.progress_bar:
            # 如果主内容里不包含进度条，才独立显示
            content_str = spec.content_markdown or ""
            if spec.progress_bar not in content_str:
                elements.append({"tag": "markdown", "content": f"📊 {spec.progress_bar}"})

        # ---- 4b. 引擎元数据后的分隔线 ----
        if has_engine_meta:
            elements.append({"tag": "hr"})

        # ---- 5. 图片 ----
        if spec.image_keys:
            for i, key in enumerate(spec.image_keys):
                elements.append(
                    {
                        "tag": "img",
                        "img_key": key,
                        "alt": {"tag": "plain_text", "content": UI_TEXT["image_alt_text"].format(index=i + 1)},
                    }
                )
            elements.append({"tag": "hr"})

        # ---- 6. 主内容 ----
        if spec.content_elements:
            # 结构化内容（包含 collapsible_panel 等），仅非空时使用
            elements.extend(spec.content_elements)
        elif spec.content_markdown is not None:
            md_element: dict = {"tag": "markdown", "content": spec.content_markdown}
            if not spec.legacy_safe:
                md_element["element_id"] = spec.content_element_id
                md_element["text_size"] = "normal"
            elements.append(md_element)

        # ---- 7+8. 验收标准区域（引擎特有） ----
        if spec.criteria_section:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": spec.criteria_section})

        # ---- 9. 脚注（引擎特有） ----
        if spec.footer_note:
            elements.append(
                {
                    "tag": "markdown",
                    "content": spec.footer_note,
                    "text_size": "notation",
                }
            )

        # ---- 10+11. 按钮 ----
        if spec.footer_status:
            elements.extend(_build_footer_element(spec.footer_status))

        if spec.button_elements:
            # 引擎模式：预构建的按钮 elements（含 grouped layout 等）
            elements.append({"tag": "hr"})
            elements.extend(spec.button_elements)
        elif spec.buttons:
            button_elements = build_responsive_layout(spec.buttons)
            if button_elements:
                elements.append({"tag": "hr"})
                elements.extend(button_elements)

        # ---- 12. 终态标记 (Task 9) ----
        if spec.terminal_state:
            marker_text = TERMINAL_MARKERS.get(spec.terminal_state)
            if marker_text:
                elements.append({"tag": "markdown", "content": marker_text})

        return elements


# ---- 内部辅助 ----


def _build_footer_element(footer_status: Union[FooterStatusKey, str]) -> list[dict]:
    """构建 footer 状态行元素（hr + notation markdown）。

    ``footer_status`` may be a :class:`FooterStatusKey` (translated via
    ``FOOTER_STATUS`` dict) or a pre-formatted display string (used as-is).
    """
    footer_text = FOOTER_STATUS.get(footer_status, footer_status)
    return [
        {"tag": "hr"},
        {"tag": "markdown", "content": footer_text, "text_size": "notation"},
    ]

def _build_path_and_meta(spec: CardLayoutSpec) -> dict:
    """构建路径 + 状态/元数据行。

    流式模式：📁 path + 状态栏（🔵 状态: BLUE | 错误 | 进度）
    引擎模式：📁 path（无状态栏，元数据在分隔线后单独显示）
    """
    path_display = spec.project_path or "~"
    has_engine_meta = spec.status_line or spec.duration_line

    if has_engine_meta:
        # 引擎模式：路径单独一行，元数据由 build() 主流程通过 _build_engine_meta_elements 处理
        return {"tag": "markdown", "content": f"📁 `{path_display}`"}

    # 流式模式：路径 + 状态栏合并
    status_icon = {"green": "🟢", "red": "🔴", "blue": "🔵", "grey": "⚪"}.get(spec.status_color, "🔵")
    status_info = f"{status_icon} **状态**: {spec.status_color.upper()}"
    if spec.error_count > 0:
        status_info += f" | ❌ 错误: {spec.error_count}"
    if spec.progress_text:
        status_info += f" | {spec.progress_text}"

    md_element: dict = {"tag": "markdown", "content": f"📁 `{path_display}`\n{status_info}"}
    if not spec.legacy_safe:
        md_element["element_id"] = "path_md"
        md_element["text_size"] = "notation"
    return md_element


def _build_engine_meta(spec: CardLayoutSpec) -> dict | None:
    """构建引擎元数据行（status_line + duration_line）。

    返回 None 时表示无元数据，调用方应跳过。
    """
    meta_items: list[str] = []
    if spec.status_line:
        # 如果状态行已经包含分隔符，拆分为独立项
        if " · " in spec.status_line:
            meta_items.extend([s.strip() for s in spec.status_line.split(" · ") if s.strip()])
        else:
            meta_items.append(spec.status_line.strip())
    if spec.duration_line:
        meta_items.append(spec.duration_line.strip())

    if not meta_items:
        return None

    # 策略：元数据项过多时强制换行，避免移动端截断
    separator = spec.engine_meta_separator
    if len(meta_items) > 3:
        separator = "\n"

    return {
        "tag": "markdown",
        "content": separator.join(meta_items),
        "text_size": "notation",
    }
