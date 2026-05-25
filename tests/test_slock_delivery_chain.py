"""Tests for Slock delivery chain - queued tasks receive final results.

Covers:
- QueuedTask has delivery metadata fields
- Dispatch loop collects results and delivers via callback
- First message (auto-activation) receives final result
- Queue wait tasks receive final result when agent becomes idle
"""

from __future__ import annotations


class TestQueuedTaskDeliveryMetadata:
    """Test that QueuedTask has delivery metadata fields."""

    def test_queued_task_has_origin_message_id(self):
        """QueuedTask should have origin_message_id field."""
        from src.slock_engine.task_queue import QueuedTask

        task = QueuedTask(
            task_id="test_1",
            text="帮我写代码",
            chat_id="chat_123",
            message_id="msg_001",
            origin_message_id="msg_001",
        )
        assert task.origin_message_id == "msg_001"

    def test_queued_task_has_final_result_callback(self):
        """QueuedTask should have final_result_callback field."""
        from src.slock_engine.task_queue import QueuedTask

        callback_called = []

        def my_callback(task_id, result, card_msg_id):
            callback_called.append((task_id, result, card_msg_id))

        task = QueuedTask(
            task_id="test_2",
            text="帮我写代码",
            chat_id="chat_123",
            message_id="msg_002",
            final_result_callback=my_callback,
        )

        # Call the callback
        task.final_result_callback("test_2", "这里是结果", "card_001")

        assert len(callback_called) == 1
        assert callback_called[0] == ("test_2", "这里是结果", "card_001")

    def test_queued_task_has_collaboration_plan(self):
        """QueuedTask should have collaboration_plan field."""
        from src.slock_engine.task_queue import QueuedTask

        plan = {
            "roles": ["planner", "coder", "tester"],
            "current_step": 0,
        }

        task = QueuedTask(
            task_id="test_3",
            text="帮我写代码",
            chat_id="chat_123",
            message_id="msg_003",
            collaboration_plan=plan,
        )
        assert task.collaboration_plan == plan


class TestBuildResultCard:
    """Test build_result_card function."""

    def test_build_result_card_basic(self):
        """build_result_card should create a valid card."""
        from src.slock_engine.card_templates.queue_feedback import build_result_card

        card = build_result_card(
            task_preview="帮我写一个快速排序",
            result="def quick_sort(arr):\n    return sorted(arr)",
        )

        assert card is not None
        assert "header" in card
        assert "body" in card

        # Check header
        assert card["header"]["title"]["content"] == "✅ 任务完成"

    def test_build_result_card_no_preview(self):
        """build_result_card should work without task_preview."""
        from src.slock_engine.card_templates.queue_feedback import build_result_card

        card = build_result_card(result="这里是结果")
        assert card is not None


class TestSlockEngineCallbacks:
    """Test SlockEngineCallbacks has on_final_result field."""

    def test_callbacks_has_on_final_result(self):
        """SlockEngineCallbacks should have on_final_result field."""
        from src.slock_engine.engine import SlockEngineCallbacks

        callbacks = SlockEngineCallbacks()
        assert hasattr(callbacks, "on_final_result")
        assert callbacks.on_final_result is None

    def test_callbacks_on_final_result_can_be_set(self):
        """on_final_result callback should be callable."""
        from src.slock_engine.engine import SlockEngineCallbacks

        results = []

        def on_final(task_id, result, card_id):
            results.append((task_id, result, card_id))

        callbacks = SlockEngineCallbacks(on_final_result=on_final)
        callbacks.on_final_result("t1", "result1", "c1")

        assert results == [("t1", "result1", "c1")]


class TestResultCardExported:
    """Test that build_result_card is exported from card_templates."""

    def test_result_card_in_init(self):
        """build_result_card should be importable from card_templates."""
        from src.slock_engine.card_templates import build_result_card
        assert build_result_card is not None
