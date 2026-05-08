"""Unit tests for card state reducers: criteria, cycle, phase, lifecycle."""
import pytest
from dataclasses import replace

from src.card.events import CardEvent, CardEventType
from src.card.state.button_intent import ButtonIntent
from src.card.state.models import (
    CardState, CardMetadata, ContentBlock, FooterState, EngineExtState, ButtonSpec,
)
from src.card.state.reducers.criteria import reduce_criteria
from src.card.state.reducers.cycle import reduce_cycle
from src.card.state.reducers.phase import reduce_phase
from src.card.state.reducers.lifecycle import reduce_lifecycle


def _base_state(**kwargs) -> CardState:
    """Create a base state with sensible defaults for testing."""
    if "engine_ext" not in kwargs:
        kwargs["engine_ext"] = EngineExtState()
    return CardState(**kwargs)


# ==============================================================================
# reduce_criteria tests
# ==============================================================================


class TestReduceCriteria:
    def test_criteria_updated_sets_content_and_counts(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.CRITERIA_UPDATED, payload={
            "content": "## Criteria\n- [x] done\n- [ ] pending",
            "satisfied_count": 1,
            "total_count": 2,
        })
        new = reduce_criteria(state, event)
        assert new.engine_ext.criteria_section == "## Criteria\n- [x] done\n- [ ] pending"
        assert new.engine_ext.criteria_satisfied == 1
        assert new.engine_ext.criteria_total == 2

    def test_criteria_updated_preserves_existing_counts_if_not_provided(self):
        ext = EngineExtState(criteria_satisfied=3, criteria_total=5)
        state = _base_state(engine_ext=ext)
        event = CardEvent(type=CardEventType.CRITERIA_UPDATED, payload={
            "content": "updated",
        })
        new = reduce_criteria(state, event)
        assert new.engine_ext.criteria_section == "updated"
        assert new.engine_ext.criteria_satisfied == 3
        assert new.engine_ext.criteria_total == 5

    def test_warning_updated_sets_banner(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.WARNING_UPDATED, payload={
            "warning": "⚠️ Token limit approaching"
        })
        new = reduce_criteria(state, event)
        assert new.footer.warning_banner == "⚠️ Token limit approaching"

    def test_warning_updated_clears_banner_with_empty(self):
        state = _base_state(footer=FooterState(warning_banner="old warning"))
        event = CardEvent(type=CardEventType.WARNING_UPDATED, payload={
            "warning": ""
        })
        new = reduce_criteria(state, event)
        assert new.footer.warning_banner is None

    def test_review_retry_waiting_status(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.REVIEW_RETRY, payload={
            "attempt": 2,
            "max_attempts": 3,
            "status": "waiting",
            "delay_sec": 10,
        })
        new = reduce_criteria(state, event)
        assert "等待 10 秒" in new.footer.status_text
        assert "2/3" in new.footer.status_text
        assert any(b.action_id == ButtonIntent.SPEC_STOP for b in new.buttons)

    def test_review_retry_exhausted_shows_resume_button(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.REVIEW_RETRY, payload={
            "attempt": 3,
            "max_attempts": 3,
            "status": "exhausted",
        })
        new = reduce_criteria(state, event)
        assert "重试已耗尽" in new.footer.status_text
        assert any(b.action_id == ButtonIntent.SPEC_RESUME for b in new.buttons)

    def test_review_retry_delay_sec_none_no_crash(self):
        """AC: delay_sec=None should not raise TypeError."""
        state = _base_state()
        event = CardEvent(type=CardEventType.REVIEW_RETRY, payload={
            "attempt": 1,
            "max_attempts": 2,
            "status": "waiting",
            "delay_sec": None,
        })
        new = reduce_criteria(state, event)
        assert "等待 0 秒" in new.footer.status_text

    def test_unrelated_event_returns_state_unchanged(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.STARTED)
        assert reduce_criteria(state, event) is state


# ==============================================================================
# reduce_cycle tests
# ==============================================================================


