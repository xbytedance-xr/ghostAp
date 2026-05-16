"""Tests for config.py validators: boundary values and Literal type constraints."""

import pytest
from pydantic import ValidationError

from src.config import Settings


def _build_settings(**overrides) -> Settings:
    """Create a Settings instance with only the specified overrides.

    Uses model_validate to bypass env file reading.
    """
    # Provide minimal required defaults to avoid env-file side effects
    defaults = {
        "app_id": "test",
        "app_secret": "test",
    }
    defaults.update(overrides)
    return Settings.model_validate(defaults)


class TestCardMaxCharsValidator:
    """Boundary tests for card_max_chars [1000, 50000]."""

    def test_at_lower_bound(self):
        s = _build_settings(card_max_chars=1000)
        assert s.card.max_chars == 1000

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError, match="card_max_chars"):
            _build_settings(card_max_chars=999)

    def test_at_upper_bound(self):
        s = _build_settings(card_max_chars=50000)
        assert s.card.max_chars == 50000

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError, match="card_max_chars"):
            _build_settings(card_max_chars=50001)


class TestCardSessionLockMaxValidator:
    """Boundary tests for card_session_lock_max [1000, 100000]."""

    def test_at_lower_bound(self):
        s = _build_settings(card_session_lock_max=1000)
        assert s.card.session_lock_max == 1000

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError, match="card_session_lock_max"):
            _build_settings(card_session_lock_max=999)

    def test_at_upper_bound(self):
        s = _build_settings(card_session_lock_max=100000)
        assert s.card.session_lock_max == 100000

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError, match="card_session_lock_max"):
            _build_settings(card_session_lock_max=100001)


class TestCardSessionLockTtlValidator:
    """Boundary tests for card_session_lock_ttl [60, 3600]."""

    def test_at_lower_bound(self):
        s = _build_settings(card_session_lock_ttl=60)
        assert s.card.session_lock_ttl == 60.0

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError, match="card_session_lock_ttl"):
            _build_settings(card_session_lock_ttl=59)

    def test_at_upper_bound(self):
        s = _build_settings(card_session_lock_ttl=3600, card_session_idle_timeout=7200)
        assert s.card.session_lock_ttl == 3600.0

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError, match="card_session_lock_ttl"):
            _build_settings(card_session_lock_ttl=3601)


class TestCardSessionIdleTimeoutValidator:
    """Boundary tests for card_session_idle_timeout [300, 7200]."""

    def test_at_lower_bound(self):
        s = _build_settings(card_session_idle_timeout=300, card_session_lock_ttl=60, card_session_idle_warn_at_remaining=60)
        assert s.card.session_idle_timeout == 300

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError, match="card_session_idle_timeout"):
            _build_settings(card_session_idle_timeout=299)

    def test_at_upper_bound(self):
        s = _build_settings(card_session_idle_timeout=7200)
        assert s.card.session_idle_timeout == 7200

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError, match="card_session_idle_timeout"):
            _build_settings(card_session_idle_timeout=7201)


class TestCardButtonLayoutLiteral:
    """Tests for card_button_layout Literal['desktop','mobile','responsive']."""

    def test_valid_values(self):
        for val in ("desktop", "mobile", "responsive"):
            s = _build_settings(card_button_layout=val)
            assert s.card.button_layout == val

    def test_invalid_value(self):
        with pytest.raises(ValidationError):
            _build_settings(card_button_layout="foobar")


class TestCardButtonSizeLiteral:
    """Tests for card_button_size Literal['small','medium','large']."""

    def test_valid_values(self):
        for val in ("small", "medium", "large"):
            s = _build_settings(card_button_size=val)
            assert s.card.button_size == val

    def test_invalid_value(self):
        with pytest.raises(ValidationError):
            _build_settings(card_button_size="big")


class TestCardActionDedupTtlValidator:
    """Boundary tests for card_action_dedup_ttl [0, 10]."""

    def test_at_lower_bound(self):
        s = _build_settings(card_action_dedup_ttl=0)
        assert s.card.action_dedup_ttl == 0

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError):
            _build_settings(card_action_dedup_ttl=-1)

    def test_mid_value(self):
        s = _build_settings(card_action_dedup_ttl=5)
        assert s.card.action_dedup_ttl == 5

    def test_at_upper_bound(self):
        s = _build_settings(card_action_dedup_ttl=10)
        assert s.card.action_dedup_ttl == 10

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError):
            _build_settings(card_action_dedup_ttl=11)


