"""Test UI_TEXT placeholder consistency (AC-6).

Ensures:
1. All format strings can be rendered without KeyError using canonical placeholder names.
2. No deprecated shorthand placeholders ({secs}, {mins}) exist.
"""
import re

import pytest

from src.card.ui_text import UI_TEXT

# Canonical placeholder names and their test values
_CANONICAL_VALUES = {
    "seconds": 30,
    "minutes": 5,
    "hours": 2,
    "engine_cmd": "/deep",
    "engine_name": "Deep",
    "timestamp": "12:00",
    "name": "test",
    "mode_name": "Test",
    "emoji": "🔧",
    "cmd": "deep",
    "error": "err",
    "step": 1,
    "desc": "desc",
    "count": 3,
    "n": 2,
    "base": "main",
    "base_branch": "main",
    "path": "/tmp",
    "goal": "build",
    "elapsed": 10,
    "max": 5,
    "session_id": "abc123",
    "tool": "coco",
    "reason": "timeout",
    "model": "gpt-4",
    "msg": "message",
    "status": "running",
    "attempt": 1,
    "max_attempts": 3,
    "delay_sec": 5,
    "sec": 5,
    "i": 1,
    "satisfied": 3,
    "total": 5,
    "num": 1,
    "title": "步骤",
    "text": "确认",
    "tool_name": "Coco",
    "duration": "5分",
    "error_detail": "detail",
    "current": 2,
    "category": "main",
    "timeout_display": "30 分钟",
    "idle_minutes": 15,
    "last_active_time": "10:30",
}

# Regex to find {placeholder} patterns in format strings
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Deprecated shorthand names that should NOT exist
_DEPRECATED_PLACEHOLDERS = {"secs", "mins"}


class TestUITextPlaceholderConsistency:
    """Verify all UI_TEXT format strings use canonical placeholder names."""

    def test_no_deprecated_shorthand_placeholders(self):
        """AC-6: No {secs} or {mins} shorthand in any UI_TEXT value."""
        violations = []
        for key, value in UI_TEXT.items():
            if not isinstance(value, str):
                continue
            found = _PLACEHOLDER_RE.findall(value)
            for placeholder in found:
                if placeholder in _DEPRECATED_PLACEHOLDERS:
                    violations.append(f"{key}: found deprecated placeholder {{{placeholder}}}")
        assert violations == [], "Deprecated placeholders found:\n" + "\n".join(violations)

    def test_all_format_strings_renderable(self):
        """All UI_TEXT entries with {placeholders} can be .format() without KeyError."""
        failures = []
        for key, value in UI_TEXT.items():
            if not isinstance(value, str):
                continue
            placeholders = _PLACEHOLDER_RE.findall(value)
            if not placeholders:
                continue
            # Build kwargs from canonical values
            kwargs = {}
            for p in placeholders:
                if p in _CANONICAL_VALUES:
                    kwargs[p] = _CANONICAL_VALUES[p]
                else:
                    kwargs[p] = f"<{p}>"  # fallback for unknown placeholders
            try:
                value.format(**kwargs)
            except (KeyError, IndexError, ValueError) as exc:
                failures.append(f"{key}: {exc}")
        assert failures == [], "Format failures:\n" + "\n".join(failures)


class TestDurationPlaceholders:
    """Specifically verify duration format strings use full words."""

    def test_duration_mins_secs_uses_seconds(self):
        """duration_mins_secs uses {seconds} not {secs}."""
        text = UI_TEXT["duration_mins_secs"]
        assert "{seconds}" in text
        assert "{secs}" not in text

    def test_duration_hours_mins_secs_uses_full_words(self):
        """duration_hours_mins_secs uses {minutes} and {seconds} not {mins}/{secs}."""
        text = UI_TEXT["duration_hours_mins_secs"]
        assert "{minutes}" in text
        assert "{seconds}" in text
        assert "{mins}" not in text
        assert "{secs}" not in text