class TestReduceCycle:
    def test_cycle_started_sets_cycle_num_and_footer(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.CYCLE_STARTED, payload={
            "cycle_num": 2,
            "max_cycles": 5,
        })
        new = reduce_cycle(state, event)
        assert new.engine_ext.cycle_num == 2
        assert new.engine_ext.max_cycles == 5
        assert "2/5" in new.footer.status_text
        assert new.terminal == "running"

    def test_cycle_started_with_cycle_num_zero(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.CYCLE_STARTED, payload={
            "cycle_num": 0,
            "max_cycles": 1,
        })
        new = reduce_cycle(state, event)
        assert new.engine_ext.cycle_num == 0

    def test_cycle_done_clears_footer(self):
        ext = EngineExtState(cycle_num=2, max_cycles=5)
        state = _base_state(
            engine_ext=ext,
            footer=FooterState(status="tool_running", status_text="⏳ 迭代 2/5"),
        )
        event = CardEvent(type=CardEventType.CYCLE_DONE, payload={
            "cycle_num": 2,
        })
        new = reduce_cycle(state, event)
        assert new.footer.status == "idle"
        assert new.footer.status_text is None

    def test_unrelated_event_returns_state_unchanged(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.COMPLETED)
        assert reduce_cycle(state, event) is state


# ==============================================================================
# reduce_phase tests
# ==============================================================================


class TestReducePhase:
    def test_phase_started_adds_block(self):
        ext = EngineExtState(cycle_num=1)
        state = _base_state(engine_ext=ext)
        event = CardEvent(type=CardEventType.PHASE_STARTED, payload={
            "cycle_num": 1,
            "phase": "planning",
        })
        new = reduce_phase(state, event)
        assert len(new.blocks) == 1
        assert new.blocks[0].block_id == "phase_1_planning"
        assert new.blocks[0].status == "active"
        assert new.engine_ext.phase_info == "planning"

    def test_phase_started_idempotent_replaces_existing(self):
        """AC: Duplicate PHASE_STARTED replaces existing active block."""
        ext = EngineExtState(cycle_num=1)
        block = ContentBlock(
            kind="phase", block_id="phase_1_planning",
            content="planning", status="active", phase_name="planning", cycle_num=1,
        )
        state = _base_state(engine_ext=ext, blocks=(block,))
        event = CardEvent(type=CardEventType.PHASE_STARTED, payload={
            "cycle_num": 1,
            "phase": "planning",
        })
        new = reduce_phase(state, event)
        # Should still have exactly one block with same id
        matching = [b for b in new.blocks if b.block_id == "phase_1_planning"]
        assert len(matching) == 1
        assert matching[0].status == "active"

    def test_phase_done_marks_completed(self):
        ext = EngineExtState(cycle_num=1, phase_info="planning")
        block = ContentBlock(
            kind="phase", block_id="phase_1_planning",
            content="planning", status="active", phase_name="planning", cycle_num=1,
        )
        state = _base_state(engine_ext=ext, blocks=(block,))
        event = CardEvent(type=CardEventType.PHASE_DONE, payload={
            "cycle_num": 1,
            "phase": "planning",
            "output": "Plan created",
        })
        new = reduce_phase(state, event)
        assert new.blocks[0].status == "completed"
        assert new.blocks[0].content == "Plan created"
        assert new.engine_ext.phase_info is None

    def test_phase_done_without_prior_start_returns_unchanged(self):
        """AC: PHASE_DONE without PHASE_STARTED logs warning, returns state."""
        state = _base_state()
        event = CardEvent(type=CardEventType.PHASE_DONE, payload={
            "cycle_num": 1,
            "phase": "unknown",
        })
        new = reduce_phase(state, event)
        assert new is state

    def test_unrelated_event_returns_state_unchanged(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.STARTED)
        assert reduce_phase(state, event) is state


# ==============================================================================
# reduce_lifecycle tests
# ==============================================================================


