"""Tests for src/card/nav_link.py — full branch coverage."""
import pytest
from src.card.nav_link import (
    format_back_link,
    format_navigation_link,
    format_task_continuation_link,
)


class TestFormatNavigationLink:
    """format_navigation_link: session-level rotation deep-link."""

    def test_with_msg_id_returns_deep_link_and_no_fallback(self):
        nav, fallback = format_navigation_link(
            new_msg_id="om_abc123", rotation_seq=2
        )
        assert "lark://message/om_abc123" in nav
        assert fallback is None

    def test_with_msg_id_includes_seq_info(self):
        nav, _ = format_navigation_link(
            new_msg_id="om_test", rotation_seq=3
        )
        assert "3" in nav

    def test_without_msg_id_returns_fallback_notice(self):
        nav, fallback = format_navigation_link(
            new_msg_id=None, rotation_seq=1
        )
        assert "lark://message/" not in nav
        assert fallback is not None
        assert "下方" in fallback

    def test_empty_string_msg_id_treated_as_missing(self):
        nav, fallback = format_navigation_link(
            new_msg_id="", rotation_seq=1
        )
        assert "lark://message/" not in nav
        assert fallback is not None

    def test_nav_uses_pian_terminology(self):
        """Navigation text uses '第 N 篇' for continuation."""
        nav, _ = format_navigation_link(new_msg_id="om_x", rotation_seq=2)
        assert "第 2 篇" in nav

    def test_no_sid_short_in_output(self):
        """No technical session ID leaks into user-facing text."""
        nav, fallback = format_navigation_link(new_msg_id=None, rotation_seq=1)
        assert "会话" not in nav
        assert "会话" not in (fallback or "")


class TestFormatTaskContinuationLink:
    """format_task_continuation_link: task-level rotation text with page number."""

    def test_with_msg_id_returns_deep_link(self):
        result = format_task_continuation_link(
            task_name="修复登录", rotation_count=2, new_msg_id="om_xyz"
        )
        assert "lark://message/om_xyz" in result
        assert "修复登录" in result
        # page = rotation_count + 1 = 3
        assert "第 3 篇" in result

    def test_page_number_is_rotation_plus_one(self):
        """Verify page = rotation_count + 1 (first card = page 1, first rotation = page 2)."""
        result = format_task_continuation_link(
            task_name="测试", rotation_count=1, new_msg_id="om_abc"
        )
        assert "第 2 篇" in result

    def test_without_msg_id_returns_fallback_text(self):
        result = format_task_continuation_link(
            task_name="重构模块", rotation_count=1, new_msg_id=None
        )
        assert "lark://message/" not in result
        assert "重构模块" in result
        assert "请查看下方最新卡片" in result
        assert "第 2 篇" in result

    def test_empty_string_msg_id_treated_as_missing(self):
        result = format_task_continuation_link(
            task_name="测试任务", rotation_count=3, new_msg_id=""
        )
        assert "lark://message/" not in result

    def test_uses_down_arrow_symbol(self):
        """Task continuation uses ↓ symbol for forward navigation."""
        result = format_task_continuation_link(
            task_name="X", rotation_count=1, new_msg_id="om_y"
        )
        assert "↓" in result


class TestFormatBackLink:
    """format_back_link: reverse deep-link to previous card."""

    def test_with_msg_id_returns_back_link(self):
        result = format_back_link("om_old_card")
        assert result is not None
        assert "lark://message/om_old_card" in result

    def test_without_msg_id_returns_none(self):
        assert format_back_link(None) is None

    def test_empty_string_returns_none(self):
        assert format_back_link("") is None

    def test_with_task_name_includes_task_name(self):
        """Back link with task_name uses the task-specific template."""
        result = format_back_link("om_old_card", task_name="修复登录")
        assert result is not None
        assert "修复登录" in result
        assert "lark://message/om_old_card" in result

    def test_without_task_name_uses_generic_text(self):
        """Back link without task_name uses generic '前篇' text."""
        result = format_back_link("om_old_card", task_name=None)
        assert result is not None
        assert "lark://message/om_old_card" in result
        assert "前篇" in result

    def test_uses_up_arrow_symbol(self):
        """Back link uses ↑ symbol for backward navigation."""
        result = format_back_link("om_x")
        assert "↑" in result
