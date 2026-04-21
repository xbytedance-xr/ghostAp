import json
from typing import Optional

from src.project.context import ProjectContext

from ..models import EngineCardState
from ..shared import (
    apply_compact_style,
    build_responsive_layout,
    get_theme,
)
from ..styles import BUTTON_CONFIG, ENGINE_STYLES, THRESHOLDS, UI_TEXT
from .core import CoreBuilder


class DeepBuilder:
    """Deep engine card building utilities."""

    @staticmethod
    def _resolve_style(engine_name: str) -> dict:
        name_lower = (engine_name or "").strip().lower()
        for key, style in ENGINE_STYLES.items():
            if key != "default" and name_lower.startswith(key):
                return style
        return ENGINE_STYLES["default"]

    @staticmethod
    def _build_deep_header_title(
        project: Optional[ProjectContext],
        engine_name: str = "Coco",
    ) -> str:
        style = DeepBuilder._resolve_style(engine_name)
        icon = style["icon"]

        if "label_static" in style:
            label = style["label_static"]
        else:
            label = style["label_format"].format(name=engine_name)

        if project:
            return f"{icon} {project.project_name} · {label}"
        return f"{icon} {label}"

    @staticmethod
    def _pick_deep_template(engine_name: str, status: str = "running") -> str:
        """Pick header template color based on engine and status."""
        status = status.lower()
        STATUS_COLORS = {
            "error": "red",
            "completed": "green",
            "paused": "orange",
            "planning": "blue",
        }
        if status in STATUS_COLORS:
            return STATUS_COLORS[status]

        style = DeepBuilder._resolve_style(engine_name)
        return style["color"]

    @staticmethod
    def _create_button(action_key: str, state: EngineCardState, action_suffix: str = None) -> dict:
        """Helper to create a button based on config and state."""
        config = BUTTON_CONFIG.get(action_key)
        if not config:
            return {}

        # Determine specific action string (e.g. "loop_pause")
        if action_suffix:
            action_name = f"{state.action_prefix}_{action_suffix}"
        else:
            action_name = f"{state.action_prefix}_{action_key}"

        # Special handling for "history" which is just "loop_history" currently but let's keep it generic
        if action_key == "history" and state.action_prefix == "loop":
            action_name = "loop_history"

        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": config["text"]},
            "type": config["type"],
            "value": {
                "action": action_name,
                "project_id": state.project_id or state.engine_project_id,
                "deep_project_id": state.engine_project_id,
            },
        }

    @staticmethod
    def _build_deep_buttons(state: EngineCardState) -> list[dict]:
        """Build a flat list of deep engine buttons (for backward compatibility with tests)."""
        buttons = []

        # Control buttons
        if state.is_executing:
            buttons.append(DeepBuilder._create_button("pause", state))
            buttons.append(DeepBuilder._create_button("stop", state))
        elif state.is_paused:
            buttons.append(DeepBuilder._create_button("resume", state))
            buttons.append(DeepBuilder._create_button("stop", state))

        # View buttons
        lines = (state.content or "").split("\n")
        threshold = THRESHOLDS["COMPACT_LINE_THRESHOLD"] if state.compact else THRESHOLDS["FULL_LINE_THRESHOLD"]
        if len(lines) > threshold:
            action_key = "collapse" if state.expanded else "expand"
            buttons.append(DeepBuilder._create_button(action_key, state))

        # Mode switch
        mode_key = "mode_full" if state.compact else "mode_compact"
        buttons.append(DeepBuilder._create_button(mode_key, state))

        # Feature-specific buttons
        style = DeepBuilder._resolve_style(state.engine_name)
        features = style.get("features", {})
        if features.get("history_button"):
            buttons.append(DeepBuilder._create_button("history", state))

        # Extra buttons (custom actions like retry)
        if state.extra_buttons:
            for b in state.extra_buttons:
                if b:
                    buttons.append(b)

        return [apply_compact_style(b) for b in buttons if b]

    @staticmethod
    def _build_grouped_layout(state: EngineCardState) -> list[dict]:
        """Build layout with grouped control buttons (Pause/Stop side-by-side)."""
        control_buttons = []
        other_buttons = []
        style = DeepBuilder._resolve_style(state.engine_name)
        features = style.get("features", {})

        # 1. Collect Control Buttons (Pause/Resume, Stop)
        if state.is_executing:
            control_buttons.append(DeepBuilder._create_button("pause", state))
            control_buttons.append(DeepBuilder._create_button("stop", state))
        elif state.is_paused:
            control_buttons.append(DeepBuilder._create_button("resume", state))
            control_buttons.append(DeepBuilder._create_button("stop", state))

        # 2. Collect View/Mode Buttons
        # Only show these auxiliary buttons if we don't have high-priority extra buttons (like Retry)
        # This keeps the user focused on the recovery action when an error occurs
        if not state.extra_buttons:
            # Log Expand/Collapse
            lines = (state.content or "").split("\n")
            threshold = THRESHOLDS["COMPACT_LINE_THRESHOLD"] if state.compact else THRESHOLDS["FULL_LINE_THRESHOLD"]
            if len(lines) > threshold:
                action_key = "collapse" if state.expanded else "expand"
                other_buttons.append(DeepBuilder._create_button(action_key, state))

            # AC Expand/Collapse
            if state.criteria_section:
                ac_lines = state.criteria_section.split("\n")
                if len(ac_lines) > THRESHOLDS["AC_LINE_THRESHOLD"]:
                    ac_action_key = "collapse_ac" if state.expand_ac else "expand_ac"
                    other_buttons.append(DeepBuilder._create_button(ac_action_key, state))

            # Mode Switch
            mode_key = "mode_full" if state.compact else "mode_compact"
            other_buttons.append(DeepBuilder._create_button(mode_key, state))

        # Feature-specific Buttons (History)
        if features.get("history_button"):
            other_buttons.append(DeepBuilder._create_button("history", state))

        # Extra buttons (custom actions like retry) - priority high
        if state.extra_buttons:
            for b in reversed(state.extra_buttons): # Insert at beginning of other_buttons
                if b:
                    other_buttons.insert(0, b)

        # 3. Build Layout Elements
        elements = []

        # Apply styling
        control_buttons = [apply_compact_style(b) for b in control_buttons if b]
        other_buttons = [apply_compact_style(b) for b in other_buttons if b]

        # Group controls in a specific column set (force side-by-side)
        if control_buttons:
            cols = []
            for btn in control_buttons:
                cols.append({"tag": "column", "width": "weighted", "weight": 1, "elements": [btn]})
            elements.append(
                {"tag": "column_set", "flex_mode": "stretch", "background_style": "default", "columns": cols}
            )

        # Group others using standard responsive layout
        if other_buttons:
            elements.extend(build_responsive_layout(other_buttons))

        return elements

    @staticmethod
    def build_engine_card(
        project: Optional[ProjectContext],
        state: EngineCardState,
    ) -> tuple[str, str]:
        # Determine status for color mapping
        status_key = "running"
        title_lower = state.title.lower()
        if "error" in title_lower or UI_TEXT["deep_status_failed_zh"] in state.title:
            status_key = "error"
        elif (
            UI_TEXT["deep_status_completed_zh"] in state.title
            or UI_TEXT["deep_status_finished_zh"] in state.title
            or "completed" in title_lower
            or "finished" in title_lower
            or "success" in title_lower
        ):
            status_key = "completed"
        elif state.is_paused:
            status_key = "paused"
        elif (
            UI_TEXT["deep_status_planning_zh"] in state.title
            or UI_TEXT["deep_status_analyzing_zh"] in state.title
            or "planning" in title_lower
            or "analyzing" in title_lower
        ):
            status_key = "planning"

        header_template = DeepBuilder._pick_deep_template(state.engine_name, status_key)
        theme = get_theme(header_template)

        # Optimize Title with Icons based on status if not already present
        if not state.title:
            header_title = DeepBuilder._build_deep_header_title(project, state.engine_name)
        else:
            # Force loop emoji if loop engine
            style = DeepBuilder._resolve_style(state.engine_name)
            icon = style["icon"]
            if icon not in state.title:
                header_title = f"{icon} {state.title}"
            else:
                header_title = state.title

        elements = [
            CoreBuilder._build_directory_element(project, state.working_dir),
            {"tag": "hr"},
        ]

        # Progress bar
        if state.progress_bar and (not state.content or state.progress_bar not in state.content):
            elements.append({"tag": "markdown", "content": f"📊 {state.progress_bar}"})

        # Status + duration line (compact, notation-size)
        # 智能拼接元数据，针对 Spec Engine 等多维度状态场景优化显示，确保移动端可见性
        meta_items = []
        if state.status_line:
            # 如果状态行已经包含分隔符，拆分为独立项以便后续智能换行
            if " · " in state.status_line:
                meta_items.extend([s.strip() for s in state.status_line.split(" · ") if s.strip()])
            else:
                meta_items.append(state.status_line.strip())

        if state.duration_line:
            meta_items.append(state.duration_line.strip())

        if meta_items:
            style = DeepBuilder._resolve_style(state.engine_name)
            separator = style.get("meta_separator", " · ")

            # 策略：如果元数据项过多（例如增加了规格单元进度），则强制换行展示，避免在移动端被截断或重叠
            if len(meta_items) > 3:
                separator = "\n"

            elements.append(
                {
                    "tag": "markdown",
                    "content": separator.join(meta_items),
                    "text_size": "notation",
                }
            )

        # Separator before main content (only if we have meta above)
        if meta_items:
            elements.append({"tag": "hr"})

        # Warning banner
        if state.warning_banner:
            elements.append(CoreBuilder._build_banner_element(state.warning_banner, type="warning"))

        # Main content processing
        display_content = state.content if isinstance(state.content, str) else str(state.content or "")

        if state.expanded:
            # If expanded, show full content regardless of mode
            pass
        elif state.compact:
            # Error check - show more context for errors
            is_error = status_key == "error"

            if is_error:
                if not display_content:
                    display_content = UI_TEXT["deep_error_no_detail"]
                else:
                    lines = display_content.split("\n")
                    # Show first N lines for errors
                    if len(lines) > THRESHOLDS["COMPACT_LINE_THRESHOLD"]:
                        display_content = "\n".join(lines[:THRESHOLDS["COMPACT_LINE_THRESHOLD"]]) + UI_TEXT["deep_error_expand_hint"]
            else:
                # Compact mode: show last N lines for running/paused to avoid scroll trap
                if not display_content:
                    display_content = UI_TEXT["deep_executing_placeholder"]
                else:
                    lines = display_content.split("\n")
                    if len(lines) > THRESHOLDS["COMPACT_LINE_THRESHOLD"]:
                        display_content = "...\n" + "\n".join(lines[-THRESHOLDS["COMPACT_LINE_THRESHOLD"]:]) + UI_TEXT["deep_content_expand_hint"]
                    elif len(display_content) > THRESHOLDS["COMPACT_CHAR_FALLBACK"]:
                        # Fallback for very long lines
                        display_content = "..." + display_content[-THRESHOLDS["COMPACT_CHAR_FALLBACK"]:]
        else:
            # Full mode: Line-based truncation if not expanded
            if display_content:
                lines = display_content.split("\n")
                if len(lines) > THRESHOLDS["FULL_LINE_THRESHOLD"]:
                    display_content = UI_TEXT["deep_folded_lines_hint"].format(count=len(lines) - THRESHOLDS["FULL_LINE_THRESHOLD"]) + "\n".join(
                        lines[-THRESHOLDS["FULL_LINE_THRESHOLD"]:]
                    )

        elements.append(CoreBuilder._build_content_element(display_content))

        # Criteria section (independent element) - Skip in compact mode unless very short
        if state.criteria_section and not state.compact:
            elements.append({"tag": "hr"})

            # Smart truncation for Criteria Section
            display_ac = state.criteria_section
            if not state.expand_ac:
                ac_lines = display_ac.split("\n")
                if len(ac_lines) > THRESHOLDS["AC_LINE_THRESHOLD"]:
                    display_ac = "\n".join(ac_lines[:THRESHOLDS["AC_LINE_THRESHOLD"]]) + UI_TEXT["deep_ac_expand_hint"]

            elements.append({"tag": "markdown", "content": display_ac})

        # Footer note
        if state.footer_note:
            elements.append(
                {
                    "tag": "markdown",
                    "content": state.footer_note,
                    "text_size": "notation",
                }
            )

        if state.show_buttons:
            if state.is_executing or state.is_paused:
                # New grouped layout for running states
                layout_elements = DeepBuilder._build_grouped_layout(state)
                if layout_elements:
                    elements.append({"tag": "hr"})
                    elements.extend(layout_elements)
            else:
                buttons = []
                # Finished or not started, still show mode switch
                from src.mode.manager import InteractionMode
                base_buttons = CoreBuilder._build_footer_buttons(project, mode=InteractionMode.SMART)

                # Custom extra buttons (e.g. retry/recover) should be first
                if state.extra_buttons:
                    for b in state.extra_buttons:
                        if b:
                            buttons.append(apply_compact_style(b))

                # If we have extra buttons (like Retry), hide the noise like expand/collapse and mode switch
                if not state.extra_buttons:
                    # Also add expand/collapse if there is enough content
                    lines = (state.content or "").split("\n")
                    threshold = THRESHOLDS["COMPACT_LINE_THRESHOLD"] if state.compact else THRESHOLDS["FULL_LINE_THRESHOLD"]
                    if len(lines) > threshold:
                        action_key = "collapse" if state.expanded else "expand"
                        expand_btn = apply_compact_style(DeepBuilder._create_button(action_key, state))
                        buttons.append(expand_btn)

                    # Add mode switch button
                    mode_key = "mode_full" if state.compact else "mode_compact"
                    mode_btn = apply_compact_style(DeepBuilder._create_button(mode_key, state))
                    if mode_btn:
                        buttons.append(mode_btn)

                buttons.extend(base_buttons)

                if buttons:
                    elements.append({"tag": "hr"})
                    elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    build_deep_card = build_engine_card

    @staticmethod
    def build_history_list_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        history_buttons: list[dict],
        page: int,
        has_next: bool,
        deep_project_id: Optional[str] = None,
        engine_name: str = "Coco",
    ) -> tuple[str, str]:
        """Build a history list card with pagination."""
        header_template = DeepBuilder._pick_deep_template(engine_name, "running")
        theme = get_theme(header_template)

        # Consistent title using helper (or similar logic)
        header_title = f"📜 {project.project_name if project else 'Loop'}{UI_TEXT['system_history_record_title']}"

        elements = [
            {"tag": "markdown", "content": f"**{title}**\n\n{content}"},
            {"tag": "hr"},
        ]

        # History Items (as buttons grid)
        elements.extend(build_responsive_layout(history_buttons))

        # Pagination Controls
        nav_buttons = []
        if page > 1:
            nav_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": UI_TEXT["system_btn_prev_page"]},
                    "type": "default",
                    "value": {
                        "action": "loop_history_page",
                        "page": page - 1,
                        "project_id": project.project_id if project else None,
                        "deep_project_id": deep_project_id,
                    },
                }
            )

        if has_next:
            nav_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": UI_TEXT["system_btn_next_page"]},
                    "type": "default",
                    "value": {
                        "action": "loop_history_page",
                        "page": page + 1,
                        "project_id": project.project_id if project else None,
                        "deep_project_id": deep_project_id,
                    },
                }
            )

        # Back to Status
        nav_buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": UI_TEXT["system_btn_back_status"]},
                "type": "primary",
                "value": {
                    "action": "loop_back_to_list",  # Reusing generic back action name or specific
                    "project_id": project.project_id if project else None,
                    "deep_project_id": deep_project_id,
                },
            }
        )

        if nav_buttons:
            elements.append({"tag": "hr"})
            elements.extend(build_responsive_layout([apply_compact_style(b) for b in nav_buttons]))

        card = CoreBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