class TestReduceLifecycle:
    def test_started_sets_running(self):
        state = _base_state()
        event = CardEvent.started()
        new = reduce_lifecycle(state, event)
        assert new.terminal == "running"
        assert new.footer.status == "thinking"

    def test_completed_sets_terminal_and_clears_buttons(self):
        state = _base_state(buttons=(ButtonSpec(text="x", action_id="y"),))
        event = CardEvent.completed()
        new = reduce_lifecycle(state, event)
        assert new.terminal == "completed"
        assert new.buttons == ()

    def test_completed_with_summary_appends_block(self):
        """AC: COMPLETED with summary adds ContentBlock."""
        state = _base_state()
        event = CardEvent.completed(summary="Done: 5 tools called")
        new = reduce_lifecycle(state, event)
        assert new.terminal == "completed"
        summary_blocks = [b for b in new.blocks if b.block_id == "_summary"]
        assert len(summary_blocks) == 1
        assert "5 tools called" in summary_blocks[0].content

    def test_failed_inserts_error_block(self):
        """AC: FAILED with error inserts visible error block."""
        state = _base_state()
        event = CardEvent.failed("Connection timeout")
        new = reduce_lifecycle(state, event)
        assert new.terminal == "failed"
        error_blocks = [b for b in new.blocks if b.block_id == "_error"]
        assert len(error_blocks) == 1
        assert "Connection timeout" in error_blocks[0].content

    def test_failed_injects_retry_button_for_deep(self):
        """AC: FAILED on deep engine injects retry button."""
        meta = CardMetadata(engine_type="deep")
        state = _base_state(metadata=meta)
        event = CardEvent.failed("error")
        new = reduce_lifecycle(state, event)
        assert any(b.action_id == ButtonIntent.DEEP_RESUME for b in new.buttons)

    def test_failed_injects_retry_button_for_loop(self):
        meta = CardMetadata(engine_type="loop")
        state = _base_state(metadata=meta)
        event = CardEvent.failed("error")
        new = reduce_lifecycle(state, event)
        assert any(b.action_id == ButtonIntent.LOOP_RESUME for b in new.buttons)

    def test_failed_injects_retry_button_for_spec(self):
        meta = CardMetadata(engine_type="spec")
        state = _base_state(metadata=meta)
        event = CardEvent.failed("error")
        new = reduce_lifecycle(state, event)
        assert any(b.action_id == ButtonIntent.SPEC_RESUME for b in new.buttons)

    def test_failed_no_retry_for_unknown_engine(self):
        meta = CardMetadata(engine_type="unknown")
        state = _base_state(metadata=meta)
        event = CardEvent.failed("error")
        new = reduce_lifecycle(state, event)
        # No retry button for unknown engine, but "查看详情" is always present
        assert not any(b.action_id in (ButtonIntent.DEEP_RESUME, ButtonIntent.LOOP_RESUME, ButtonIntent.SPEC_RESUME, ButtonIntent.WORKTREE_RETRY_FAILED) for b in new.buttons)
        assert any(b.action_id == ButtonIntent.SHOW_STATUS for b in new.buttons)

    def test_cancelled_clears_buttons(self):
        state = _base_state(buttons=(ButtonSpec(text="x", action_id="y"),))
        event = CardEvent.cancelled()
        new = reduce_lifecycle(state, event)
        assert new.terminal == "cancelled"
        assert new.buttons == ()

    def test_unrelated_event_returns_state_unchanged(self):
        state = _base_state()
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b", "text": "t"})
        assert reduce_lifecycle(state, event) is state


# ==============================================================================
# Stop button injection tests (P4)
# ==============================================================================