class TestSessionConfigBudgetClamp:
    """Verify SessionConfig clamps budget.visible_chars when > card_max_chars."""

    def test_budget_exceeds_card_max_chars_is_clamped(self, caplog):
        """When visible_chars > card_max_chars, factory clamps it during session creation."""
        from unittest.mock import MagicMock, patch

        from src.card.render.budget import RenderBudget
        from src.card.session.config import SessionCallbacks
        from src.card.session.factory import CardSessionFactory
        from src.card.state.models import CardMetadata

        mock_settings = MagicMock()
        mock_settings.card.max_chars = 20000
        mock_settings.card.button_size = "medium"
        mock_settings.card.session_idle_timeout = 1800
        mock_settings.card.session_idle_warn_at_remaining = 300

        oversized_budget = RenderBudget(visible_chars=25000)
        metadata = CardMetadata(engine_type="deep", mode_name="Test", mode_emoji="🔵")

        delivery = MagicMock()
        factory = CardSessionFactory(delivery=delivery)
        cbs = SessionCallbacks(notify_callback=lambda _c, _t: None)

        with patch("src.config.get_settings", return_value=mock_settings):
            session = factory.create(chat_id="c1", metadata=metadata, budget=oversized_budget, callbacks=cbs)

        assert session._budget.visible_chars == 20000
        assert "clamping" in caplog.text.lower() or "exceeds" in caplog.text.lower()

    def test_budget_within_limit_unchanged(self):
        """When visible_chars <= card_max_chars, budget remains unchanged."""
        from unittest.mock import MagicMock, patch

        from src.card.render.budget import RenderBudget
        from src.card.session.config import SessionCallbacks
        from src.card.session.factory import CardSessionFactory
        from src.card.state.models import CardMetadata

        mock_settings = MagicMock()
        mock_settings.card.max_chars = 28000
        mock_settings.card.button_size = "medium"
        mock_settings.card.session_idle_timeout = 1800
        mock_settings.card.session_idle_warn_at_remaining = 300

        budget = RenderBudget(visible_chars=25000)
        metadata = CardMetadata(engine_type="deep", mode_name="Test", mode_emoji="🔵")

        delivery = MagicMock()
        factory = CardSessionFactory(delivery=delivery)
        cbs = SessionCallbacks(notify_callback=lambda _c, _t: None)

        with patch("src.config.get_settings", return_value=mock_settings):
            session = factory.create(chat_id="c1", metadata=metadata, budget=budget, callbacks=cbs)

        assert session._budget.visible_chars == 25000


class TestCardActionDedupMaxSizeValidator:
    """Boundary tests for card_action_dedup_max_size [100, 50000]."""

    def test_at_lower_bound(self):
        s = _build_settings(card_action_dedup_max_size=100)
        assert s.card.action_dedup_max_size == 100

    def test_at_upper_bound(self):
        s = _build_settings(card_action_dedup_max_size=50000)
        assert s.card.action_dedup_max_size == 50000

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError, match="card_action_dedup_max_size"):
            _build_settings(card_action_dedup_max_size=99)

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError, match="card_action_dedup_max_size"):
            _build_settings(card_action_dedup_max_size=50001)

    def test_default_value_in_range(self):
        s = _build_settings()
        assert 100 <= s.card.action_dedup_max_size <= 50000


class TestLockUndoWindowSecondsValidator:
    """Boundary tests for lock_undo_window_seconds [60, 600]."""

    def test_at_lower_bound(self):
        s = _build_settings(lock_undo_window_seconds=60)
        assert s.lock_undo_window_seconds == 60

    def test_below_lower_bound(self):
        with pytest.raises(ValidationError, match="lock_undo_window_seconds"):
            _build_settings(lock_undo_window_seconds=59)

    def test_at_upper_bound(self):
        s = _build_settings(lock_undo_window_seconds=600)
        assert s.lock_undo_window_seconds == 600

    def test_above_upper_bound(self):
        with pytest.raises(ValidationError, match="lock_undo_window_seconds"):
            _build_settings(lock_undo_window_seconds=601)

    def test_default_value(self):
        s = _build_settings()
        assert s.lock_undo_window_seconds == 300

    def test_not_multiple_of_60_rejected(self):
        """lock_undo_window_seconds must be a multiple of 60."""
        with pytest.raises(ValidationError, match="必须为 60 的整数倍"):
            _build_settings(lock_undo_window_seconds=90)

    def test_multiple_of_60_accepted(self):
        s = _build_settings(lock_undo_window_seconds=180)
        assert s.lock_undo_window_seconds == 180


class TestCardSessionCrossFieldValidator:
    """Cross-field: card_session_lock_ttl must be <= card_session_idle_timeout."""

    def test_lock_ttl_greater_than_idle_timeout_raises(self):
        with pytest.raises(ValidationError, match="card_session_lock_ttl"):
            _build_settings(card_session_lock_ttl=3600, card_session_idle_timeout=300)

    def test_lock_ttl_equal_to_idle_timeout_accepted(self):
        s = _build_settings(card_session_lock_ttl=600, card_session_idle_timeout=600)
        assert s.card.session_lock_ttl == 600.0

    def test_default_values_pass(self):
        """Default 600 <= 1800 should pass."""
        s = _build_settings()
        assert s.card.session_lock_ttl <= s.card.session_idle_timeout