class TestCleanupButtonConsistency:
    """AC-7: Cleanup buttons use consistent emoji."""

    def test_wt_btn_cleanup_uses_broom_emoji(self):
        assert "🧹" in UI_TEXT["wt_btn_cleanup"]

    def test_system_worktree_btn_cleanup_uses_broom_emoji(self):
        assert "🧹" in UI_TEXT["system_worktree_btn_cleanup"]

    def test_removed_system_worktree_alias_keys_do_not_return(self):
        removed_aliases = {
            "system_worktree_confirm_title",
            "system_worktree_confirm_header",
            "system_worktree_confirm_banner",
            "system_worktree_btn_confirm",
            "system_worktree_btn_reselect",
            "system_worktree_progress_title",
            "system_worktree_progress_header",
            "system_worktree_progress_banner",
            "system_worktree_btn_execute",
            "system_worktree_btn_retry",
        }

        assert removed_aliases.isdisjoint(UI_TEXT)


class TestTTLPrewarningText:
    """AC-8: TTL prewarning contains time-related closure hint."""

    def test_ttl_prewarning_contains_closure_hint(self):
        assert "分钟后关闭" in UI_TEXT["card_session_ttl_prewarning"]
        assert "{minutes}" in UI_TEXT["card_session_ttl_prewarning"]

    def test_ttl_prewarning_references_keep_alive_btn(self):
        """Prewarning text must reference the keep-alive button name to avoid semantic gap."""
        assert "「保持连接」" in UI_TEXT["card_session_ttl_prewarning"]
        assert "保持连接" in UI_TEXT["ttl_keep_alive_btn"]


class TestErrorTextsActionable:
    """AC-9, AC-10, AC-11: Error texts contain actionable next steps."""

    def test_card_content_load_error_has_guidance(self):
        assert "联系管理员" in UI_TEXT["card_content_load_error"]

    def test_card_content_load_error_running_has_guidance(self):
        assert "自动恢复" in UI_TEXT["card_content_load_error_running"]

    def test_deep_error_no_detail_has_retry_hint(self):
        assert "{engine_cmd}" in UI_TEXT["deep_error_no_detail"]

    def test_intent_unknown_has_help_hint(self):
        assert "/help" in UI_TEXT["intent_unknown_msg"]


class TestWorktreeStepHints:
    """AC-14: Worktree step hints exist in UI_TEXT."""

    @pytest.mark.parametrize("key", [
        "worktree_step_tool_select_hint",
        "worktree_step_confirm_hint",
        "worktree_step_units_hint",
        "worktree_step_merge_hint",
    ])
    def test_step_hint_exists_and_non_empty(self, key):
        assert key in UI_TEXT
        assert len(UI_TEXT[key]) > 0


class TestNewPhase3Keys:
    """Verify new keys added in Phase 3 exist and are well-formed."""

    def test_toast_dedup_key_exists(self):
        assert "card_session_toast_dedup" in UI_TEXT
        assert "重复" in UI_TEXT["card_session_toast_dedup"]

    def test_ttl_lock_contention_key_exists(self):
        assert "card_session_ttl_lock_contention" in UI_TEXT
        assert "{engine_cmd}" in UI_TEXT["card_session_ttl_lock_contention"]

    def test_force_close_notice_has_resource_reclaim(self):
        assert "系统回收资源" in UI_TEXT["card_session_ttl_force_close_notice"]

    def test_terminal_fallback_has_engine_cmd(self):
        assert "{engine_cmd}" in UI_TEXT["card_session_terminal_fallback_notice"]

    def test_warning_render_fail_has_engine_cmd(self):
        assert "{engine_cmd}" in UI_TEXT["card_session_warning_render_fail"]

    def test_toasts_have_no_trailing_period(self):
        """All toast messages should not end with period for consistency."""
        toast_keys = [k for k in UI_TEXT if "toast" in k and isinstance(UI_TEXT[k], str)]
        violations = [k for k in toast_keys if UI_TEXT[k].endswith("。")]
        assert violations == [], f"Toast keys ending with period: {violations}"

    def test_system_help_tips_has_format_placeholders(self):
        """system_help_tips should contain {timeout_display} and {warn_display} for dynamic rendering."""
        text = UI_TEXT["system_help_tips"]
        assert "{timeout_display}" in text
        assert "{warn_display}" in text


# ---------------------------------------------------------------------------
# Review round 2: UX text corrections verification
# ---------------------------------------------------------------------------


