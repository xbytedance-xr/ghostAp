"""Tests for sub-reducers."""
from src.card.events import CardEvent
from src.card.state.models import CardMetadata, CardState
from src.card.state.reducer import reduce_card_state
from src.card.state.reducers.lifecycle import reduce_lifecycle
from src.card.state.reducers.plan import reduce_plan
from src.card.state.reducers.reasoning import reduce_reasoning
from src.card.state.reducers.text import reduce_text
from src.card.state.reducers.tool import reduce_tool


def _base_state() -> CardState:
    return CardState(metadata=CardMetadata(engine_type="deep", project_name="Test", mode_name="Deep Agent", mode_emoji="🧠"))


class TestTextReducer:
    def test_text_started_creates_block(self):
        s = reduce_text(_base_state(), CardEvent.text_started("b1"))
        assert len(s.blocks) == 1
        assert s.blocks[0].kind == "text"
        assert s.blocks[0].block_id == "b1"
        assert s.blocks[0].status == "active"

    def test_text_delta_appends(self):
        s = _base_state()
        s = reduce_text(s, CardEvent.text_started("b1"))
        s = reduce_text(s, CardEvent.text_delta("b1", "hello "))
        s = reduce_text(s, CardEvent.text_delta("b1", "world"))
        assert s.blocks[0].content == "hello world"

    def test_text_delta_collapses_soft_streaming_newlines(self):
        s = _base_state()
        s = reduce_text(s, CardEvent.text_started("b1"))
        for chunk in ("让我先\n", "了解\n", "飞书\n", "channel\n", "的实\n", "现文件。"):
            s = reduce_text(s, CardEvent.text_delta("b1", chunk))

        assert s.blocks[0].content == "让我先了解飞书 channel 的实现文件。"

    def test_text_delta_preserves_markdown_structural_newlines(self):
        s = _base_state()
        s = reduce_text(s, CardEvent.text_started("b1"))
        for chunk in ("计划：\n", "1. 检查实现\n", "2. 补充测试\n\n", "第二段"):
            s = reduce_text(s, CardEvent.text_delta("b1", chunk))

        assert s.blocks[0].content == "计划：\n1. 检查实现\n2. 补充测试\n\n第二段"

    def test_text_delta_collapses_leading_soft_newline(self):
        s = _base_state()
        s = reduce_text(s, CardEvent.text_started("b1"))
        s = reduce_text(s, CardEvent.text_delta("b1", "Now"))
        s = reduce_text(s, CardEvent.text_delta("b1", "\nlet"))
        s = reduce_text(s, CardEvent.text_delta("b1", "\nme read"))

        assert s.blocks[0].content == "Now let me read"

    def test_text_delta_collapses_double_newline_between_short_stream_chunks(self):
        s = _base_state()
        s = reduce_text(s, CardEvent.text_started("b1"))
        for chunk in ("现在让\n\n", "我查\n\n", "看飞书channel\n\n", "的主要实现文件。"):
            s = reduce_text(s, CardEvent.text_delta("b1", chunk))

        assert s.blocks[0].content == "现在让我查看飞书channel 的主要实现文件。"

    def test_text_delta_auto_creates(self):
        s = reduce_text(_base_state(), CardEvent.text_delta("b1", "hi"))
        assert len(s.blocks) == 1
        assert s.blocks[0].content == "hi"

    def test_text_done_marks_completed(self):
        s = reduce_text(_base_state(), CardEvent.text_started("b1"))
        s = reduce_text(s, CardEvent.text_done("b1"))
        assert s.blocks[0].status == "completed"
        assert s.blocks[0].element_id is None


class TestToolReducer:
    def test_tool_started(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("t1", "bash", "ls"))
        assert len(s.blocks) == 1
        assert s.blocks[0].tool_name == "bash"
        assert s.footer.status == "tool_running"

    def test_tool_done(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("t1", "bash"))
        s = reduce_tool(s, CardEvent.tool_done("t1", tool_output="ok", tool_summary="ls -la"))
        assert s.blocks[0].status == "completed"
        assert s.blocks[0].tool_output == "ok"
        assert s.footer.status is None

    def test_tool_failed(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("t1", "bash"))
        s = reduce_tool(s, CardEvent.tool_failed("t1", error="not found"))
        assert s.blocks[0].status == "failed"
        assert s.blocks[0].tool_output == "not found"

    def test_task_tool_delta_updates_late_description_summary(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("task-1", "task", "task"))
        s = reduce_tool(s, CardEvent.tool_delta("task-1", "整理 Spec Review 角色面板"))

        assert s.blocks[0].tool_summary == "整理 Spec Review 角色面板"


class TestReasoningReducer:
    def test_reasoning_started(self):
        s = reduce_reasoning(_base_state(), CardEvent.reasoning_started("r1"))
        assert s.blocks[0].kind == "reasoning"
        assert s.footer.status == "thinking"

    def test_reasoning_delta_accumulates(self):
        s = reduce_reasoning(_base_state(), CardEvent.reasoning_started("r1"))
        s = reduce_reasoning(s, CardEvent.reasoning_delta("r1", "think"))
        s = reduce_reasoning(s, CardEvent.reasoning_delta("r1", "ing"))
        assert s.blocks[0].content == "thinking"
        assert s.blocks[0].char_count == 8

    def test_reasoning_done(self):
        s = reduce_reasoning(_base_state(), CardEvent.reasoning_started("r1"))
        s = reduce_reasoning(s, CardEvent.reasoning_done("r1"))
        assert s.blocks[0].status == "completed"


