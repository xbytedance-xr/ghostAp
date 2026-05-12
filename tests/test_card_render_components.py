"""Tests for header/footer/buttons rendering."""
import pytest
from src.card.state.models import CardState, HeaderState, FooterState, ButtonSpec, CardMetadata, TextBlock
from src.card.render.header import render_header
from src.card.render.footer import render_footer
from src.card.render.buttons import render_buttons


class TestRenderHeader:
    def test_header_with_project(self):
        """Project name present → "emoji ProjectName · ModeName" """
        state = CardState(
            header=HeaderState(title="🧠 MyProject · Deep Agent", template="turquoise"),
            metadata=CardMetadata(project_name="MyProject", mode_name="Deep Agent", mode_emoji="🧠"),
        )
        result = render_header(state)
        assert result["title"]["content"] == "🧠 MyProject · Deep Agent"
        assert result["template"] == "turquoise"

    def test_header_without_project(self):
        """No project → "emoji ModeName 编程模式" """
        state = CardState(
            header=HeaderState(title="🤖 Coco 编程模式", template="blue"),
            metadata=CardMetadata(mode_name="Coco", mode_emoji="🤖"),
        )
        result = render_header(state)
        assert result["title"]["content"] == "🤖 Coco 编程模式"

    def test_header_subtitle_with_tool_and_model(self):
        """Both tool and model → v2 header puts them on the first row."""
        state = CardState(
            header=HeaderState(title="test", subtitle="🔧 coco · gpt-4o"),
            metadata=CardMetadata(tool_name="coco", model_name="gpt-4o"),
        )
        result = render_header(state)
        assert result["title"]["content"] == "📁 test · 🤖 Coco · #1 · gpt-4o"
        assert "subtitle" not in result

    def test_header_subtitle_with_status(self):
        """Subtitle with status → "🔧 tool · model · status" """
        state = CardState(
            header=HeaderState(title="test", subtitle="🔧 coco · gpt-4o · 正在执行"),
        )
        result = render_header(state)
        assert result["subtitle"]["content"] == "🔧 coco · gpt-4o · 正在执行"

    def test_header_no_subtitle(self):
        """No subtitle → no subtitle key in result"""
        state = CardState(header=HeaderState(title="test", subtitle=None))
        result = render_header(state)
        assert "subtitle" not in result

    def test_header_template_running(self):
        """Running state uses mode color"""
        state = CardState(header=HeaderState(title="test", template="purple"))
        result = render_header(state)
        assert result["template"] == "purple"


class TestRenderFooter:
    def test_footer_thinking(self):
        """status=thinking → 💭 text"""
        state = CardState(footer=FooterState(status="thinking", status_text="💭 正在思考..."))
        result = render_footer(state)
        assert len(result) == 2  # hr + markdown
        assert result[0]["tag"] == "hr"
        assert result[1]["content"] == "💭 正在思考..."
        assert result[1]["text_size"] == "notation"

    def test_footer_tool_running(self):
        """status=tool_running → 🔧 text"""
        state = CardState(footer=FooterState(status="tool_running", status_text="🔧 执行中: bash"))
        result = render_footer(state)
        assert result[1]["content"] == "🔧 执行中: bash"

    def test_footer_with_progress(self):
        """Progress merged with status into one line"""
        state = CardState(footer=FooterState(
            status="tool_running",
            status_text="🔧 执行中: bash",
            progress="▰▰▰▱▱▱▱▱▱▱ 30%"
        ))
        result = render_footer(state)
        assert len(result) == 2  # hr + merged status+progress
        assert "🔧 执行中: bash" in result[1]["content"]
        assert "▰▰▰▱▱▱▱▱▱▱ 30%" in result[1]["content"]

    def test_footer_none(self):
        """status=None → empty list"""
        state = CardState(footer=FooterState(status=None))
        result = render_footer(state)
        assert result == []


