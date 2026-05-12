"""Direct unit tests for render_worktree_panel function.

Covers boundary cases: data=None, empty lists, missing fields, unknown kinds.
"""
import pytest

from src.card.events import CardEvent
from src.card.events.worktree import worktree_tool_select
from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card
from src.card.state.models import CardMetadata, CardState
from src.card.state.reducer import reduce_card_state
from src.card.render.worktree import render_worktree_panel
from src.card.state.models import (
    WorktreeSelectBlock,
    WorktreeConfirmBlock,
    WorktreeUnitsBlock,
    WorktreeMergeBlock,
    WorktreeCleanupBlock,
)


def _collect_buttons(node):
    if isinstance(node, dict):
        found = [node] if node.get("tag") == "button" else []
        for value in node.values():
            found.extend(_collect_buttons(value))
        return found
    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(_collect_buttons(item))
        return found
    return []


def _assert_callback_button(button, expected_value):
    assert button.get("value") == expected_value
    assert button.get("behaviors") == [
        {"type": "callback", "value": expected_value}
    ]


class TestRenderWorktreePanelDataNone:
    """block.data=None should produce a safe fallback."""

    def test_tool_select_data_none(self):
        block = WorktreeSelectBlock(block_id="ts", data=None)
        result = render_worktree_panel(block)
        assert result is not None
        assert result.get("tag") in ("markdown", "collapsible_panel", "column_set")

    def test_confirm_data_none(self):
        block = WorktreeConfirmBlock(block_id="cf", data=None)
        result = render_worktree_panel(block)
        assert result is not None

    def test_units_data_none(self):
        block = WorktreeUnitsBlock(block_id="un", data=None)
        result = render_worktree_panel(block)
        assert result is not None
        assert result.get("tag") == "markdown"  # Fallback for None data

    def test_merge_data_none(self):
        block = WorktreeMergeBlock(block_id="mg", data=None)
        result = render_worktree_panel(block)
        assert result is not None

    def test_cleanup_data_none(self):
        block = WorktreeCleanupBlock(block_id="cl", data=None)
        result = render_worktree_panel(block)
        assert result is not None


class TestRenderWorktreePanelEmptyLists:
    """Empty lists in data should not crash."""

    def test_tool_select_empty_tools(self):
        block = WorktreeSelectBlock(block_id="ts", data={"tools": [], "selected": []})
        result = render_worktree_panel(block)
        assert result is not None

    def test_units_empty_units(self):
        block = WorktreeUnitsBlock(block_id="un", data={"units": [], "message": ""})
        result = render_worktree_panel(block)
        # render_worktree_panel wraps in column_set with stepper elements + content
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        # Stepper produces 2 markdown elements (active + pending) + 1 content element
        assert len(elements) >= 2
        # First element is stepper markdown (active part)
        assert elements[0]["tag"] == "markdown"
        # Last element is the collapsible_panel
        panel = elements[-1]
        assert panel["tag"] == "collapsible_panel"
        assert len(panel["elements"]) >= 1

    def test_merge_empty_notes(self):
        block = WorktreeMergeBlock(block_id="mg", data={"merge_notes": [], "base_branch": "main"})
        result = render_worktree_panel(block)
        assert result is not None

    def test_cleanup_empty_notes(self):
        block = WorktreeCleanupBlock(block_id="cl", data={
            "merge_notes": [], "base_branch": "main",
            "merge_results": None, "cleanup_phase": "summary",
        })
        result = render_worktree_panel(block)
        assert result is not None


class TestRenderWorktreePanelMissingFields:
    """Dicts with missing expected keys should use defaults."""

    def test_unit_missing_name_and_status(self):
        block = WorktreeUnitsBlock(block_id="un", data={
            "units": [{}],  # No name, no status
            "message": "",
        })
        result = render_worktree_panel(block)
        # Wrapped in column_set with stepper
        assert result["tag"] == "column_set"
        panel = result["columns"][0]["elements"][-1]
        assert panel["tag"] == "collapsible_panel"

    def test_tool_select_missing_selected(self):
        block = WorktreeSelectBlock(block_id="ts", data={
            "tools": [{"name": "tool1"}],
            # 'selected' key missing
        })
        result = render_worktree_panel(block)
        assert result is not None

    def test_merge_note_missing_status(self):
        block = WorktreeMergeBlock(block_id="mg", data={
            "merge_notes": [{"branch": "feat-1"}],  # No 'status'
            "base_branch": "main",
        })
        result = render_worktree_panel(block)
        assert result is not None


