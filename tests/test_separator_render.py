"""Tests for separator reducer and atom rendering (AC-R17)."""
from __future__ import annotations

import pytest

from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardState, SeparatorBlock, CardMetadata, HeaderState, FooterState
from src.card.state.reducers.separator import reduce_separator
from src.card.render.atoms import _block_to_separator_atom


def _make_empty_state() -> CardState:
    return CardState(
        header=HeaderState(title="Test", subtitle="", template="turquoise"),
        metadata=CardMetadata(mode_name="Test", tool_name="test", model_name="gpt-4o"),
        footer=FooterState(status="thinking", status_text=""),
        blocks=(),
        buttons=(),
    )


class TestReduceSeparator:
    """Unit tests for reduce_separator sub-reducer."""

    def test_appends_separator_block_to_state(self):
        """SECTION_SEPARATOR event correctly appends SeparatorBlock."""
        state = _make_empty_state()
        event = CardEvent(
            type=CardEventType.SECTION_SEPARATOR,
            payload={"block_id": "sep_1", "task_name": "任务A", "is_first_overflow": False, "status_emoji": "🔄"},
        )
        new_state = reduce_separator(state, event)
        assert len(new_state.blocks) == 1
        block = new_state.blocks[0]
        assert isinstance(block, SeparatorBlock)
        assert block.task_name == "任务A"
        assert block.is_first_overflow is False
        assert block.status_emoji == "🔄"
        assert block.block_id == "sep_1"
        assert block.element_id == "el_sep_1"
        assert block.status == "completed"

    def test_is_first_overflow_true(self):
        """is_first_overflow=True is preserved in the block."""
        state = _make_empty_state()
        event = CardEvent(
            type=CardEventType.SECTION_SEPARATOR,
            payload={"block_id": "sep_x", "task_name": "First", "is_first_overflow": True, "status_emoji": "⏳"},
        )
        new_state = reduce_separator(state, event)
        assert new_state.blocks[0].is_first_overflow is True

    def test_empty_task_name(self):
        """Empty task_name does not crash and stores empty string."""
        state = _make_empty_state()
        event = CardEvent(
            type=CardEventType.SECTION_SEPARATOR,
            payload={"block_id": "sep_e", "task_name": "", "is_first_overflow": False, "status_emoji": "⏳"},
        )
        new_state = reduce_separator(state, event)
        assert new_state.blocks[0].task_name == ""

    def test_default_status_emoji_when_missing(self):
        """Missing status_emoji defaults to ⏳."""
        state = _make_empty_state()
        event = CardEvent(
            type=CardEventType.SECTION_SEPARATOR,
            payload={"block_id": "sep_d", "task_name": "X"},
        )
        new_state = reduce_separator(state, event)
        assert new_state.blocks[0].status_emoji == "⏳"

    def test_ignores_non_separator_events(self):
        """Non-SECTION_SEPARATOR events pass through unchanged."""
        state = _make_empty_state()
        event = CardEvent(type=CardEventType.TEXT_STARTED, payload={"block_id": "b1"})
        result = reduce_separator(state, event)
        assert result is state  # unchanged