class TestRenderButtons:
    def test_no_buttons(self):
        """No buttons → empty list"""
        state = CardState(buttons=())
        result = render_buttons(state)
        assert result == []

    def test_single_button_action_block(self):
        """1 button → column_set with flex_mode 'none' (full width for mobile accessibility)"""
        state = CardState(buttons=(
            ButtonSpec(text="停止", action_id="stop", type="danger"),
        ))
        result = render_buttons(state)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert result[0]["flex_mode"] == "none"
        assert len(result[0]["columns"]) == 1
        assert result[0]["columns"][0]["width"] == "weighted"
        assert result[0]["columns"][0]["weight"] == 1
        assert result[0]["columns"][0]["elements"][0]["text"]["content"] == "停止"
        button = result[0]["columns"][0]["elements"][0]
        assert button["behaviors"] == [
            {"type": "callback", "value": button["value"]}
        ]

    def test_two_buttons_column_set(self):
        """2 buttons → column_set layout with bisect"""
        state = CardState(buttons=(
            ButtonSpec(text="停止", action_id="stop", type="danger"),
            ButtonSpec(text="继续", action_id="continue", type="primary"),
        ))
        result = render_buttons(state)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert len(result[0]["columns"]) == 2
        assert result[0]["flex_mode"] == "bisect"

    def test_many_buttons_schema_v2_layout(self):
        """3+ buttons avoid the Schema V2-incompatible action container."""
        from src.card.render.budget import RenderBudget
        budget = RenderBudget(mobile_force_vertical=True)
        # 3 buttons with mobile_force_vertical=True → vertical column_set
        state = CardState(buttons=(
            ButtonSpec(text="A", action_id="a"),
            ButtonSpec(text="B", action_id="b"),
            ButtonSpec(text="C", action_id="c"),
        ))
        result = render_buttons(state, budget=budget)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert result[0]["flex_mode"] == "none"
        assert len(result[0]["columns"]) == 1
        assert len(result[0]["columns"][0]["elements"]) == 3

        # 4 buttons → two Schema V2-compatible rows
        state4 = CardState(buttons=(
            ButtonSpec(text="A", action_id="a"),
            ButtonSpec(text="B", action_id="b"),
            ButtonSpec(text="C", action_id="c"),
            ButtonSpec(text="D", action_id="d"),
        ))
        result4 = render_buttons(state4)
        assert len(result4) == 2
        assert all(row["tag"] == "column_set" for row in result4)
        assert all(row["flex_mode"] == "bisect" for row in result4)
        assert sum(len(row["columns"]) for row in result4) == 4

    def test_button_with_confirm(self):
        """Button with confirm → confirm dialog"""
        state = CardState(buttons=(
            ButtonSpec(text="删除", action_id="delete", type="danger", confirm="确定要删除吗？"),
            ButtonSpec(text="取消", action_id="cancel"),
        ))
        result = render_buttons(state)
        # Find the delete button
        columns = result[0]["columns"]
        delete_btn = columns[0]["elements"][0]
        assert "confirm" in delete_btn
        assert delete_btn["confirm"]["text"]["content"] == "确定要删除吗？"


# ---------------------------------------------------------------------------
# Phase 5: render_progress_bar boundary tests
# ---------------------------------------------------------------------------
from src.card.render.progress import render_progress_bar


class TestRenderProgressBarBoundary:
    """Boundary value tests for render_progress_bar."""

    def test_pct_zero(self):
        result = render_progress_bar(0)
        assert "▱▱▱▱▱" in result
        assert "▰" not in result

    def test_pct_hundred(self):
        result = render_progress_bar(100)
        assert "▰▰▰▰▰" in result
        assert "▱" not in result

    def test_pct_over_hundred_clamps(self):
        result = render_progress_bar(150)
        # Should clamp to 100%
        assert "▰▰▰▰▰" in result

    def test_pct_negative_clamps(self):
        result = render_progress_bar(-10)
        # Should clamp to 0%
        assert "▱▱▱▱▱" in result

    def test_pct_midpoint(self):
        result = render_progress_bar(50)
        assert "▰" in result
        assert "▱" in result

    def test_pct_one_shows_at_least_one_filled(self):
        """AC13: pct>0 must show at least 1 filled block."""
        result = render_progress_bar(1)
        assert "▰" in result

    def test_percentage_in_output(self):
        """Progress bar should include percentage number."""
        result = render_progress_bar(50)
        assert "50%" in result

    def test_total_segments_zero_returns_empty(self):
        """total_segments=0 should return empty string without raising."""
        result = render_progress_bar(50, total_segments=0)
        assert result == ""

    def test_total_segments_negative_returns_empty(self):
        """total_segments<0 should return empty string without raising."""
        result = render_progress_bar(100, total_segments=-5)
        assert result == ""