class TestPlanReducer:
    def test_plan_creates_block(self):
        s = reduce_plan(_base_state(), CardEvent.plan_updated("step1\nstep2"))
        assert len(s.blocks) == 1
        assert s.blocks[0].kind == "plan"
        assert s.blocks[0].content == "step1\nstep2"

    def test_plan_updates_existing(self):
        s = reduce_plan(_base_state(), CardEvent.plan_updated("v1"))
        s = reduce_plan(s, CardEvent.plan_updated("v2"))
        assert len(s.blocks) == 1
        assert s.blocks[0].content == "v2"


class TestReviewRoleReducer:
    def test_review_result_updated_creates_one_block_per_role(self):
        event = CardEvent.review_result_updated(
            2,
            [
                {
                    "role_id": "tester",
                    "title": "测试工程师",
                    "emoji": "🧪",
                    "status_text": "❌ 有建议",
                    "passed": False,
                    "suggestions": ["补充 schema 回归", "覆盖分页边界"],
                    "summary": "测试覆盖不足",
                    "agent_detail": "Codex / gpt-5.5",
                    "blocking": True,
                },
                {
                    "role_id": "designer",
                    "title": "体验设计师",
                    "emoji": "🎨",
                    "status_text": "✅ PASS",
                    "passed": True,
                    "suggestions": [],
                    "summary": "",
                    "agent_detail": "",
                    "blocking": False,
                },
            ],
        )

        state = reduce_card_state(_base_state(), event)

        assert [block.kind for block in state.blocks] == ["review_role", "review_role"]
        assert state.blocks[0].data["title"] == "测试工程师"
        assert state.blocks[0].data["suggestions"] == ["补充 schema 回归", "覆盖分页边界"]
        assert state.blocks[1].data["title"] == "体验设计师"


class TestSpecArtifactReducers:
    def test_spec_plan_updated_creates_structured_plan_block(self):
        event = CardEvent.spec_plan_updated(
            1,
            {
                "architecture": "保持 Spec 阶段产物结构化展示，避免 raw JSON 直接入卡。",
                "tech_stack": ["CardSession", "Feishu Schema 2.0"],
                "steps": ["添加事件", "添加 reducer", "添加 renderer"],
                "file_changes": ["src/card/events/types.py", "src/card/render/spec_artifacts.py"],
                "test_plan": ["覆盖事件、状态、渲染"],
                "risks": ["飞书 payload 过大时依赖分页"],
            },
        )

        state = reduce_card_state(_base_state(), event)

        assert [block.kind for block in state.blocks] == ["spec_plan"]
        assert state.blocks[0].data["cycle_num"] == 1
        assert state.blocks[0].data["steps"] == ["添加事件", "添加 reducer", "添加 renderer"]

    def test_spec_tasks_updated_creates_one_block_per_task_without_truncating_description(self):
        full_description = "调整 Spec 卡片任务分解展示，任务 1 的完整说明必须保留，避免 build 阶段说任务 1 时上下文丢失"
        event = CardEvent.spec_tasks_updated(
            2,
            [
                {"task_id": 1, "description": full_description, "dependencies": []},
                {"task_id": 2, "description": "补充方案规划展示", "dependencies": []},
                {"task_id": 3, "description": "补充任务分解逐项展示", "dependencies": [1, 2]},
            ],
        )

        state = reduce_card_state(_base_state(), event)

        assert [block.kind for block in state.blocks] == ["spec_task", "spec_task", "spec_task"]
        assert state.blocks[0].data["description"] == full_description
        assert state.blocks[2].data["dependencies"] == [1, 2]


class TestLifecycleReducer:
    def test_started(self):
        s = reduce_lifecycle(_base_state(), CardEvent.started())
        assert s.terminal == "running"
        assert s.header.title == "🧠 Test · Deep Agent"

    def test_completed_green(self):
        s = reduce_lifecycle(_base_state(), CardEvent.completed())
        assert s.terminal == "completed"
        assert s.header.template == "green"

    def test_failed_red(self):
        s = reduce_lifecycle(_base_state(), CardEvent.failed("err"))
        assert s.terminal == "failed"
        assert s.header.template == "red"

    def test_failed_uses_unified_error_visual_contract(self):
        s = reduce_lifecycle(
            _base_state(),
            CardEvent.failed(
                "boom",
                details="trace 摘要",
                detail_action={"action": "show_error_details"},
                retry_action={"action": "deep_resume"},
            ),
        )

        error_block = s.blocks[-1].content
        assert "**错误摘要**" in error_block
        assert "boom" in error_block
        assert "详情已收起" in error_block
        assert "**详细信息**" not in error_block
        assert "trace 摘要" not in error_block
        assert any(button.text == "查看详情" for button in s.buttons)
        assert any(button.action_id == "show_error_details" for button in s.buttons)
        detail_buttons = [button for button in s.buttons if button.action_id == "show_error_details"]
        assert detail_buttons[0].value and detail_buttons[0].value.get("diagnostic_token")
        assert "details" not in detail_buttons[0].value
        assert any(button.text.startswith("🔁") for button in s.buttons)
        assert any(button.action_id == "deep_resume" for button in s.buttons)

    def test_failed_without_detail_action_does_not_show_misleading_details_button(self):
        s = reduce_lifecycle(
            _base_state(),
            CardEvent.failed("boom", retry_action={"action": "deep_resume"}),
        )

        assert not any(button.text == "查看详情" for button in s.buttons)
        assert any(button.text == "📋 查看状态" and button.action_id == "intent.global.show_status" for button in s.buttons)

    def test_cancelled_grey(self):
        s = reduce_lifecycle(_base_state(), CardEvent.cancelled())
        assert s.terminal == "cancelled"
        assert s.header.template == "grey"