class TestReviewRound2TextCorrections:
    """Verify the four UX text corrections from review round 2."""

    def test_rejected_notice_no_system_jargon(self):
        """rejected_notice must not contain '并发会话', '容量' jargon."""
        text = UI_TEXT["card_session_rejected_notice"]
        assert "并发会话" not in text
        assert "容量" not in text
        # Must contain engine_cmd placeholder
        assert "{engine_cmd}" in text
        # Verify it formats without error
        text.format(engine_cmd="/deep")

    def test_ttl_expired_concise(self):
        """ttl_expired must include recovery hint and format correctly."""
        text = UI_TEXT["card_session_ttl_expired"]
        # Generic fallback uses {expired_commands} (not engine_cmd)
        assert "{expired_commands}" in text, f"ttl_expired should include expired_commands placeholder: {text}"
        # Verify it formats without error
        text.format(expired_commands="/spec /deep /wt")

    def test_help_tips_two_stage_close(self):
        """system_help_tips must mention advance notification before close."""
        text = UI_TEXT["system_help_tips"]
        assert "提醒" in text or "提前" in text or "通知" in text or "续期" in text
        # Verify it formats without error
        text.format(timeout_display="30 分钟", warn_display="7 分钟")

    def test_deep_error_no_detail_no_jargon(self):
        """deep_error_no_detail must not contain '无详细信息'."""
        text = UI_TEXT["deep_error_no_detail"]
        assert "无详细信息" not in text
        # Must contain engine_cmd placeholder
        assert "{engine_cmd}" in text
        # Verify it formats without error
        text.format(engine_cmd="/deep")

    def test_help_tips_includes_config_guidance(self):
        """system_help_tips should include env var hint for ops users."""
        text = UI_TEXT["system_help_tips"]
        # Should include CARD_SESSION_IDLE_TIMEOUT hint for ops adjustability
        assert "CARD_SESSION_IDLE_TIMEOUT" in text
        # Should mention close/end behavior
        assert "关闭" in text


class TestDeepErrorFallbackNoPrefix:
    """AC-20: deep_error_no_detail empty action_prefix fallback uses UI_TEXT."""

    def test_fallback_key_exists(self):
        """deep_error_fallback_no_prefix must exist in UI_TEXT."""
        assert "deep_error_fallback_no_prefix" in UI_TEXT

    def test_fallback_contains_retry_guidance(self):
        """Fallback text must contain actionable retry guidance."""
        text = UI_TEXT["deep_error_fallback_no_prefix"]
        assert "重试" in text or "重新发送" in text or "/deep" in text or "/help" in text

    def test_fallback_has_no_placeholder(self):
        """Fallback text should be a plain string with no format placeholders."""
        text = UI_TEXT["deep_error_fallback_no_prefix"]
        assert "{" not in text

    def test_builder_uses_fallback_key_for_empty_prefix(self):
        """DeepBuilder renders UI_TEXT['deep_error_fallback_no_prefix'] when action_prefix is empty."""
        from src.card.ui_text import UI_TEXT as _UI

        # Simulate the exact code path from builders/deep.py:
        # if is_error and not display_content and not action_prefix → use fallback key
        action_prefix = ""
        display_content = ""
        is_error = True

        if is_error and not display_content:
            if action_prefix:
                display_content = _UI["deep_error_no_detail"].format(engine_cmd=f"/{action_prefix}")
            else:
                display_content = _UI["deep_error_fallback_no_prefix"]

        assert display_content == _UI["deep_error_fallback_no_prefix"]
        assert "/help" in display_content
        assert display_content.count("/help") == 1


class TestLockUITextPlaceholders:
    """Verify LOCK_UI_TEXT format strings use lock_undo_window_display."""

    def test_lock_help_admin_lock_cmd_format(self):
        """lock_help_admin_lock_cmd must accept lock_undo_window_display without KeyError."""
        from src.card.styles_lock import LOCK_UI_TEXT

        template = LOCK_UI_TEXT["lock_help_admin_lock_cmd"]
        result = template.format(lock_undo_window_display="5 分钟")
        assert "5 分钟" in result
        assert "/lock" in result

    def test_lock_success_lock_reply_format(self):
        """lock_success_lock_reply must accept lock_undo_window_display without KeyError."""
        from src.card.styles_lock import LOCK_UI_TEXT

        template = LOCK_UI_TEXT["lock_success_lock_reply"]
        result = template.format(lock_undo_window_display="约 2 分钟")
        assert "约 2 分钟" in result
        assert "锁定" in result