# ---------------------------------------------------------------------------
# Phase 5: Footer warning_banner + progress_pct coexistence
# ---------------------------------------------------------------------------

class TestFooterWarningAndProgressCoexist:
    """Warning banner rendered in body top; footer only has status + progress."""

    def test_footer_has_no_banner_warning_type(self):
        """Banner is now in body top, not footer — footer should only have status+progress."""
        state = CardState(
            footer=FooterState(
                status="tool_running",
                status_text="⏳ 编码中",
                progress="步骤 3/6",
                progress_pct=50,
                warning_banner="注意：资源即将耗尽",
                warning_type="warning",
            ),
        )
        elements = render_footer(state)
        # Footer should NOT contain any banner div (moved to body top)
        banner_divs = [e for e in elements if e.get("tag") == "div"]
        assert len(banner_divs) == 0
        # Should still have hr + status + progress
        all_content = str(elements)
        assert "▰" in all_content
        assert "步骤 3/6" in all_content

    def test_warning_only_footer_shows_status(self):
        state = CardState(
            footer=FooterState(
                status="idle",
                warning_banner="仅警告",
                warning_type="warning",
            ),
        )
        elements = render_footer(state)
        # Footer should have hr + status only (no banner)
        assert len(elements) >= 1
        banner_divs = [e for e in elements if e.get("tag") == "div"]
        assert len(banner_divs) == 0


# ---------------------------------------------------------------------------
# Banner unified position tests (all levels in body top)
# ---------------------------------------------------------------------------
from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card

class TestBannerUnifiedPosition:
    """All banner types (error/warning/info/success) render at body_elements[0]."""

    def _render_with_banner(self, warning_type: str):
        state = CardState(
            blocks=(TextBlock(kind="text", block_id="b1", content="hello", status="completed"),),
            footer=FooterState(
                status="idle",
                status_text="ready",
                warning_banner="Test banner message",
                warning_type=warning_type,
            ),
        )
        cards = render_card(state, RenderBudget())
        return cards[0]._card_json["body"]["elements"]

    def test_error_banner_in_body_top(self):
        elements = self._render_with_banner("error")
        assert elements[0]["tag"] == "column_set"
        assert elements[0]["background_style"] == "red"
        assert "Test banner message" in elements[0]["columns"][0]["elements"][0]["content"]

    def test_warning_banner_in_body_top(self):
        elements = self._render_with_banner("warning")
        assert elements[0]["tag"] == "column_set"
        assert elements[0]["background_style"] == "yellow"
        assert "Test banner message" in elements[0]["columns"][0]["elements"][0]["content"]

    def test_info_banner_in_body_top(self):
        elements = self._render_with_banner("info")
        assert elements[0]["tag"] == "column_set"
        assert elements[0]["background_style"] == "wathet"
        assert "Test banner message" in elements[0]["columns"][0]["elements"][0]["content"]

    def test_success_banner_in_body_top(self):
        elements = self._render_with_banner("success")
        assert elements[0]["tag"] == "column_set"
        assert elements[0]["background_style"] == "green"
        assert "Test banner message" in elements[0]["columns"][0]["elements"][0]["content"]


# ---------------------------------------------------------------------------
# Phase 6: Worktree render functions unit tests
# ---------------------------------------------------------------------------

from src.card.render.worktree import (
    render_worktree_panel,
    _render_worktree_tool_select,
    _render_worktree_confirm,
    _render_worktree_units,
    _render_worktree_merge,
    _render_worktree_cleanup,
)
from src.card.state.models import ContentBlock
import json


