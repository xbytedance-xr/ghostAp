"""Tests for src/utils/text.py — format_duration, append_duration_to_title, generate_task_id."""

import re
import time

import pytest

from src.utils import text as text_utils
from src.utils.text import (
    append_duration_to_title,
    format_duration,
    generate_task_id,
    render_time_ago_cn,
    render_violation_report,
)
from src.utils.time_ago import compute_time_ago_bucket

# ──────────────────────────────────────────────────────────────────────
# format_duration
# ──────────────────────────────────────────────────────────────────────


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "0 秒"),
            (225, "3 分钟 45 秒"),
            (3661, "1 小时 1 分钟 1 秒"),
        ],
    )
    def test_format_duration(self, seconds, expected):
        assert format_duration(seconds) == expected


# ──────────────────────────────────────────────────────────────────────
# footer _format_duration (uses UI_TEXT templates, different from utils format_duration)
# ──────────────────────────────────────────────────────────────────────


class TestFooterFormatDuration:
    """Verify footer's _format_duration covers hours branch."""

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "< 1 秒"),
            (125, "2 分钟 5 秒"),
            (7261, "2 小时 1 分钟 1 秒"),
        ],
    )
    def test_footer_format_duration(self, seconds, expected):
        from src.card.render.footer import _format_duration
        assert _format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("优化 Deep 模式卡片", "优化 Deep 模式卡片"),
        ("  优化\nDeep\t模式卡片  ", "优化 Deep 模式卡片"),
        ("优化Deep模式消息卡片标题并展示用户问题", "优化Deep模式消息卡片标题…"),
        ("123456789012345", "123456789012345"),
        ("   ", "Deep 任务"),
        (None, "Deep 任务"),
    ],
)
def test_summarize_question_title(value, expected):
    result = text_utils.summarize_question_title(value)

    assert result == expected
    assert len(result) <= 15


# ──────────────────────────────────────────────────────────────────────
# compute_time_ago_bucket & render_time_ago_cn
# ──────────────────────────────────────────────────────────────────────


class TestComputeTimeAgoBucket:
    def test_minutes_bucket_range(self):
        assert compute_time_ago_bucket(60) == {"kind": "minutes", "value": 1}
        assert compute_time_ago_bucket(120) == {"kind": "minutes", "value": 2}
        assert compute_time_ago_bucket(3599) == {"kind": "minutes", "value": 59}

    def test_hours_bucket_range(self):
        assert compute_time_ago_bucket(3600) == {"kind": "hours", "value": 1}
        assert compute_time_ago_bucket(7200) == {"kind": "hours", "value": 2}

    def test_days_bucket_range(self):
        assert compute_time_ago_bucket(86400) == {"kind": "days", "value": 1}
        assert compute_time_ago_bucket(172800) == {"kind": "days", "value": 2}


class TestRenderTimeAgoCn:
    def test_render_seconds_bucket(self):
        assert render_time_ago_cn({"kind": "seconds", "value": 0}) == "刚刚"

    def test_render_minutes_bucket(self):
        assert render_time_ago_cn({"kind": "minutes", "value": 1}) == "1 分钟前"
        assert render_time_ago_cn({"kind": "minutes", "value": 3}) == "3 分钟前"

    def test_render_hours_bucket(self):
        assert render_time_ago_cn({"kind": "hours", "value": 1}) == "1 小时前"
        assert render_time_ago_cn({"kind": "hours", "value": 5}) == "5 小时前"

    def test_render_days_bucket(self):
        assert render_time_ago_cn({"kind": "days", "value": 1}) == "1 天前"
        assert render_time_ago_cn({"kind": "days", "value": 7}) == "7 天前"


# ──────────────────────────────────────────────────────────────────────
# format_time_ago
# ──────────────────────────────────────────────────────────────────────




# ──────────────────────────────────────────────────────────────────────
# render_violation_report
# ──────────────────────────────────────────────────────────────────────