class TestRenderWorktreeToolSelectInteractions:
    """Tool selection rows must be real Feishu actions, not static markdown checkboxes."""

    def test_tool_select_panel_renders_tool_callback_buttons(self):
        block = WorktreeSelectBlock(
            block_id="ts",
            data={
                "project_id": "proj-1",
                "tools": [
                    {
                        "provider": "acp",
                        "tool_name": "coco",
                        "display_name": "Coco",
                        "description": "字节跳动 AI",
                        "supports_model": True,
                    },
                    {
                        "provider": "cli",
                        "tool_name": "claude",
                        "display_name": "Claude",
                        "description": "Anthropic Claude CLI",
                        "supports_model": False,
                    },
                ],
                "selected": [{"tool_name": "coco", "display_name": "Coco"}],
            },
        )

        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)
        expected = {
            "action": "worktree_select_tool",
            "tool_name": "coco",
            "display_name": "Coco",
            "agent_name": "",
            "provider": "acp",
            "supports_model": True,
            "skip_model_selection": False,
            "project_id": "proj-1",
        }

        coco_button = next(
            btn for btn in buttons
            if btn.get("value", {}).get("tool_name") == "coco"
        )
        value = dict(coco_button["value"])
        assert value.pop("_selection_sig")
        assert value == expected
        assert coco_button.get("behaviors") == [
            {"type": "callback", "value": coco_button.get("value")}
        ]
        assert any(
            btn.get("value", {}).get("tool_name") == "claude"
            and btn.get("behaviors", [{}])[0].get("type") == "callback"
            for btn in buttons
        )

    def test_reducer_render_path_keeps_tool_buttons_interactive(self):
        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            worktree_tool_select(
                tools=[
                    {
                        "provider": "acp",
                        "tool_name": "coco",
                        "display_name": "Coco",
                        "description": "字节跳动 AI",
                        "supports_model": True,
                    }
                ],
                selected=[],
                project_id="proj-render",
            ),
        )

        rendered = render_card(state, RenderBudget())[0].to_feishu_json()
        buttons = _collect_buttons(rendered)
        button = next(
            btn for btn in buttons
            if (
                btn.get("value", {}).get("action") == "worktree_select_tool"
                and btn.get("value", {}).get("tool_name") == "coco"
                and btn.get("value", {}).get("project_id") == "proj-render"
            )
        )
        assert button.get("behaviors") == [
            {"type": "callback", "value": button.get("value")}
        ]

    def test_selected_actions_render_callback_behaviors(self):
        block = WorktreeSelectBlock(
            block_id="ts",
            data={
                "project_id": "proj-1",
                "tools": [{"provider": "cli", "tool_name": "claude", "display_name": "Claude"}],
                "selected": [{"selection_key": "cli:claude:default", "display_label": "CLI · Claude"}],
            },
        )

        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)

        for action in ("worktree_remove_item", "worktree_clear_items", "worktree_finish_selection"):
            button = next(
                btn for btn in buttons
                if btn.get("value", {}).get("action") == action
            )
            assert button.get("behaviors") == [
                {"type": "callback", "value": button.get("value")}
            ]

    def test_tool_button_payload_changes_after_selection_changes(self):
        """已选组合变化后，同一个工具按钮 payload 也要变化，避免被卡片 action 去重误拦。"""
        def render_coco_value(selected):
            block = WorktreeSelectBlock(
                block_id="ts",
                data={
                    "project_id": "proj-dedup",
                    "tools": [
                        {
                            "provider": "acp",
                            "tool_name": "coco",
                            "display_name": "Coco",
                            "supports_model": True,
                        },
                    ],
                    "selected": selected,
                },
            )
            result = render_worktree_panel(block)
            button = next(
                btn for btn in _collect_buttons(result)
                if btn.get("value", {}).get("action") == "worktree_select_tool"
            )
            return button["value"]

        empty_value = render_coco_value([])
        selected_value = render_coco_value([
            {
                "tool_name": "coco",
                "display_name": "Coco",
                "selection_key": "acp:coco:test-o-new-thinking",
            }
        ])

        assert empty_value["tool_name"] == selected_value["tool_name"] == "coco"
        assert empty_value["_selection_sig"] != selected_value["_selection_sig"]

    def test_reducer_render_path_keeps_tool_buttons_interactive_legacy_value(self):
        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            worktree_tool_select(
                tools=[
                    {
                        "provider": "acp",
                        "tool_name": "coco",
                        "display_name": "Coco",
                        "description": "字节跳动 AI",
                        "supports_model": True,
                    }
                ],
                selected=[],
                project_id="proj-render",
            ),
        )

        rendered = render_card(state, RenderBudget())[0].to_feishu_json()
        buttons = _collect_buttons(rendered)
        assert any(
            btn.get("value", {}).get("action") == "worktree_select_tool"
            and btn.get("value", {}).get("tool_name") == "coco"
            and btn.get("value", {}).get("project_id") == "proj-render"
            for btn in buttons
        )

    def test_model_select_panel_routes_model_callbacks(self):
        block = WorktreeSelectBlock(
            block_id="models",
            data={
                "project_id": "proj-2",
                "select_action": "worktree_select_model",
                "tools": [
                    {
                        "id": "gpt-5.2",
                        "name": "GPT-5.2",
                        "description": "模型: GPT-5.2",
                    }
                ],
                "selected": [],
            },
        )

        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)
        expected = {
            "action": "worktree_select_model",
            "model_name": "gpt-5.2",
            "model_display_name": "GPT-5.2",
            "project_id": "proj-2",
        }
        button = next(
            btn for btn in buttons
            if btn.get("value", {}).get("action") == "worktree_select_model"
        )
        _assert_callback_button(button, expected)

    def test_model_select_panel_shows_back_button_and_pending_tool_banner(self):
        """模型选择卡必须：① 显示返回工具选择按钮 ② 突出 pending_tool 名称 ③ 不输出确认/清空按钮。"""
        block = WorktreeSelectBlock(
            block_id="models",
            data={
                "project_id": "proj-back",
                "select_action": "worktree_select_model",
                "pending_tool": "Coco",
                "tools": [
                    {"id": "doubao-pro", "name": "Doubao Pro", "description": "模型: Doubao Pro"},
                ],
                "selected": [{
                    "tool_name": "aiden",
                    "display_name": "Aiden",
                    "provider": "acp",
                    "selection_key": "acp:aiden:default",
                    "display_label": "ACP · Aiden / 默认模型",
                }],
            },
        )
        result = render_worktree_panel(block)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        # 必须有醒目的 pending_tool banner
        assert "为 Coco 选择模型" in rendered
        # 已选组合上下文保留
        assert "已选组合 (1)" in rendered
        assert "ACP · Aiden / 默认模型" in rendered
        # 不允许出现确认 / 清空 按钮（它们只属于工具选择卡）
        buttons = _collect_buttons(result)
        actions = {btn.get("value", {}).get("action") for btn in buttons}
        assert "worktree_finish_selection" not in actions
        assert "worktree_clear_items" not in actions
        assert "worktree_remove_item" not in actions
        # 必须有返回工具选择按钮，且为 callback
        back_button = next(
            btn for btn in buttons
            if btn.get("value", {}).get("action") == "show_worktree_menu"
        )
        _assert_callback_button(back_button, {"action": "show_worktree_menu", "project_id": "proj-back"})

    def test_tool_select_subtitle_uses_tool_select_text(self):
        """工具选择阶段 header subtitle 文案保持为'选择工具'。"""
        from src.card.events import CardEvent
        from src.card.render.budget import RenderBudget
        from src.card.render.renderer import render_card
        from src.card.state.models import CardMetadata, CardState
        from src.card.state.reducer import reduce_card_state
        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            worktree_tool_select(
                tools=[{"provider": "acp", "tool_name": "coco", "display_name": "Coco"}],
                selected=[],
                project_id="p-tool",
            ),
        )
        rendered = render_card(state, RenderBudget())[0].to_feishu_json()
        assert rendered.get("header", {}).get("subtitle", {}).get("content") == "选择工具"

    def test_model_button_label_clamped_when_name_is_long_metadata(self):
        """防御：旧版/异常上游把 metadata blurb 塞到 name 槽位时，按钮文本仍要短，
        否则飞书会把按钮文案过长的行折叠到无法点击，用户表现为'又无法选模型了'。"""
        long_meta = "Context window: 168k, Max tool turns: 200, Quota: 48% used, resets weekly"
        block = WorktreeSelectBlock(
            block_id="models",
            data={
                "project_id": "p",
                "select_action": "worktree_select_model",
                "pending_tool": "Coco",
                # 模拟旧版 model_tools 形状：name 被错误塞成 metadata
                "tools": [{"id": "GPT-5.2", "name": long_meta, "description": ""}],
                "selected": [],
            },
        )
        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)
        button = next(
            btn for btn in buttons
            if btn.get("value", {}).get("action") == "worktree_select_model"
        )
        # 按钮 value 仍要发回真正的 model_id，不影响后端处理
        assert button["value"]["model_name"] == "GPT-5.2"
        # 但按钮文案必须短到飞书可正常渲染（< 30 chars 包含前缀）
        assert len(button["text"]["content"]) <= 30
        assert button["text"]["content"].startswith("选择 ")

    def test_model_grid_uses_clean_callback_buttons_without_metadata(self):
        """ACP 模型选择用紧凑按钮网格，按钮文案和值都不能混入 metadata。"""
        block = WorktreeSelectBlock(
            block_id="models",
            data={
                "project_id": "p",
                "select_action": "worktree_select_model",
                "pending_tool": "Coco",
                "tools": [
                    {"id": "GPT-5.2", "name": "GPT-5.2", "description": "Model load: 14%"},
                ],
                "selected": [],
            },
        )
        result = render_worktree_panel(block)

        import json
        rendered = json.dumps(result, ensure_ascii=False)
        assert "Model load: 14%" not in rendered

        button = next(
            btn for btn in _collect_buttons(result)
            if btn.get("value", {}).get("action") == "worktree_select_model"
        )
        assert button["text"]["content"] == "选择 GPT-5.2"
        assert button["value"]["model_name"] == "GPT-5.2"
        assert button["value"]["model_display_name"] == "GPT-5.2"

    def test_large_model_select_card_stays_under_feishu_node_budget(self):
        """真实 ACP 模型列表较长时，模型选择卡仍必须低于 Feishu 200 元素硬限制。"""
        from src.card.render.payload_truncator import count_tagged_nodes

        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            worktree_tool_select(
                tools=[
                    {
                        "id": f"model-{i:02d}-very-long-name",
                        "name": f"Model {i:02d}",
                        "description": "Context window: 168k, Max tool turns: 200, Quota: 48% used",
                    }
                    for i in range(35)
                ],
                selected=[],
                project_id="p-large",
                select_action="worktree_select_model",
                pending_tool="Coco",
            ),
        )
        rendered = render_card(state, RenderBudget())[0].to_feishu_json()

        assert count_tagged_nodes(rendered) <= 180

    def test_model_select_subtitle_uses_model_select_text(self):
        """模型选择阶段 header subtitle 必须显示为'选择模型'，与工具选择视觉区分。"""
        from src.card.events import CardEvent
        from src.card.render.budget import RenderBudget
        from src.card.render.renderer import render_card
        from src.card.state.models import CardMetadata, CardState
        from src.card.state.reducer import reduce_card_state
        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            worktree_tool_select(
                tools=[{"id": "gpt", "name": "GPT-5.2", "description": "模型: GPT-5.2"}],
                selected=[],
                project_id="p-model",
                select_action="worktree_select_model",
                pending_tool="Coco",
            ),
        )
        rendered = render_card(state, RenderBudget())[0].to_feishu_json()
        assert rendered.get("header", {}).get("subtitle", {}).get("content") == "选择模型"