class TestRenderWorktreeToolSelect:
    """Tests for _render_worktree_tool_select."""

    def _collect_buttons(self, node):
        if isinstance(node, dict):
            found = [node] if node.get("tag") == "button" else []
            for value in node.values():
                found.extend(self._collect_buttons(value))
            return found
        if isinstance(node, list):
            found = []
            for item in node:
                found.extend(self._collect_buttons(item))
            return found
        return []

    def test_normal_render(self):
        data = {
            "project_id": "p1",
            "tools": [{"id": "coco", "name": "Coco", "description": "AI assistant"}],
            "selected": [
                {
                    "selection_key": "acp:coco:default",
                    "display_label": "Coco / 默认模型",
                    "tool_name": "coco",
                }
            ],
            "message": "Choose tools:",
        }
        result = _render_worktree_tool_select(data)
        assert result["tag"] == "column_set"
        buttons = self._collect_buttons(result)
        # 第一个按钮：工具行的 "+ 添加 Coco"
        assert buttons[0]["text"]["content"] == "+ 添加 Coco"
        assert buttons[0]["value"]["action"] == "worktree_select_tool"
        assert buttons[0]["value"]["tool_name"] == "coco"
        assert buttons[0]["value"]["project_id"] == "p1"
        # 已选组合的 ✕ 移除按钮
        remove_btns = [b for b in buttons if b["value"].get("action") == "worktree_remove_item"]
        assert remove_btns
        assert remove_btns[0]["value"]["selection_key"] == "acp:coco:default"
        # 清空 + 确认按钮
        actions = [b["value"].get("action") for b in buttons]
        assert "worktree_clear_items" in actions
        assert "worktree_finish_selection" in actions
        # 确认按钮在 N>0 时为 primary
        confirm_btns = [b for b in buttons if b["value"].get("action") == "worktree_finish_selection"]
        assert confirm_btns[0]["type"] == "primary"

    def test_empty_tools(self):
        data = {"project_id": "p1", "tools": [], "selected": [], "message": ""}
        result = _render_worktree_tool_select(data)
        assert result["tag"] == "column_set"
        buttons = self._collect_buttons(result)
        # 空 tools + 空 selected：不渲染可点击确认按钮，避免用户触发无效确认错误
        actions = [b["value"].get("action") for b in buttons]
        assert "worktree_finish_selection" not in actions
        assert "worktree_remove_item" not in actions
        assert "worktree_clear_items" not in actions

    def test_unselected_tool(self):
        data = {
            "project_id": "p1",
            "tools": [{"id": "claude", "name": "Claude"}],
            "selected": [],
            "message": "",
        }
        result = _render_worktree_tool_select(data)
        buttons = self._collect_buttons(result)
        # 工具按钮："+ 添加 Claude"，type=default（中性，不再以高亮反映已选）
        tool_btns = [b for b in buttons if b["value"].get("action") == "worktree_select_tool"]
        assert tool_btns[0]["text"]["content"] == "+ 添加 Claude"
        assert tool_btns[0]["type"] == "default"

    def test_model_select_stage_skips_selected_block(self):
        """MODEL_SELECT 阶段不应渲染已选组合/清空/确认按钮，由用户选完模型后回到 TOOL_SELECT。"""
        data = {
            "project_id": "p1",
            "tools": [{"id": "gpt-4", "name": "GPT-4"}],
            "selected": [{"selection_key": "acp:coco:default", "display_label": "Coco"}],
            "message": "选模型",
            "select_action": "worktree_select_model",
        }
        result = _render_worktree_tool_select(data)
        buttons = self._collect_buttons(result)
        actions = [b["value"].get("action") for b in buttons]
        assert "worktree_finish_selection" not in actions
        assert "worktree_remove_item" not in actions
        assert "worktree_clear_items" not in actions
        assert "worktree_select_model" in actions


