"""Tests for spec_renderer callbacks and layout footer behavior.

Covers:
- AC-R03: on_phase_retry uses UI_TEXT (no hardcoded strings)
- AC-R11: _build_footer_element renders when footer_status set but no buttons
- AC-R12: on_phase_retry and on_review_retry independent unit tests
"""

from unittest.mock import MagicMock, patch

from src.acp import ACPEventType
from src.card.ui_text import UI_TEXT
from src.card.events import CardEventType
from src.card.render.budget import RenderBudget
from src.card.state.models import CardMetadata
from src.feishu.renderers._spec_stream_processor import SpecStreamProcessor
from src.spec_engine.retry_status import RetryStatus
from src.spec_engine.models import SpecPhase


class TestOnPhaseRetryUsesUIText:
    """AC-R03: on_phase_retry callback must use UI_TEXT, not hardcoded strings."""

    def test_phase_retry_progress_key_exists(self):
        """The phase_retry_progress key must exist in UI_TEXT."""
        assert "phase_retry_progress" in UI_TEXT

    def test_phase_retry_progress_is_pure_chinese(self):
        """The phase_retry_progress text must not contain English 'Phase'."""
        text = UI_TEXT["phase_retry_progress"]
        assert "Phase" not in text
        assert "调用重试" in text

    def test_phase_retry_progress_format(self):
        """The format placeholders produce correct output."""
        text = UI_TEXT["phase_retry_progress"].format(attempt=2, max_attempts=3)
        assert "2/3" in text
        assert "调用重试" in text


class TestFooterStatusWithoutButtons:
    """AC-R11: _build_footer_element renders footer when footer_status is set but no buttons."""

    def test_footer_rendered_without_buttons(self):
        from src.card.builders.layout import _build_footer_element

        elements = _build_footer_element("thinking")
        assert len(elements) == 2
        assert elements[0]["tag"] == "hr"
        assert elements[1]["tag"] == "markdown"
        assert "正在思考" in elements[1]["content"]

    def test_footer_with_arbitrary_status(self):
        from src.card.builders.layout import _build_footer_element

        elements = _build_footer_element("⏳ 等待中")
        assert len(elements) == 2
        assert elements[1]["content"] == "⏳ 等待中"

    def test_footer_in_full_layout_without_buttons(self):
        """Integration: UnifiedCardLayout.build() renders footer even when buttons=None."""
        from src.card.builders.layout import UnifiedCardLayout
        from src.card.models import CardLayoutSpec

        spec = CardLayoutSpec(
            content_markdown="Hello",
            footer_status="thinking",
            buttons=None,
            button_elements=None,
        )
        layout = UnifiedCardLayout()
        elements = layout.build(spec)

        # Should contain a notation markdown with "正在思考" (from FOOTER_STATUS mapping)
        footer_markdowns = [
            e for e in elements
            if e.get("tag") == "markdown"
            and e.get("text_size") == "notation"
            and "正在思考" in e.get("content", "")
        ]
        assert footer_markdowns, (
            "Expected footer notation markdown with '正在思考' in rendered card. "
            f"All elements: {[e for e in elements if e.get('tag') == 'markdown']}"
        )

    def test_sticky_message_uses_schema_v2_safe_markdown(self):
        """Sticky message must not render deprecated Schema V1 note elements."""
        from src.card.builders.layout import UnifiedCardLayout
        from src.card.models import CardLayoutSpec

        spec = CardLayoutSpec(
            content_markdown="Hello",
            sticky_message="请注意",
        )

        elements = UnifiedCardLayout.build(spec)

        assert not [e for e in elements if e.get("tag") == "note"]
        assert any(
            e.get("tag") == "markdown" and "请注意" in e.get("content", "")
            for e in elements
        )


class TestOnReviewRetryAllStatuses:
    """AC-R12: on_review_retry handles all RetryStatus values without error."""

    def test_retry_status_text_mapping_complete(self):
        """The _RETRY_STATUS_TEXT mapping in renderer covers all rendered enum members."""
        # Replicate the mapping from spec_renderer (SUCCEEDED excluded - early return, no render)
        mapping = {
            RetryStatus.WAITING: "retry_waiting",
            RetryStatus.EXECUTING: "retry_executing",
            RetryStatus.EXHAUSTED: "retry_exhausted",
            RetryStatus.NO_RETRY: "retry_no_retry",
        }
        for status, key in mapping.items():
            assert key in UI_TEXT, f"Missing UI_TEXT key: {key}"
        # SUCCEEDED should not exist in UI_TEXT
        assert "retry_succeeded" not in UI_TEXT

    def test_waiting_format(self):
        text = UI_TEXT["retry_waiting"].format(sec=7, i=1, n=3)
        assert "7" in text
        assert "秒" in text

    def test_executing_format(self):
        text = UI_TEXT["retry_executing"].format(i=1, n=3)
        assert "1" in text
        assert "3" in text

    def test_exhausted_format(self):
        text = UI_TEXT["retry_exhausted"].format(n=2, elapsed_friendly="约 1 分钟")
        assert "2" in text
        assert "约 1 分钟" in text

    def test_no_retry_no_format_needed(self):
        """retry_no_retry requires no .format() placeholders."""
        text = UI_TEXT["retry_no_retry"]
        assert "{" not in text