class TestRenderWorktreePanelUnknownKind:
    """Unknown block kind should produce a safe fallback."""

    def test_unknown_kind_returns_fallback(self):
        # Use a worktree block but give it data so it passes the None check,
        # then test the else branch by using a block whose kind isn't in the dispatch
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeBlock:
            kind: str = "worktree_unknown"
            block_id: str = "fake"
            content: str = "fallback content"
            data: dict | None = None

        block = FakeBlock(data={"something": True})
        result = render_worktree_panel(block)
        # Falls through to the else branch: returns markdown with block.content
        assert result == {"tag": "markdown", "content": "fallback content"}


class TestRenderWorktreeUnitsElapsedTime:
    """Running units should display elapsed time from metadata.started_at."""

    def test_running_unit_shows_elapsed_seconds(self):
        import time
        block = WorktreeUnitsBlock(
            block_id="progress",
            data={
                "units": [
                    {
                        "unit_id": "wt-01",
                        "display_name": "工作空间 A",
                        "status": "running",
                        "summary": "",
                        "error": "",
                        "metadata": {"started_at": time.time() - 30},
                    }
                ],
                "message": "",
            },
        )
        result = render_worktree_panel(block)
        # Should contain time indication (Ns or Nmin)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        assert "工作空间 A" in rendered
        # 30 seconds → shows "30s" or similar
        assert "s)" in rendered or "min)" in rendered

    def test_running_unit_shows_elapsed_minutes(self):
        import time
        block = WorktreeUnitsBlock(
            block_id="progress",
            data={
                "units": [
                    {
                        "unit_id": "wt-02",
                        "display_name": "工作空间 B",
                        "status": "running",
                        "summary": "",
                        "error": "",
                        "metadata": {"started_at": time.time() - 180},
                    }
                ],
                "message": "",
            },
        )
        result = render_worktree_panel(block)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        assert "3min)" in rendered

    def test_completed_unit_no_elapsed_time(self):
        import time
        block = WorktreeUnitsBlock(
            block_id="progress",
            data={
                "units": [
                    {
                        "unit_id": "wt-03",
                        "display_name": "工作空间 C",
                        "status": "completed",
                        "summary": "Done",
                        "error": "",
                        "metadata": {"started_at": time.time() - 60},
                    }
                ],
                "message": "",
            },
        )
        result = render_worktree_panel(block)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        # Completed units should NOT show elapsed time
        assert "min)" not in rendered
        assert "s)" not in rendered


class TestWorktreeStepperHintRendering:
    """AC-14: Each worktree step panel includes a hint notation element."""

    def test_tool_select_has_hint(self):
        block = WorktreeSelectBlock(block_id="ts", data={"tools": [{"name": "Coco"}], "selected": []})
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        # Find hint element
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "工具" in el.get("content", "")]
        assert len(hints) >= 1, "Tool select panel should have a hint element"

    def test_confirm_has_hint(self):
        block = WorktreeConfirmBlock(block_id="cf", data={
            "selected": [{"name": "Coco"}], "goal": "test goal", "project_id": "p1"
        })
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "确认" in el.get("content", "")]
        assert len(hints) >= 1, "Confirm panel should have a hint element"

    def test_units_has_hint(self):
        block = WorktreeUnitsBlock(block_id="un", data={"units": [], "message": ""})
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "执行中" in el.get("content", "")]
        assert len(hints) >= 1, "Units panel should have a hint element"

    def test_merge_has_hint(self):
        block = WorktreeMergeBlock(block_id="mg", data={"merge_notes": [], "base_branch": "main"})
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "合并" in el.get("content", "")]
        assert len(hints) >= 1, "Merge panel should have a hint element"