class TestRenderWorktreeConfirm:
    """Tests for _render_worktree_confirm."""

    def test_normal_render(self):
        data = {
            "selected_items": [{"tool": "Coco", "model": "gpt-4"}],
            "goal": "Build feature X",
            "message": "Please confirm",
        }
        result = _render_worktree_confirm(data)
        assert result["tag"] == "markdown"
        assert "Coco" in result["content"]
        assert "gpt-4" in result["content"]
        assert "Build feature X" in result["content"]

    def test_empty_items(self):
        data = {"selected_items": [], "goal": "", "message": ""}
        result = _render_worktree_confirm(data)
        assert "已选组合" in result["content"]

    def test_renders_agent_tool_model_tuple_from_selection_item_dicts(self):
        data = {
            "selected_items": [
                {
                    "agent_display_name": "",
                    "display_name": "Coco",
                    "effective_model_display_name": "Doubao Pro",
                    "display_label": "Coco / Doubao Pro",
                },
                {
                    "agent_display_name": "",
                    "display_name": "Claude",
                    "effective_model_display_name": "默认模型",
                    "display_label": "Claude / 默认模型",
                },
                {
                    "agent_display_name": "TTADK",
                    "display_name": "Codex",
                    "effective_model_display_name": "GPT-5.2",
                    "display_label": "TTADK · Codex / GPT-5.2",
                },
            ],
            "goal": "",
            "message": "",
        }

        result = _render_worktree_confirm(data)

        assert "Coco" in result["content"]
        assert "Doubao Pro" in result["content"]
        assert "Claude" in result["content"]
        assert "默认模型" in result["content"]
        assert "TTADK" in result["content"]
        assert "Codex" in result["content"]
        assert "GPT-5.2" in result["content"]