class TestValidatorMessagesContainUnit:
    """All card_session_* and lock_undo_window_seconds validators include '（秒）' in error."""

    def test_lock_ttl_message_has_unit(self):
        with pytest.raises(ValidationError, match=r"（秒）"):
            _build_settings(card_session_lock_ttl=10)

    def test_idle_timeout_message_has_unit(self):
        with pytest.raises(ValidationError, match=r"（秒）"):
            _build_settings(card_session_idle_timeout=100)

    def test_lock_undo_window_message_has_unit(self):
        with pytest.raises(ValidationError, match=r"（秒）"):
            _build_settings(lock_undo_window_seconds=30)


# --- Task 22: Cross-field validation for session_idle_warn_at_remaining ---


class TestSessionIdleWarnCrossField:
    """session_idle_warn_at_remaining must be < session_idle_timeout."""

    def test_warn_equal_to_timeout_rejected(self):
        with pytest.raises(ValidationError, match="card_session_idle_warn_at_remaining"):
            _build_settings(
                card_session_idle_warn_at_remaining=1800,
                card_session_idle_timeout=1800,
                card_session_lock_ttl=60,
            )

    def test_warn_greater_than_timeout_rejected(self):
        with pytest.raises(ValidationError, match="card_session_idle_warn_at_remaining"):
            _build_settings(
                card_session_idle_warn_at_remaining=2000,
                card_session_idle_timeout=1800,
                card_session_lock_ttl=60,
            )

    def test_warn_less_than_timeout_accepted(self):
        s = _build_settings(
            card_session_idle_warn_at_remaining=1799,
            card_session_idle_timeout=1800,
            card_session_lock_ttl=60,
        )
        assert s.card.session_idle_warn_at_remaining == 1799


# --- Task 23: Env var alias resolution ---


class TestEnvVarAliasResolution:
    """Both CARD_SESSION_IDLE_WARN_AT_REMAINING and CARD_SESSION_IDLE_WARN_BEFORE resolve to the same field."""

    def test_new_name_resolves(self):
        s = _build_settings(card_session_idle_warn_at_remaining=300)
        assert s.card.session_idle_warn_at_remaining == 300

    def test_old_name_resolves(self):
        s = _build_settings(card_session_idle_warn_before=300)
        assert s.card.session_idle_warn_at_remaining == 300

    def test_both_names_yield_same_value(self):
        s1 = _build_settings(card_session_idle_warn_at_remaining=250)
        s2 = _build_settings(card_session_idle_warn_before=250)
        assert s1.card.session_idle_warn_at_remaining == s2.card.session_idle_warn_at_remaining


# --- Task 24: action_dedup_cleanup_interval range validation ---


class TestActionDedupCleanupIntervalValidator:
    """Boundary tests for action_dedup_cleanup_interval [1, 3600]."""

    def test_below_lower_bound_rejected(self):
        with pytest.raises(ValidationError, match="action_dedup_cleanup_interval"):
            _build_settings(card_action_dedup_cleanup_interval=0)

    def test_above_upper_bound_rejected(self):
        with pytest.raises(ValidationError, match="action_dedup_cleanup_interval"):
            _build_settings(card_action_dedup_cleanup_interval=3601)

    def test_at_lower_bound_accepted(self):
        s = _build_settings(card_action_dedup_cleanup_interval=1)
        assert s.card.action_dedup_cleanup_interval == 1

    def test_at_upper_bound_accepted(self):
        s = _build_settings(card_action_dedup_cleanup_interval=3600)
        assert s.card.action_dedup_cleanup_interval == 3600


class TestSessionMaxRotationsValidator:
    """Boundary tests for session_max_rotations [1, 100]."""

    def test_zero_raises(self):
        with pytest.raises(ValidationError, match="card_session_max_rotations"):
            _build_settings(card_session_max_rotations=0)

    def test_one_ok(self):
        s = _build_settings(card_session_max_rotations=1)
        assert s.card.session_max_rotations == 1

    def test_hundred_ok(self):
        s = _build_settings(card_session_max_rotations=100)
        assert s.card.session_max_rotations == 100

    def test_101_raises(self):
        with pytest.raises(ValidationError, match="card_session_max_rotations"):
            _build_settings(card_session_max_rotations=101)

    def test_negative_raises(self):
        with pytest.raises(ValidationError, match="card_session_max_rotations"):
            _build_settings(card_session_max_rotations=-1)


class TestDeliveryPoolMaxWorkersValidator:
    """Boundary tests for delivery_pool_max_workers [1, 32]."""

    def test_zero_raises(self):
        with pytest.raises(ValidationError, match="card_delivery_pool_max_workers"):
            _build_settings(card_delivery_pool_max_workers=0)

    def test_one_ok(self):
        s = _build_settings(card_delivery_pool_max_workers=1)
        assert s.card.delivery_pool_max_workers == 1

    def test_32_ok(self):
        s = _build_settings(card_delivery_pool_max_workers=32)
        assert s.card.delivery_pool_max_workers == 32

    def test_33_raises(self):
        with pytest.raises(ValidationError, match="card_delivery_pool_max_workers"):
            _build_settings(card_delivery_pool_max_workers=33)

    def test_negative_raises(self):
        with pytest.raises(ValidationError, match="card_delivery_pool_max_workers"):
            _build_settings(card_delivery_pool_max_workers=-1)