class TestLifecycleStopButtons:
    """STARTED/RESUMED should inject stop buttons for engine types."""

    @pytest.mark.parametrize("engine_type,expected_action", [
        ("deep", ButtonIntent.ENGINE_STOP),
        ("loop", ButtonIntent.ENGINE_STOP),
        ("spec", ButtonIntent.ENGINE_STOP),
        ("worktree", ButtonIntent.WORKTREE_CANCEL),
    ])
    def test_started_injects_stop_button(self, engine_type, expected_action):
        state = _base_state(metadata=CardMetadata(engine_type=engine_type))
        event = CardEvent.started()
        new = reduce_lifecycle(state, event)
        # MODE toggle button + stop button
        assert len(new.buttons) == 2
        assert new.buttons[0].action_id == ButtonIntent.MODE_COMPACT
        assert new.buttons[1].action_id == expected_action
        assert new.buttons[1].type == "default"

    def test_started_no_buttons_for_none_engine(self):
        state = _base_state(metadata=CardMetadata(engine_type=None))
        event = CardEvent.started()
        new = reduce_lifecycle(state, event)
        assert new.buttons == ()

    @pytest.mark.parametrize("engine_type", ["deep", "loop", "spec", "worktree"])
    def test_resumed_injects_stop_button(self, engine_type):
        state = _base_state(metadata=CardMetadata(engine_type=engine_type))
        event = CardEvent(type=CardEventType.RESUMED)
        new = reduce_lifecycle(state, event)
        # MODE toggle button + stop button
        assert len(new.buttons) == 2
        assert new.buttons[1].type == "default"

    def test_completed_clears_buttons(self):
        state = _base_state(
            metadata=CardMetadata(engine_type="deep"),
            buttons=(ButtonSpec(text="🛑 停止", action_id=ButtonIntent.DEEP_STOP, type="danger"),),
        )
        event = CardEvent.completed()
        new = reduce_lifecycle(state, event)
        assert new.buttons == ()


# ==============================================================================
# Warning banner semantic type tests (P2)
# ==============================================================================


class TestWarningBannerType:
    """WARNING_UPDATED should infer semantic type from content."""

    def test_warning_type_inferred_from_prefix(self):
        state = _base_state()
        test_cases = [
            ("✅ 完成", "success"),
            ("❌ 失败", "error"),
            ("ℹ️ 信息", "info"),
            ("⚠️ 注意", "warning"),
            ("普通文本", "warning"),
        ]
        for text, expected_type in test_cases:
            event = CardEvent.warning_updated(text)
            new = reduce_criteria(state, event)
            assert new.footer.warning_type == expected_type, f"Failed for '{text}'"

    def test_empty_warning_clears_type(self):
        state = _base_state(footer=FooterState(warning_banner="old", warning_type="error"))
        event = CardEvent.warning_updated("")
        new = reduce_criteria(state, event)
        assert new.footer.warning_banner is None
        assert new.footer.warning_type is None


# ==============================================================================
# ContentBlock factory parametrized tests
# ==============================================================================


class TestTextDeltaAutoCreate:
    """Task 21: TEXT_DELTA auto-creates block and updates footer when block_id not found."""

    def test_text_delta_auto_creates_block(self):
        """TEXT_DELTA with unknown block_id creates a new TextBlock."""
        from src.card.state.reducers.text import reduce_text
        state = _base_state()
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={
            "block_id": "new_block",
            "text": "hello",
        })
        new = reduce_text(state, event)
        assert len(new.blocks) == 1
        assert new.blocks[0].block_id == "new_block"
        assert new.blocks[0].content == "hello"
        assert new.blocks[0].kind == "text"

    def test_text_delta_auto_create_sets_footer_thinking(self):
        """Auto-created block sets footer to 'thinking' status."""
        from src.card.state.reducers.text import reduce_text
        state = _base_state()
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={
            "block_id": "auto_block",
            "text": "content",
        })
        new = reduce_text(state, event)
        assert new.footer.status == "thinking"
        assert "思考" in new.footer.status_text

    def test_text_delta_appends_to_existing_block(self):
        """TEXT_DELTA with existing block_id appends text."""
        from src.card.state.reducers.text import reduce_text
        from src.card.state.models import TextBlock
        block = TextBlock(block_id="b1", content="hello ", status="active")
        state = _base_state(blocks=(block,))
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={
            "block_id": "b1",
            "text": "world",
        })
        new = reduce_text(state, event)
        assert len(new.blocks) == 1
        assert new.blocks[0].content == "hello world"

    def test_text_delta_existing_does_not_update_footer(self):
        """Appending to existing block does NOT change footer."""
        from src.card.state.reducers.text import reduce_text
        from src.card.state.models import TextBlock
        block = TextBlock(block_id="b1", content="x", status="active")
        state = _base_state(blocks=(block,), footer=FooterState(status="tool_running", status_text="🔧 running"))
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={
            "block_id": "b1",
            "text": "y",
        })
        new = reduce_text(state, event)
        # Footer unchanged — only auto-create changes footer
        assert new.footer.status == "tool_running"