class TestBlockToSeparatorAtom:
    """Unit tests for _block_to_separator_atom rendering."""

    def test_is_first_overflow_true_uses_first_template(self):
        """is_first_overflow=True uses orch_overflow_separator_first template."""
        block = SeparatorBlock(
            block_id="sep_1", task_name="重构模块",
            is_first_overflow=True, status_emoji="🔄",
        )
        atom = _block_to_separator_atom(block)
        assert atom.kind == "text"
        assert atom.splittable is False
        assert "以下任务合并展示" in atom.content
        assert "重构模块" in atom.content
        assert "🔄" in atom.content

    def test_is_first_overflow_false_uses_normal_template(self):
        """is_first_overflow=False uses orch_overflow_separator template."""
        block = SeparatorBlock(
            block_id="sep_2", task_name="修复Bug",
            is_first_overflow=False, status_emoji="✅",
        )
        atom = _block_to_separator_atom(block)
        assert atom.kind == "text"
        assert "以下任务合并展示" not in atom.content
        assert "修复Bug" in atom.content
        assert "✅" in atom.content

    def test_empty_task_name_no_empty_bold(self):
        """Empty task_name does not produce empty bold markers **  **."""
        block = SeparatorBlock(
            block_id="sep_3", task_name="",
            is_first_overflow=False, status_emoji="⏳",
        )
        atom = _block_to_separator_atom(block)
        # Should not have "** **" (empty bold)
        assert "** **" not in atom.content
        assert atom.byte_size > 0

    def test_default_status_emoji(self):
        """Default status_emoji (⏳) renders correctly."""
        block = SeparatorBlock(block_id="sep_4", task_name="Test")
        atom = _block_to_separator_atom(block)
        assert "⏳" in atom.content

    def test_atom_has_correct_block_id(self):
        """Atom preserves the block_id from the block."""
        block = SeparatorBlock(block_id="my_sep", task_name="X", status_emoji="❌")
        atom = _block_to_separator_atom(block)
        assert atom.block_id == "my_sep"

    def test_atom_node_count_is_one(self):
        """Separator atom has node_count=1."""
        block = SeparatorBlock(block_id="s", task_name="Y", status_emoji="⏳")
        atom = _block_to_separator_atom(block)
        assert atom.node_count == 1


class TestOverflowFoldingInOrchestrator:
    """AC-3: overflow tasks > 2 are folded with collapsed notice."""

    def test_four_overflow_tasks_only_two_separators(self):
        """With 4 overflow tasks, only 2 get full SECTION_SEPARATOR; rest get collapsed."""
        import threading
        from src.card.orchestrator import TaskOrchestrator
        from src.card.task_registry import TaskRegistry

        dispatched: list[CardEvent] = []
        dispatch_lock = threading.Lock()

        class TrackingSession:
            def __init__(self, session_id=""):
                self.session_id = session_id
                self.dispatched_events: list[CardEvent] = []
                self._lock = threading.Lock()
                self.on_first_deliver = None
                self.delivered_message_id = ""
                self._hooks: list = []

            def add_hook(self, hook):
                self._hooks.append(hook)

            def dispatch(self, event: CardEvent):
                with self._lock:
                    self.dispatched_events.append(event)
                with dispatch_lock:
                    dispatched.append(event)

            @property
            def event_count(self):
                with self._lock:
                    return len(self.dispatched_events)

        sessions: dict[str, TrackingSession] = {}

        def creator(task_id: str):
            s = TrackingSession(session_id=f"s_{task_id}")
            sessions[task_id] = s
            return s

        registry = TaskRegistry()
        # max_task_cards=2 so tasks 3-6 are overflow (4 overflow tasks)
        orch = TaskOrchestrator(
            chat_id="test",
            session_creator=creator,
            registry=registry,
            max_task_cards=2,
        )
        thinking = TrackingSession("thinking")
        orch.set_thinking_session(thinking)

        tasks = [
            {"task_id": f"t{i}", "name": f"Task{i}"}
            for i in range(6)  # 6 tasks, max_cards=2, so 4 overflow
        ]
        orch.on_plan_received(tasks)

        # Dispatch to each overflow task to trigger separators
        overflow_ids = ["t2", "t3", "t4", "t5"]
        for tid in overflow_ids:
            orch.dispatch_to_task(tid, CardEvent.text_delta(f"b_{tid}", f"msg_{tid}"))

        # Check the last session (t1) which receives all overflow
        last_session = sessions["t1"]
        all_events = last_session.dispatched_events

        # Count SECTION_SEPARATOR events (full separator with 📎)
        sep_events = [e for e in all_events if e.type == CardEventType.SECTION_SEPARATOR]
        assert len(sep_events) <= 2, f"Expected ≤2 separators, got {len(sep_events)}"

        # Check for collapsed notice (TEXT_DELTA with "…及" or "项更多")
        text_deltas = [
            e.payload.get("text", "")
            for e in all_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        combined_text = "".join(text_deltas)
        assert "项更多" in combined_text, (
            f"Expected collapsed '…及 N 项更多' notice, got: {combined_text}"
        )
