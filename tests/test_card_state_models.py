"""Tests for card state models."""
import pytest
from dataclasses import replace
from src.card.state.models import (
    CardState, ContentBlock, HeaderState, FooterState,
    CardMetadata, ButtonSpec,
)


class TestCardState:
    def test_frozen(self):
        s = CardState()
        with pytest.raises(Exception):
            s.version = 99

    def test_defaults(self):
        s = CardState()
        assert s.blocks == ()
        assert s.terminal == "running"
        assert s.version == 0

    def test_replace_produces_new(self):
        s = CardState()
        s2 = replace(s, version=1)
        assert s.version == 0
        assert s2.version == 1
        assert s is not s2


class TestContentBlock:
    def test_text_block(self):
        b = ContentBlock(kind="text", block_id="b1", content="hello")
        assert b.kind == "text"
        assert b.content == "hello"

    def test_tool_block(self):
        b = ContentBlock(kind="tool_call", block_id="t1", tool_name="bash", tool_input="ls")
        assert b.tool_name == "bash"

    def test_reasoning_block(self):
        b = ContentBlock(kind="reasoning", block_id="r1", char_count=100)
        assert b.char_count == 100

    def test_plan_block(self):
        b = ContentBlock(kind="plan", block_id="p1", content="step1\nstep2")
        assert b.kind == "plan"


class TestMetadata:
    def test_all_fields(self):
        m = CardMetadata(
            project_name="test",
            mode_name="Deep Agent",
            mode_emoji="🧠",
            tool_name="coco",
            model_name="gpt-4o",
            engine_type="deep",
        )
        assert m.project_name == "test"
        assert m.tool_name == "coco"

    def test_defaults(self):
        m = CardMetadata()
        assert m.mode_name == "Coco"
        assert m.mode_emoji == "🤖"
        assert m.engine_type is None


class TestHeaderState:
    def test_defaults(self):
        h = HeaderState()
        assert h.template == "blue"
        assert h.subtitle is None


class TestButtonSpec:
    def test_creation(self):
        b = ButtonSpec(text="Stop", action_id="stop", type="danger")
        assert b.type == "danger"
