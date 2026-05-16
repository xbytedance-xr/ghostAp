"""Tests for DeliveryTracker failure tracking and recovery banner logic."""


import pytest

from src.card.delivery.tracker import DeliveryTracker, PendingAction


class TestOnFailure:
    """on_failure accumulates failures and triggers max_failures warning."""

    def test_single_failure_increments_counter(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        assert tracker.delivery_failures == 1

    def test_multiple_failures_accumulate(self):
        tracker = DeliveryTracker(max_failures=5)
        for _ in range(4):
            tracker.on_failure()
        assert tracker.delivery_failures == 4

    def test_max_failures_triggers_warning(self):
        tracker = DeliveryTracker(max_failures=3)
        for _ in range(3):
            tracker.on_failure()
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_MAX_FAILURES_WARNING in actions

    def test_max_failures_sets_notify_flag(self):
        tracker = DeliveryTracker(max_failures=2)
        tracker.on_failure()
        tracker.on_failure()
        assert tracker.should_notify_max_failures is True
        # Consuming once clears the flag
        assert tracker.should_notify_max_failures is False

    def test_failure_records_timestamp(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        assert tracker.last_failure_timestamp is not None
        # Format should be HH:MM
        parts = tracker.last_failure_timestamp.split(":")
        assert len(parts) == 2
        assert all(p.isdigit() for p in parts)

    def test_below_max_failures_no_warning(self):
        tracker = DeliveryTracker(max_failures=5)
        tracker.on_failure()
        tracker.on_failure()
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_MAX_FAILURES_WARNING not in actions


class TestOnSuccess:
    """on_success resets failures and triggers recovery banner."""

    def test_success_after_failures_triggers_recovery(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RECOVERY in actions

    def test_success_resets_failure_counter(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        assert tracker.delivery_failures == 0

    def test_terminal_success_after_failures_no_recovery_banner(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        tracker.on_failure()
        tracker.on_success(is_terminal=True)
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RECOVERY not in actions

    def test_success_without_prior_failures_no_recovery(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RECOVERY not in actions


class TestConsecutiveSuccessesClearBanner:
    """Consecutive successes after recovery clear the banner."""

    def test_clear_banner_after_threshold(self):
        clock_time = [0.0]
        tracker = DeliveryTracker(
            max_failures=3,
            clear_threshold=2,
            min_banner_display_secs=1.0,
            clock=lambda: clock_time[0],
        )
        # Trigger recovery
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RECOVERY in actions

        # Advance clock past min_banner_display_secs
        clock_time[0] = 2.0

        # Send clear_threshold consecutive successes
        tracker.on_success(is_terminal=False)
        tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.CLEAR_BANNER in actions

    def test_clear_banner_respects_min_display_time(self):
        clock_time = [0.0]
        tracker = DeliveryTracker(
            max_failures=3,
            clear_threshold=2,
            min_banner_display_secs=5.0,
            clock=lambda: clock_time[0],
        )
        # Trigger recovery
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        tracker.consume_pending_actions()

        # Clock NOT advanced enough
        clock_time[0] = 1.0
        tracker.on_success(is_terminal=False)
        tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.CLEAR_BANNER not in actions

    def test_clear_banner_requires_enough_successes(self):
        clock_time = [0.0]
        tracker = DeliveryTracker(
            max_failures=3,
            clear_threshold=5,
            min_banner_display_secs=0.0,
            clock=lambda: clock_time[0],
        )
        # Trigger recovery
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        tracker.consume_pending_actions()

        # Only 3 successes, need 5
        for _ in range(3):
            tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.CLEAR_BANNER not in actions


class TestFlagRetryPending:
    """flag_retry_pending sets SHOW_RETRY_PENDING action."""

    def test_flag_retry_pending(self):
        tracker = DeliveryTracker()
        tracker.flag_retry_pending()
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RETRY_PENDING in actions

    def test_flag_retry_consumed_only_once(self):
        tracker = DeliveryTracker()
        tracker.flag_retry_pending()
        tracker.consume_pending_actions()
        # Second consume should be empty
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RETRY_PENDING not in actions


class TestMutualExclusion:
    """Recovery takes priority over max_failures warning."""

    def test_recovery_trumps_max_failures(self):
        tracker = DeliveryTracker(max_failures=2)
        # Hit max failures
        tracker.on_failure()
        tracker.on_failure()
        # Then succeed (triggers recovery)
        tracker.on_success(is_terminal=False)
        actions = tracker.consume_pending_actions()
        assert PendingAction.SHOW_RECOVERY in actions
        assert PendingAction.SHOW_MAX_FAILURES_WARNING not in actions


class TestValidation:
    """Constructor validation."""

    def test_max_failures_must_be_positive(self):
        with pytest.raises(ValueError, match="max_failures must be >= 1"):
            DeliveryTracker(max_failures=0)

    def test_clear_threshold_must_be_positive(self):
        with pytest.raises(ValueError, match="clear_threshold must be >= 1"):
            DeliveryTracker(clear_threshold=0)
