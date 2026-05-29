"""Tests for sidebar_channel.py — lightweight inter-agent communication."""
import time
import pytest
from src.slock_engine.sidebar_channel import (
    SidebarChannel, SidebarMessage, SidebarMsgType, _SIDEBAR_PATTERN
)


class TestSidebarMessage:
    def test_ttl_expiry(self):
        """Message expires after TTL."""
        msg = SidebarMessage(
            sender_id="a", sender_name="Coder", recipient_id="b",
            msg_type=SidebarMsgType.FYI, content="test",
            ttl_seconds=0.01,
        )
        time.sleep(0.02)
        assert msg.is_expired

    def test_not_expired_within_ttl(self):
        """Message is not expired within TTL."""
        msg = SidebarMessage(
            sender_id="a", sender_name="Coder", recipient_id="b",
            msg_type=SidebarMsgType.FYI, content="test",
            ttl_seconds=60.0,
        )
        assert not msg.is_expired


class TestSidebarChannel:
    def test_post_and_consume(self):
        """Post a message and consume it."""
        ch = SidebarChannel()
        msg = SidebarMessage(
            sender_id="a", sender_name="Coder", recipient_id="b",
            msg_type=SidebarMsgType.FYI, content="heads up",
        )
        assert ch.post(msg) is True
        pending = ch.get_pending("b")
        assert len(pending) == 1
        assert pending[0].content == "heads up"
        # Consumed — second get returns empty
        assert ch.get_pending("b") == []

    def test_rate_limiting(self):
        """Rate limit: max 3 messages per 5 min per sender."""
        ch = SidebarChannel()
        for i in range(3):
            msg = SidebarMessage(
                sender_id="a", sender_name="Coder", recipient_id="b",
                msg_type=SidebarMsgType.FYI, content=f"msg {i}",
            )
            assert ch.post(msg) is True
        # 4th should be rate-limited
        msg4 = SidebarMessage(
            sender_id="a", sender_name="Coder", recipient_id="b",
            msg_type=SidebarMsgType.FYI, content="msg 4",
        )
        assert ch.post(msg4) is False

    def test_max_pending_cap(self):
        """Inbox is capped at max_pending_per_agent."""
        ch = SidebarChannel(max_pending_per_agent=2)
        for i in range(3):
            msg = SidebarMessage(
                sender_id=f"sender{i}", sender_name=f"S{i}", recipient_id="b",
                msg_type=SidebarMsgType.FYI, content=f"msg {i}",
            )
            ch.post(msg)
        # Only last 2 should be in inbox (deque maxlen=2)
        pending = ch.get_pending("b")
        assert len(pending) == 2

    def test_expire_stale(self):
        """expire_stale removes expired messages."""
        ch = SidebarChannel()
        msg = SidebarMessage(
            sender_id="a", sender_name="Coder", recipient_id="b",
            msg_type=SidebarMsgType.FYI, content="old",
            ttl_seconds=0.01,
        )
        ch.post(msg)
        time.sleep(0.02)
        removed = ch.expire_stale()
        assert removed == 1
        assert ch.get_pending("b") == []

    def test_different_recipients_independent(self):
        """Messages to different recipients don't interfere."""
        ch = SidebarChannel()
        ch.post(SidebarMessage(sender_id="a", sender_name="A", recipient_id="b", msg_type=SidebarMsgType.FYI, content="for b"))
        ch.post(SidebarMessage(sender_id="a", sender_name="A", recipient_id="c", msg_type=SidebarMsgType.FYI, content="for c"))
        assert len(ch.get_pending("b")) == 1
        assert len(ch.get_pending("c")) == 1


class TestSidebarParsing:
    def test_parse_fyi_marker(self):
        """Parse [FYI:@Name] marker from output."""
        output = "Here is my analysis.\n[FYI:@Reviewer] 我发现了一个潜在问题，你可能想看看"
        cleaned, markers = SidebarChannel.parse_output_markers(output)
        assert len(markers) == 1
        assert markers[0] == ("FYI", "Reviewer", "我发现了一个潜在问题，你可能想看看")
        assert "[FYI:" not in cleaned

    def test_parse_question_marker(self):
        """Parse [QUESTION:@Name] marker."""
        output = "[QUESTION:@Coder] 这个接口的超时时间是多少？"
        cleaned, markers = SidebarChannel.parse_output_markers(output)
        assert len(markers) == 1
        assert markers[0][0] == "QUESTION"

    def test_parse_multiple_markers(self):
        """Parse multiple sidebar markers."""
        output = "Done.\n[FYI:@Coder] 代码已就绪\n[OFFER:@Tester] 需要我帮你写测试吗"
        cleaned, markers = SidebarChannel.parse_output_markers(output)
        assert len(markers) == 2

    def test_no_markers_returns_original(self):
        """No markers returns original text unchanged."""
        output = "Normal output with no markers."
        cleaned, markers = SidebarChannel.parse_output_markers(output)
        assert cleaned == output
        assert markers == []

    def test_long_content_truncated(self):
        """Content over 500 chars is not captured."""
        long_content = "x" * 600
        output = f"[FYI:@Name] {long_content}"
        _, markers = SidebarChannel.parse_output_markers(output)
        assert len(markers) == 0  # Too long, skipped


class TestSidebarRendering:
    def test_render_pending_for_prompt(self):
        """Render pending messages as prompt section."""
        ch = SidebarChannel()
        ch.post(SidebarMessage(sender_id="a", sender_name="Coder", recipient_id="b", msg_type=SidebarMsgType.FYI, content="check this"))
        ch.post(SidebarMessage(sender_id="c", sender_name="Tester", recipient_id="b", msg_type=SidebarMsgType.QUESTION, content="需要测试吗？"))
        rendered = ch.render_pending_for_prompt("b")
        assert "# Sidebar Messages" in rendered
        assert "[FYI from @Coder]" in rendered
        assert "[Q from @Tester]" in rendered
        assert "队友的非正式消息" in rendered

    def test_render_empty_returns_empty(self):
        """No pending messages returns empty string."""
        ch = SidebarChannel()
        assert ch.render_pending_for_prompt("nobody") == ""