class TestRenderWorktreeUnits:
    """Tests for _render_worktree_units."""

    def _get_content(self, result: dict) -> str:
        """Extract markdown content from collapsible_panel, div-wrapped, or plain markdown result."""
        parts = []
        self._collect_markdown(result, parts)
        return "\n".join(parts)

    def _collect_markdown(self, node: dict, parts: list[str]) -> None:
        """Recursively collect all markdown content from a Feishu card element tree."""
        if node.get("tag") == "markdown":
            parts.append(node.get("content", ""))
            return
        # Recurse into known container keys
        for key in ("elements", "columns"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self._collect_markdown(child, parts)

    def test_normal_render(self):
        data = {
            "units": [
                {"name": "unit-1", "status": "completed", "summary": "Done"},
                {"name": "unit-2", "status": "running"},
            ],
            "message": "",
        }
        result = _render_worktree_units(data)
        assert result["tag"] == "collapsible_panel"
        assert result["border"] == {"color": "wathet", "corner_radius": "8px"}
        assert result["vertical_spacing"] == "8px"
        assert result["padding"] == "8px 16px"
        assert result["header"]["title"]["tag"] == "markdown"
        content = self._get_content(result)
        assert "✅" in content
        assert "⏳" in content

    def test_empty_units(self):
        data = {"units": [], "message": ""}
        result = _render_worktree_units(data)
        content = self._get_content(result)
        assert content == "—"

    def test_failed_unit_with_error(self):
        data = {
            "units": [{"name": "u1", "status": "failed", "error": "timeout"}],
            "message": "",
        }
        result = _render_worktree_units(data)
        content = self._get_content(result)
        assert "❌" in content
        assert "timeout" in content


class TestRenderWorktreeMerge:
    """Tests for _render_worktree_merge."""

    def test_normal_render(self):
        data = {
            "merge_notes": [{"branch": "feat-1", "status": "ready", "summary": "ok"}],
            "base_branch": "main",
        }
        result = _render_worktree_merge(data)
        assert "feat-1" in result["content"]
        assert "main" in result["content"]
        assert "🟢" in result["content"]

    def test_empty_notes(self):
        data = {"merge_notes": [], "base_branch": "main"}
        result = _render_worktree_merge(data)
        assert "main" in result["content"]


class TestRenderWorktreeCleanup:
    """Tests for _render_worktree_cleanup."""

    def test_normal_render(self):
        data = {
            "merge_notes": [{"branch": "feat-1", "status": "merged"}],
            "base_branch": "develop",
            "merge_results": [{"branch": "feat-1", "success": True}],
        }
        result = _render_worktree_cleanup(data)
        assert "develop" in result["content"]
        assert "✅" in result["content"]

    def test_empty_notes(self):
        data = {"merge_notes": [], "base_branch": "main", "merge_results": None}
        result = _render_worktree_cleanup(data)
        assert "main" in result["content"]

    def test_failed_merge_result(self):
        data = {
            "merge_notes": [{"branch": "feat-x", "status": "conflict"}],
            "base_branch": "main",
            "merge_results": [{"branch": "feat-x", "success": False}],
        }
        result = _render_worktree_cleanup(data)
        assert "❌" in result["content"]


class TestRenderWorktreePanelDispatch:
    """Tests for render_worktree_panel routing and data failure fallback."""

    def test_json_parse_failure(self):
        """Block with no .data should return fallback message."""
        block = ContentBlock(kind="worktree_tool_select", block_id="wt1", content="not json")
        result = render_worktree_panel(block)
        assert "加载异常" in result["content"]

    def test_block_not_found_fallback(self):
        """Block with no data returns load failed message (new contract: caller passes block directly)."""
        block = ContentBlock(kind="worktree_tool_select", block_id="missing", content="raw")
        result = render_worktree_panel(block)
        # With new signature, a block without data returns load_failed message
        assert "加载异常" in result["content"]


# ---------------------------------------------------------------------------
# Task 5: Multi-page banner appears on ALL pages
# ---------------------------------------------------------------------------


class TestBannerMultiPagePosition:
    """Verify that warning banner appears only on the first page in multi-page cards."""

    def test_banner_on_first_page_only_with_large_content(self):
        """Construct state with enough content to trigger pagination + warning_banner."""
        # Generate many blocks to exceed single-page budget
        blocks = tuple(
            TextBlock(
                kind="text",
                block_id=f"b_{i}",
                content=f"{'x' * 500}\n" * 5,  # ~2500 chars per block
                status="completed",
            )
            for i in range(20)  # 20 blocks × 2500 chars = ~50000 chars → multi-page
        )
        state = CardState(
            blocks=blocks,
            footer=FooterState(
                status="idle",
                warning_banner="⚠️ 注意：系统负载较高",
                warning_type="warning",
            ),
        )
        cards = render_card(state, RenderBudget())

        # Should produce multiple pages
        assert len(cards) > 1, f"Expected multi-page but got {len(cards)} page(s)"

        # Verify banner appears only on the FIRST page
        first_body = cards[0]._card_json["body"]["elements"]
        first_elem = first_body[0]
        assert first_elem["tag"] == "column_set", (
            f"Page 0: first element should be banner column_set, got {first_elem.get('tag')}"
        )
        assert first_elem["background_style"] == "yellow", (
            "Page 0: warning banner should be yellow"
        )
        # Verify banner text
        banner_text = json.dumps(first_elem, ensure_ascii=False)
        assert "注意：系统负载较高" in banner_text, (
            "Page 0: banner text not found"
        )

        # Verify banner does NOT appear on subsequent pages
        for i, card in enumerate(cards[1:], start=1):
            body_elements = card._card_json["body"]["elements"]
            first_elem = body_elements[0]
            # Should be content (markdown), not a banner div with background_style
            assert first_elem.get("background_style") != "orange", (
                f"Page {i}: banner should NOT appear on non-first pages"
            )


# ---------------------------------------------------------------------------
# AC-2: render_buttons stop intent → flex_mode == "none"
# ---------------------------------------------------------------------------


class TestRenderButtonsStopIntent:
    """Verify that stop intent buttons produce flex_mode='none' (full-width layout)."""

    def test_stop_intent_flex_mode_none(self):
        """ButtonSpec with action_id='intent.engine.stop' → flex_mode='none'."""
        state = CardState(
            buttons=(ButtonSpec(action_id="intent.engine.stop", text="停止"),),
        )
        result = render_buttons(state)
        # 1 layout element (escalation hint is now managed by STOPPING reducer, not render_buttons)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert result[0]["flex_mode"] == "none"

    def test_deep_stop_intent_flex_mode_none(self):
        """ButtonSpec with action_id='intent.deep.stop' → flex_mode='none'."""
        state = CardState(
            buttons=(ButtonSpec(action_id="intent.deep.stop", text="停止 Deep"),),
        )
        result = render_buttons(state)
        assert result[0]["flex_mode"] == "none"

    def test_non_stop_single_button_flex_mode_none(self):
        """Non-stop single button → also flex_mode='none' (unified full-width for mobile)."""
        state = CardState(
            buttons=(ButtonSpec(action_id="some_other_action", text="提交"),),
        )
        result = render_buttons(state)
        assert result[0]["flex_mode"] == "none"


class TestToolStatusIcons:
    """Verify tool status icons use emoji style."""

    def test_status_icons_are_emoji(self):
        from src.card.render.tools import _STATUS_ICONS
        assert _STATUS_ICONS["completed"] == "✅"
        assert _STATUS_ICONS["failed"] == "❌"
        assert _STATUS_ICONS["active"] == "⏳"


class TestCriteriaPanelIcon:
    """Verify criteria panel has standard collapsible icon config."""

    def test_criteria_panel_has_icon_config(self):
        from src.card.render.renderer import _render_criteria_panel
        from src.card.render.atoms import RenderAtom
        from src.card.state.models import CardState, CardMetadata

        atom = RenderAtom(kind="criteria", content="- [x] Passes")
        state = CardState(metadata=CardMetadata(expand_ac=True))
        result = _render_criteria_panel(atom, state)

        header = result["header"]
        assert "icon" in header
        assert header["icon"]["token"] == "down-small-ccm_outlined"
        assert header["icon_position"] == "follow_text"
        assert header["icon_expanded_angle"] == -180


class TestWorktreeStepperTotal:
    """Verify stepper total is 4 (merge+cleanup merged into one step)."""

    def test_stepper_total_is_4(self):
        from src.card.render.worktree import _TOTAL_STEPS, _STEP_MAP
        assert _TOTAL_STEPS == 4
        # merge and cleanup share the same step index
        assert _STEP_MAP["worktree_merge"] == _STEP_MAP["worktree_cleanup"]

    def test_stepper_render_format_is_n_of_4(self):
        from src.card.render.worktree import _render_stepper
        elements = _render_stepper(0)
        # Concatenate all content and verify (n/4) format
        all_text = " ".join(e.get("content", "") for e in elements)
        assert "(1/4)" in all_text

    def test_stepper_last_step_is_4_of_4(self):
        from src.card.render.worktree import _render_stepper
        elements = _render_stepper(3)  # Last step (index 3)
        all_text = " ".join(e.get("content", "") for e in elements)
        assert "(4/4)" in all_text


class TestWorktreeMergeStatusChinese:
    """Verify merge status uses Chinese text, not English."""

    def test_merge_status_ready_chinese(self):
        """ready status should display 就绪."""
        data = {
            "merge_notes": [{"branch": "feat-1", "status": "ready", "summary": "ok"}],
            "base_branch": "main",
        }
        result = _render_worktree_merge(data)
        assert "就绪" in result["content"]
        assert "ready" not in result["content"].lower().replace("worktree", "")

    def test_merge_status_conflict_chinese(self):
        """conflict status should display 冲突."""
        data = {
            "merge_notes": [{"branch": "feat-1", "status": "conflict"}],
            "base_branch": "main",
        }
        result = _render_worktree_merge(data)
        assert "冲突" in result["content"]

    def test_merge_status_merged_chinese(self):
        """merged status should display 已合并."""
        data = {
            "merge_notes": [{"branch": "feat-1", "status": "merged"}],
            "base_branch": "main",
        }
        result = _render_worktree_merge(data)
        assert "已合并" in result["content"]


class TestFooterBlockedReason:
    """Footer renders blocked reason via UI_TEXT key."""

    def test_blocked_reason_renders_in_footer(self):
        from src.card.render.footer import render_footer
        from src.card.state.models import CardState, FooterState, EngineExtState, CardMetadata, HeaderState
        from dataclasses import replace

        meta = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔍")
        state = CardState(
            metadata=meta,
            terminal="blocked",
            header=HeaderState(),
            footer=FooterState(status="idle"),
            engine_ext=EngineExtState(blocked_reason="需要人工确认"),
        )
        elements = render_footer(state)
        texts = [e.get("content", "") for e in elements if e.get("tag") == "markdown"]
        assert any("需要人工确认" in t for t in texts)
        assert any("任务阻塞" in t for t in texts)