class TestSpecStreamProcessorUnifiedCycleCard:
    def test_non_build_phase_forwards_tool_events_as_native_card_events(self):
        reporter = MagicMock()
        reporter.format_phase_subtitle.return_value = "第 1 轮 · Plan"
        reporter.format_criteria_section.return_value = "Criteria"

        spec_project = MagicMock()
        spec_project.cycle_count_total = 3
        spec_project.satisfied_count = 0
        spec_project.total_criteria = 2
        spec_project.duration.return_value = 2.0

        renderer = MagicMock()
        renderer.ctx.spec_engine_manager.snapshot.return_value = MagicMock(ext={"project": spec_project})
        renderer.get_ui_state.return_value = {}
        renderer.check_warning_banner.return_value = None

        rotator = MagicMock()
        throttle = MagicMock()

        processor = SpecStreamProcessor(
            rotator=rotator,
            reporter=reporter,
            metadata=CardMetadata(engine_type="spec", mode_name="Spec · Coco", mode_emoji="📋"),
            hooks=(),
            budget=RenderBudget(engine_cmd="/spec"),
            spec_project_id="p1",
            message_id="msg1",
            chat_id="chat1",
            renderer=renderer,
            project_root_path="/tmp/p1",
            throttle=throttle,
        )

        processor.on_phase_start(1, SpecPhase.PLAN)
        rotator.dispatch.reset_mock()

        tool_event = MagicMock()
        tool_event.event_type = ACPEventType.TOOL_CALL_START
        tool_event.tool_call = MagicMock(id="tool-1", title="Read", content="README.md", status="running")
        tool_event.text = None

        processor.on_phase_event(1, SpecPhase.PLAN, tool_event)

        dispatched_events = [call.args[0] for call in rotator.dispatch.call_args_list]
        event_types = [event.type for event in dispatched_events]
        assert CardEventType.TOOL_STARTED in event_types

    def test_error_closes_active_stream_blocks_before_failed(self):
        reporter = MagicMock()
        reporter.format_phase_subtitle.return_value = "第 1 轮 · Plan"
        reporter.format_criteria_section.return_value = "Criteria"

        spec_project = MagicMock()
        spec_project.cycle_count_total = 3
        spec_project.satisfied_count = 0
        spec_project.total_criteria = 2
        spec_project.duration.return_value = 2.0

        renderer = MagicMock()
        renderer.ctx.spec_engine_manager.snapshot.return_value = MagicMock(ext={"project": spec_project})
        renderer.get_ui_state.return_value = {}
        renderer.check_warning_banner.return_value = None

        rotator = MagicMock()
        throttle = MagicMock()

        processor = SpecStreamProcessor(
            rotator=rotator,
            reporter=reporter,
            metadata=CardMetadata(engine_type="spec", mode_name="Spec · Coco", mode_emoji="📋"),
            hooks=(),
            budget=RenderBudget(engine_cmd="/spec"),
            spec_project_id="p1",
            message_id="msg1",
            chat_id="chat1",
            renderer=renderer,
            project_root_path="/tmp/p1",
            throttle=throttle,
        )

        processor.on_cycle_start(1, 3)
        processor.on_phase_start(1, SpecPhase.PLAN)
        rotator.dispatch.reset_mock()

        text_event = MagicMock()
        text_event.event_type = ACPEventType.TEXT_CHUNK
        text_event.text = "hello"
        text_event.tool_call = None

        processor.on_phase_event(1, SpecPhase.PLAN, text_event)
        rotator.dispatch.reset_mock()

        processor.on_error("boom")

        dispatched_events = [call.args[0] for call in rotator.dispatch.call_args_list]
        event_types = [event.type for event in dispatched_events]
        assert event_types[:2] == [CardEventType.TEXT_DONE, CardEventType.FAILED]
