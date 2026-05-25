"""Tests for STATUS_BG_STYLE_MAP legal values and validate_background_style guard (AC-18)."""
from __future__ import annotations

import logging

from src.slock_engine.card_templates.common import (
    STATUS_BG_STYLE_MAP,
    build_status_badge,
    validate_background_style,
)
from src.slock_engine.models import AgentStatus

_VALID_BACKGROUND_STYLES = {"default", "grey", "card_primary"}


class TestStatusBgStyleMap:
    """Verify all values in STATUS_BG_STYLE_MAP are legal Feishu enum values."""

    def test_all_values_are_legal(self):
        """Every value in STATUS_BG_STYLE_MAP must be in the legal set."""
        for status, style in STATUS_BG_STYLE_MAP.items():
            assert style in _VALID_BACKGROUND_STYLES, (
                f"STATUS_BG_STYLE_MAP[{status}] = '{style}' is not a legal Feishu value. "
                f"Legal values: {_VALID_BACKGROUND_STYLES}"
            )

    def test_no_blue_yellow_purple(self):
        """Explicitly verify old illegal values are gone."""
        illegal = {"blue", "yellow", "purple"}
        for status, style in STATUS_BG_STYLE_MAP.items():
            assert style not in illegal, (
                f"STATUS_BG_STYLE_MAP[{status}] = '{style}' — "
                f"this was an illegal value that should have been replaced"
            )


class TestValidateBackgroundStyle:
    """Verify the guard function behavior."""

    def test_legal_values_pass_through(self):
        """Legal values are returned unchanged."""
        for val in _VALID_BACKGROUND_STYLES:
            assert validate_background_style(val) == val

    def test_illegal_value_falls_back_to_default(self):
        """Illegal values fall back to 'default'."""
        assert validate_background_style("blue") == "default"
        assert validate_background_style("yellow") == "default"
        assert validate_background_style("purple") == "default"
        assert validate_background_style("nonexistent") == "default"

    def test_illegal_value_logs_warning(self, caplog):
        """Illegal values trigger a warning log."""
        with caplog.at_level(logging.WARNING):
            validate_background_style("blue")
        assert "Invalid background_style 'blue'" in caplog.text

    def test_empty_string_falls_back(self):
        """Empty string is not a legal value."""
        assert validate_background_style("") == "default"


# ===========================================================================
# Task 17.3: Three-tier background style contrast
# ===========================================================================


class TestThreeTierBackgroundContrast:
    """Task 17.3: Verify three-tier visual contrast for key statuses.

    Tier 1 (default): IDLE - idle/completed states
    Tier 2 (grey): WAKING, THINKING, SENDING, MOVING, DISCUSSING - transition states
    Tier 3 (card_primary): RUNNING, CHECKING, PENDING_DISCUSSION - active/waiting states
    """

    def test_idle_uses_default(self):
        """IDLE status should use 'default' background (tier 1: idle state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.IDLE] == "default"

    def test_waking_uses_grey(self):
        """WAKING status should use 'grey' background (tier 2: transition state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.WAKING] == "grey"

    def test_thinking_uses_grey(self):
        """THINKING status should use 'grey' background (tier 2: transition state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.THINKING] == "grey"

    def test_running_uses_card_primary(self):
        """RUNNING status should use 'card_primary' background (tier 3: active state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.RUNNING] == "card_primary"

    def test_checking_uses_card_primary(self):
        """CHECKING status should use 'card_primary' background (tier 3: active state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.CHECKING] == "card_primary"

    def test_pending_discussion_uses_card_primary(self):
        """PENDING_DISCUSSION should use 'card_primary' (tier 3: waiting state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.PENDING_DISCUSSION] == "card_primary"

    def test_sending_uses_grey(self):
        """SENDING status should use 'grey' background (tier 2: transition state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.SENDING] == "grey"

    def test_moving_uses_grey(self):
        """MOVING status should use 'grey' background (tier 2: transition state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.MOVING] == "grey"

    def test_discussing_uses_grey(self):
        """DISCUSSING status should use 'grey' background (tier 2: transition state)."""
        assert STATUS_BG_STYLE_MAP[AgentStatus.DISCUSSING] == "grey"

    def test_three_tiers_distinct(self):
        """The three tiers should have distinct background styles."""
        tier1 = {STATUS_BG_STYLE_MAP[AgentStatus.IDLE]}
        tier2 = {
            STATUS_BG_STYLE_MAP[s] for s in [
                AgentStatus.WAKING, AgentStatus.THINKING,
                AgentStatus.SENDING, AgentStatus.MOVING, AgentStatus.DISCUSSING
            ]}
        tier3 = {
            STATUS_BG_STYLE_MAP[s] for s in [
                AgentStatus.RUNNING, AgentStatus.CHECKING,
                AgentStatus.PENDING_DISCUSSION
            ]}

        assert tier1 == {"default"}
        assert tier2 == {"grey"}
        assert tier3 == {"card_primary"}

    def test_build_status_badge_idle_has_default_background(self):
        """build_status_badge for IDLE should have 'default' background_style."""
        badge = build_status_badge(AgentStatus.IDLE)
        assert badge["background_style"] == "default"

    def test_build_status_badge_thinking_has_grey_background(self):
        """build_status_badge for THINKING should have 'grey' background_style."""
        badge = build_status_badge(AgentStatus.THINKING)
        assert badge["background_style"] == "grey"

    def test_build_status_badge_running_has_card_primary_background(self):
        """build_status_badge for RUNNING should have 'card_primary' background_style."""
        badge = build_status_badge(AgentStatus.RUNNING)
        assert badge["background_style"] == "card_primary"

    def test_build_status_badge_pending_discussion_has_card_primary(self):
        """build_status_badge for PENDING_DISCUSSION should have 'card_primary'."""
        badge = build_status_badge(AgentStatus.PENDING_DISCUSSION)
        assert badge["background_style"] == "card_primary"