class TestRenderViolationReport:
    def test_basic_structure_with_fix_note(self) -> None:
        title = "发现针对 session_key 的手工字符串解析反模式:"
        fix = "请改用统一的解析辅助函数。"
        lines = [
            "- src/foo.py:10: reason-one",
            "- src/bar.py:20: reason-two",
        ]

        rendered = render_violation_report(title, fix, lines)
        parts = rendered.splitlines()

        # 标题 → 空行 → 标签 → 修复说明 → 空行 → 违规列表
        assert parts[0] == title
        assert parts[1] == ""
        assert parts[2] == "【推荐修复方式】"
        assert parts[3] == fix
        assert parts[4] == ""
        assert parts[5:] == lines

    def test_without_fix_note_skips_fix_block(self) -> None:
        title = "发现针对 session_key 的手工字符串解析反模式:"
        lines = ["- src/foo.py:10: reason-one"]

        rendered = render_violation_report(title, "", lines)
        parts = rendered.splitlines()

        # 没有推荐修复文案时：仅包含标题 + 违规列表，无标签和多余空行
        assert parts[0] == title
        assert "【推荐修复方式】" not in rendered
        assert parts[1:] == lines

    def test_whitespace_fix_treated_as_empty(self) -> None:
        """recommended_fix 仅包含空白时应视为无修复文案。"""

        title = "发现针对 session_key 的手工字符串解析反模式:"
        fix = "  \n"
        lines = [
            "- src/foo.py:10: reason-one",
            "- src/bar.py:20: reason-two",
        ]

        rendered = render_violation_report(title, fix, lines)
        parts = rendered.splitlines()

        # 视为空文案：不渲染标签，不插入额外空行，直接标题 + 违规列表
        assert parts[0] == title
        assert parts[1:] == lines
        assert "【推荐修复方式】" not in rendered
        # 违规列表中不应混入空行
        assert "" not in parts[1:]

    def test_empty_violations_without_fix_block(self) -> None:
        """violation_lines 为空且无有效修复文案时，只渲染标题一行。"""

        title = "发现针对 session_key 的手工字符串解析反模式:"
        empty_violations: list[str] = []

        for fix in ("", None, "  \n"):
            rendered = render_violation_report(title, fix, empty_violations)
            parts = rendered.splitlines()

            assert parts == [title]
            assert "【推荐修复方式】" not in rendered

    def test_empty_violations_with_fix_has_no_trailing_blank(self) -> None:
        """violation_lines 为空但存在修复文案时，不应在末尾保留多余空行。"""

        title = "发现针对 session_key 的手工字符串解析反模式:"
        fix = "请改用统一的解析辅助函数。"
        empty_violations: list[str] = []

        rendered = render_violation_report(title, fix, empty_violations)
        parts = rendered.splitlines()

        # 标题 → 空行 → 标签 → 修复说明；无多余结尾空行
        assert parts[0] == title
        assert parts[1] == ""
        assert parts[2] == "【推荐修复方式】"
        assert parts[3] == fix
        assert len(parts) == 4
        assert parts[-1] != ""


# ──────────────────────────────────────────────────────────────────────
# append_duration_to_title
# ──────────────────────────────────────────────────────────────────────


class TestAppendDurationToTitle:
    def test_positive_duration_appended(self):
        result = append_duration_to_title("🔄 执行中", 225)
        assert result == "🔄 执行中 · 3 分钟 45 秒"

    def test_large_duration(self):
        result = append_duration_to_title("标题", 7261)
        assert result == "标题 · 2 小时 1 分钟 1 秒"




# ──────────────────────────────────────────────────────────────────────
# generate_task_id
# ──────────────────────────────────────────────────────────────────────


class TestGenerateTaskId:
    def test_format_structure(self):
        tid = generate_task_id("myproject")
        # Should be: myproject_YYYYMMDD_HHMMSS_XXXX
        parts = tid.split("_")
        assert len(parts) == 4, f"Expected 4 parts, got {parts}"
        assert parts[0] == "myproject"
        assert len(parts[1]) == 8  # YYYYMMDD
        assert len(parts[2]) == 6  # HHMMSS
        assert len(parts[3]) == 4  # hex suffix

    def test_hex_suffix_is_valid_hex(self):
        tid = generate_task_id("test")
        suffix = tid.split("_")[-1]
        int(suffix, 16)  # Should not raise

    def test_special_chars_sanitized(self):
        tid = generate_task_id("my-project/v2.0")
        name_part = tid.split("_")[0]
        assert re.match(r"^[a-zA-Z0-9_]+$", name_part), f"Name contains invalid chars: {name_part}"

    def test_long_name_truncated(self):
        long_name = "a" * 100
        tid = generate_task_id(long_name)
        name_part = tid.split("_")[0]
        assert len(name_part) <= 30

    def test_uniqueness(self):
        ids = {generate_task_id("proj") for _ in range(50)}
        # With 4-hex suffix, 50 IDs should all be unique
        assert len(ids) == 50

    def test_timestamp_is_current(self):
        before = time.strftime("%Y%m%d", time.localtime())
        tid = generate_task_id("test")
        date_part = tid.split("_")[1]
        after = time.strftime("%Y%m%d", time.localtime())
        assert date_part in (before, after)
