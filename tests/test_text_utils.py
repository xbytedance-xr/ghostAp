"""Tests for src/utils/text.py — format_duration, append_duration_to_title, generate_task_id."""

import re
import time

from src.utils.text import append_duration_to_title, format_duration, generate_task_id

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
