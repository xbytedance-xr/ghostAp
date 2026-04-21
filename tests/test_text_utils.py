"""Tests for src/utils/text.py — format_duration, append_duration_to_title, generate_task_id."""

import re
import time

from src.utils.text import (
    append_duration_to_title,
    format_duration,
    format_idle_health,
    format_seconds_ago,
    format_time_ago,
    format_time_ago_from_bucket,
    generate_task_id,
    render_time_ago_cn,
    render_violation_report,
)
from src.utils.time_ago import IdleHealth, compute_time_ago_bucket

# ──────────────────────────────────────────────────────────────────────
# format_duration
# ──────────────────────────────────────────────────────────────────────


class TestFormatDuration:
    def test_zero(self):
        assert format_duration(0) == "0秒"

    def test_fractional_rounds_down(self):
        assert format_duration(0.9) == "0秒"

    def test_seconds_only(self):
        assert format_duration(5) == "5秒"
        assert format_duration(59) == "59秒"

    def test_minutes_boundary(self):
        assert format_duration(60) == "1分0秒"

    def test_minutes_and_seconds(self):
        assert format_duration(225) == "3分45秒"

    def test_just_under_one_hour(self):
        assert format_duration(3599) == "59分59秒"

    def test_one_hour_boundary(self):
        assert format_duration(3600) == "1小时0分0秒"

    def test_hours_minutes_seconds(self):
        assert format_duration(3661) == "1小时1分1秒"

    def test_large_value(self):
        # 24 hours
        assert format_duration(86400) == "24小时0分0秒"

    def test_negative_clamped_to_zero(self):
        assert format_duration(-10) == "0秒"

    def test_very_small_positive(self):
        assert format_duration(0.001) == "0秒"


# ──────────────────────────────────────────────────────────────────────
# compute_time_ago_bucket & render_time_ago_cn
# ──────────────────────────────────────────────────────────────────────


class TestComputeTimeAgoBucket:
    def test_seconds_bucket_for_small_and_negative_values(self):
        assert compute_time_ago_bucket(0) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket(-5) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket(1) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket(59) == {"kind": "seconds", "value": 0}

    def test_minutes_bucket_range(self):
        assert compute_time_ago_bucket(60) == {"kind": "minutes", "value": 1}
        # 1 分钟多一点仍按 1 分钟前处理
        assert compute_time_ago_bucket(119) == {"kind": "minutes", "value": 1}
        assert compute_time_ago_bucket(120) == {"kind": "minutes", "value": 2}
        assert compute_time_ago_bucket(3599) == {"kind": "minutes", "value": 59}

    def test_hours_bucket_range(self):
        assert compute_time_ago_bucket(3600) == {"kind": "hours", "value": 1}
        assert compute_time_ago_bucket(7200) == {"kind": "hours", "value": 2}
        # 恰好 23 小时
        assert compute_time_ago_bucket(23 * 3600) == {"kind": "hours", "value": 23}

    def test_days_bucket_range(self):
        # 恰好 24 小时
        assert compute_time_ago_bucket(86400) == {"kind": "days", "value": 1}
        # 超过 24 小时按天取整
        assert compute_time_ago_bucket(172800) == {"kind": "days", "value": 2}

    def test_non_numeric_input_falls_back_to_seconds(self):
        assert compute_time_ago_bucket(None) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket("not-a-number") == {"kind": "seconds", "value": 0}


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


class TestFormatTimeAgo:
    def test_just_now_for_zero_and_negative(self):
        assert format_time_ago(0) == "刚刚"
        assert format_time_ago(-5) == "刚刚"

    def test_under_one_minute_is_just_now(self):
        assert format_time_ago(1) == "刚刚"
        assert format_time_ago(59) == "刚刚"

    def test_minutes_range(self):
        assert format_time_ago(60) == "1 分钟前"
        # 1 分钟多一点仍按 1 分钟前处理
        assert format_time_ago(119) == "1 分钟前"
        assert format_time_ago(120) == "2 分钟前"

    def test_hours_range(self):
        assert format_time_ago(3600) == "1 小时前"
        assert format_time_ago(7200) == "2 小时前"

    def test_days_range(self):
        # 恰好 24 小时
        assert format_time_ago(86400) == "1 天前"
        # 超过 24 小时按天取整
        assert format_time_ago(172800) == "2 天前"


class TestBucketAndFormatConsistency:
    """确保 bucket → 文案 与 format_time_ago 行为一致。"""

    def test_render_from_bucket_matches_format_time_ago(self):
        # 选择一组典型输入覆盖 seconds/minutes/hours/days 区间
        samples = [
            0,
            10,
            59,
            60,
            119,
            3600,
            7200,
            86400,
            172800,
        ]

        for value in samples:
            bucket = compute_time_ago_bucket(value)
            # 通过 bucket 渲染的文案
            from_bucket = render_time_ago_cn(bucket)
            # 通过专用 helper 渲染的文案
            from_helper = format_time_ago_from_bucket(bucket)
            # 通过秒数直接渲染的文案
            direct = format_time_ago(value)

            assert from_bucket == direct
            assert from_helper == direct


class TestFormatSecondsAgoCompat:
    """兼容包装：确保行为与 format_time_ago 一致。"""

    def test_delegates_to_format_time_ago(self):
        # 这里不严格断言文案细节，只要与 format_time_ago 相同即可
        for value in (0, 10, 59, 60, 3600, 86400, -5):
            assert format_seconds_ago(value) == format_time_ago(value)


class TestFormatIdleHealth:
    def test_basic_mapping(self) -> None:
        assert "健康" in format_idle_health(IdleHealth.HEALTHY)
        assert "空闲" in format_idle_health(IdleHealth.IDLE)
        assert "陈旧" in format_idle_health(IdleHealth.STALE)

    def test_unknown_and_future_values(self) -> None:
        assert format_idle_health(IdleHealth.UNKNOWN) == "未知"


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
    def test_none_returns_title_unchanged(self):
        assert append_duration_to_title("🔄 执行中", None) == "🔄 执行中"

    def test_zero_returns_title_unchanged(self):
        # 0.0 is falsy → title unchanged (acceptable: omit trivial durations)
        assert append_duration_to_title("🔄 执行中", 0.0) == "🔄 执行中"

    def test_positive_duration_appended(self):
        result = append_duration_to_title("🔄 执行中", 225)
        assert result == "🔄 执行中 · 3分45秒"

    def test_separator_is_middot(self):
        result = append_duration_to_title("title", 60)
        assert " · " in result

    def test_large_duration(self):
        result = append_duration_to_title("标题", 7261)
        assert result == "标题 · 2小时1分1秒"

    def test_small_duration(self):
        result = append_duration_to_title("标题", 3)
        assert result == "标题 · 3秒"


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

    def test_empty_name(self):
        tid = generate_task_id("")
        # Should produce _YYYYMMDD_HHMMSS_XXXX (name part is empty)
        assert tid.startswith("_") or len(tid.split("_")) >= 3

    def test_chinese_name_sanitized(self):
        tid = generate_task_id("我的项目")
        name_part = tid.split("_")[0]
        # Chinese chars are not alnum, replaced with _
        assert all(c.isalnum() or c == "_" for c in name_part)

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