class TestContentBlockFactory:
    """ContentBlock() factory creates correct subclass for each kind."""

    @pytest.mark.parametrize("kind,expected_kind", [
        ("text", "text"),
        ("tool_call", "tool_call"),
        ("reasoning", "reasoning"),
        ("plan", "plan"),
        ("phase", "phase"),
        ("criteria", "criteria"),
        ("worktree_tool_select", "worktree_tool_select"),
        ("worktree_confirm", "worktree_confirm"),
        ("worktree_units", "worktree_units"),
        ("worktree_merge", "worktree_merge"),
        ("worktree_cleanup", "worktree_cleanup"),
    ])
    def test_factory_creates_correct_kind(self, kind, expected_kind):
        block = ContentBlock(kind=kind, block_id="test_1")
        assert block.kind == expected_kind
        assert block.block_id == "test_1"

    def test_factory_unknown_kind_defaults_to_text(self):
        block = ContentBlock(kind="unknown_xyz")
        assert block.kind == "text"

    def test_factory_filters_invalid_kwargs(self):
        # tool_name is not a field of TextBlock, should be silently ignored
        block = ContentBlock(kind="text", block_id="b1", tool_name="grep")
        assert block.kind == "text"
        assert block.block_id == "b1"
        assert not hasattr(block, "tool_name") or block.kind == "text"

    def test_factory_tool_block_accepts_tool_fields(self):
        block = ContentBlock(kind="tool_call", block_id="t1", tool_name="read_file")
        assert block.kind == "tool_call"
        assert block.tool_name == "read_file"


# ==============================================================================
# reduce_worktree tests (Task 1 — worktree reducer parametrized coverage)
# ==============================================================================


class TestReduceWorktree:
    """Parametrized tests for reduce_worktree covering all 5 event types."""

    @pytest.fixture
    def wt_state(self):
        """Base state with worktree metadata."""
        return _base_state(metadata=CardMetadata(engine_type="worktree"))

    def test_tool_select_sets_block_no_reducer_buttons(self, wt_state):
        """Tool-select reducer 不再下发 footer 按钮——确认/移除/清空 由 render 层负责
        以避免与卡片内嵌的 '✅ 确认选择' 按钮重复出现。
        """
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_TOOL_SELECT, payload={
            "tools": [{"name": "coco"}, {"name": "claude"}],
            "selected": ["coco"],
            "message": "请选择工具",
        })
        new = reduce_worktree(wt_state, event)
        assert len(new.blocks) == 1
        assert new.blocks[0].kind == "worktree_tool_select"
        assert new.blocks[0].data["selected"] == ["coco"]
        # Render 层全权负责 confirm/remove/clear 按钮
        assert new.buttons == ()

    def test_tool_select_no_selection_no_buttons(self, wt_state):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_TOOL_SELECT, payload={
            "tools": [{"name": "coco"}],
            "selected": [],
            "message": "",
        })
        new = reduce_worktree(wt_state, event)
        assert new.buttons == ()

    def test_confirm_sets_block_and_three_buttons(self, wt_state):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_CONFIRM, payload={
            "selected_items": [{"tool": "coco", "model": "gpt-4o"}],
            "goal": "Build feature X",
            "message": "确认配置",
        })
        new = reduce_worktree(wt_state, event)
        assert len(new.blocks) == 1
        assert new.blocks[0].kind == "worktree_confirm"
        assert len(new.buttons) == 3
        action_ids = [b.action_id for b in new.buttons]
        assert ButtonIntent.WORKTREE_CONFIRM_START in action_ids
        assert ButtonIntent.WORKTREE_SHOW_MENU in action_ids
        assert ButtonIntent.WORKTREE_CANCEL in action_ids

    @pytest.mark.parametrize("units,expected_pct,has_retry", [
        # Running units → no retry, has stop
        ([{"name": "A", "status": "running"}], 0, False),
        # Mixed → retry_failed + stop
        ([{"name": "A", "status": "completed"}, {"name": "B", "status": "failed"}], 50, True),
        # All complete no failures → retry_all (special "no change" case)
        ([{"name": "A", "status": "completed"}, {"name": "B", "status": "completed"}], 100, True),
    ])
    def test_progress_buttons_and_pct(self, wt_state, units, expected_pct, has_retry):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_PROGRESS, payload={
            "units": units,
            "message": "progress",
        })
        new = reduce_worktree(wt_state, event)
        assert new.blocks[0].kind == "worktree_units"
        assert new.footer.progress_pct == expected_pct
        has_retry_btn = any(
            b.action_id in (ButtonIntent.WORKTREE_RETRY_FAILED, ButtonIntent.WORKTREE_RETRY_ALL)
            for b in new.buttons
        )
        assert has_retry_btn == has_retry

    def test_merge_sets_block_and_merge_button(self, wt_state):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_MERGE, payload={
            "merge_notes": [{"branch": "feat/a", "status": "ready"}],
            "base_branch": "main",
        })
        new = reduce_worktree(wt_state, event)
        assert len(new.blocks) == 1
        assert new.blocks[0].kind == "worktree_merge"
        assert new.blocks[0].data["base_branch"] == "main"
        assert any(b.action_id == ButtonIntent.WORKTREE_MERGE for b in new.buttons)

    def test_cleanup_summary_phase_buttons(self, wt_state):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_CLEANUP, payload={
            "merge_notes": [{"branch": "feat/a", "status": "merged"}],
            "base_branch": "main",
            "merge_results": [{"branch": "feat/a", "success": True}],
            "cleanup_phase": "summary",
        })
        new = reduce_worktree(wt_state, event)
        assert new.blocks[0].kind == "worktree_cleanup"
        action_ids = [b.action_id for b in new.buttons]
        assert ButtonIntent.WORKTREE_MERGE in action_ids
        assert ButtonIntent.WORKTREE_CANCEL in action_ids

    def test_cleanup_actions_phase_has_cleanup_button(self, wt_state):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.WORKTREE_CLEANUP, payload={
            "merge_notes": [{"branch": "feat/a", "status": "merged"}],
            "base_branch": "develop",
            "merge_results": [{"branch": "feat/a", "success": True}],
            "cleanup_phase": "actions",
        })
        new = reduce_worktree(wt_state, event)
        action_ids = [b.action_id for b in new.buttons]
        assert ButtonIntent.WORKTREE_CLEANUP in action_ids
        # Cleanup button should be danger type
        cleanup_btn = next(b for b in new.buttons if b.action_id == ButtonIntent.WORKTREE_CLEANUP)
        assert cleanup_btn.type == "danger"

    def test_unrelated_event_returns_unchanged(self, wt_state):
        from src.card.state.reducers.worktree import reduce_worktree
        event = CardEvent(type=CardEventType.STARTED)
        assert reduce_worktree(wt_state, event) is wt_state


# ==============================================================================
# engine_ext=None defense tests (AC7)
# ==============================================================================


class TestEngineExtNoneDefense:
    """Verify that reducers accessing engine_ext handle None gracefully."""

    def test_criteria_returns_state_when_engine_ext_none(self):
        state = CardState()  # engine_ext defaults to None
        event = CardEvent(type=CardEventType.CRITERIA_UPDATED, payload={
            "content": "test", "satisfied_count": 1, "total_count": 2,
        })
        result = reduce_criteria(state, event)
        assert result is state

    def test_phase_returns_state_when_engine_ext_none(self):
        state = CardState()
        event = CardEvent(type=CardEventType.PHASE_STARTED, payload={
            "cycle_num": 1, "phase": "planning",
        })
        result = reduce_phase(state, event)
        assert result is state

    def test_cycle_returns_state_when_engine_ext_none(self):
        state = CardState()
        event = CardEvent(type=CardEventType.CYCLE_STARTED, payload={
            "cycle_num": 1, "max_cycles": 3,
        })
        result = reduce_cycle(state, event)
        assert result is state

    def test_criteria_total_zero(self):
        """Edge case: criteria_total=0 should not crash."""
        ext = EngineExtState(criteria_satisfied=0, criteria_total=0)
        state = _base_state(engine_ext=ext)
        event = CardEvent(type=CardEventType.CRITERIA_UPDATED, payload={
            "content": "No criteria defined",
            "satisfied_count": 0,
            "total_count": 0,
        })
        new = reduce_criteria(state, event)
        assert new.engine_ext.criteria_total == 0
        assert new.engine_ext.criteria_satisfied == 0

    def test_phase_display_name_unknown_phase(self):
        """Unknown phase name should get default display '进行中'."""
        ext = EngineExtState(cycle_num=1)
        state = _base_state(engine_ext=ext)
        event = CardEvent(type=CardEventType.PHASE_STARTED, payload={
            "cycle_num": 1, "phase": "unknown_phase_xyz",
        })
        new = reduce_phase(state, event)
        assert "进行中" in new.footer.status_text


# ==============================================================================
# Sliding-window conditional gating tests
# ==============================================================================


class TestSlidingWindowGating:
    """Verify sliding-window trim only fires on TOOL_DONE/TOOL_FAILED events."""

    def _state_with_excess_completed_tools(self) -> CardState:
        """Create a state with >MAX_COMPLETED_TOOL_BLOCKS completed tools."""
        from src.card.state.reducer import MAX_COMPLETED_TOOL_BLOCKS
        from src.card.state.models import ToolBlock

        blocks = []
        for i in range(MAX_COMPLETED_TOOL_BLOCKS + 5):
            blocks.append(ToolBlock(
                kind="tool_call",
                block_id=f"tool_{i}",
                content=f"tool {i}",
                status="completed",
            ))
        # Add an active text block
        blocks.append(ContentBlock(kind="text", block_id="text_1", content="hello"))
        return _base_state(blocks=tuple(blocks))

    def test_text_delta_does_not_trigger_trim(self):
        """TEXT_DELTA on state with excess completed tools should NOT trim."""
        from src.card.state.reducer import reduce_card_state, MAX_COMPLETED_TOOL_BLOCKS

        state = self._state_with_excess_completed_tools()
        initial_block_count = len(state.blocks)

        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={
            "block_id": "text_1", "delta": " world",
        })
        new_state = reduce_card_state(state, event)
        # Blocks should NOT be trimmed — text delta doesn't trigger sliding window
        assert len(new_state.blocks) == initial_block_count

    def test_tool_done_triggers_trim(self):
        """TOOL_DONE on state with excess completed tools should trigger trim."""
        from src.card.state.reducer import reduce_card_state, MAX_COMPLETED_TOOL_BLOCKS
        from src.card.state.models import ToolBlock

        state = self._state_with_excess_completed_tools()
        # Add a running tool to complete
        running_tool = ToolBlock(kind="tool_call", block_id="tool_running", content="running", status="running")
        state = replace(state, blocks=state.blocks + (running_tool,))

        event = CardEvent(type=CardEventType.TOOL_DONE, payload={
            "block_id": "tool_running", "tool_output": "done",
        })
        new_state = reduce_card_state(state, event)
        # After TOOL_DONE, completed tools should be trimmed to MAX
        completed = [b for b in new_state.blocks if b.kind == "tool_call" and b.status == "completed"]
        assert len(completed) <= MAX_COMPLETED_TOOL_BLOCKS

    def test_tool_failed_triggers_trim(self):
        """TOOL_FAILED on state with excess completed tools should trigger trim."""
        from src.card.state.reducer import reduce_card_state, MAX_COMPLETED_TOOL_BLOCKS
        from src.card.state.models import ToolBlock

        state = self._state_with_excess_completed_tools()
        running_tool = ToolBlock(kind="tool_call", block_id="tool_running", content="running", status="running")
        state = replace(state, blocks=state.blocks + (running_tool,))

        event = CardEvent(type=CardEventType.TOOL_FAILED, payload={
            "block_id": "tool_running", "error": "timeout",
        })
        new_state = reduce_card_state(state, event)
        completed = [b for b in new_state.blocks if b.kind == "tool_call" and b.status == "completed"]
        assert len(completed) <= MAX_COMPLETED_TOOL_BLOCKS
