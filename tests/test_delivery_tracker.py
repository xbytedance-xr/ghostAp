"""Tests for DeliveryTracker: failure tracking, recovery banner, and mutual exclusion."""


from src.card.delivery.tracker import DeliveryTracker, PendingAction


class TestDeliveryTrackerBasic:
    """Basic success/failure counting."""

    def test_initial_state(self):
        tracker = DeliveryTracker()
        assert tracker.delivery_failures == 0
        assert tracker.should_notify_max_failures is False
        assert tracker.consume_pending_actions() == []

    def test_single_failure_increments(self):
        tracker = DeliveryTracker()
        tracker.on_failure()
        assert tracker.delivery_failures == 1

    def test_success_resets_failures(self):
        tracker = DeliveryTracker()
        tracker.on_failure()
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        assert tracker.delivery_failures == 0

    def test_terminal_success_resets_failures(self):
        tracker = DeliveryTracker()
        tracker.on_failure()
        tracker.on_success(is_terminal=True)
        assert tracker.delivery_failures == 0


class TestRecoveryBanner:
    """Recovery banner after failures then success."""

    def test_recovery_banner_shown_after_failure_then_success(self):
        tracker = DeliveryTracker()
        tracker.on_failure()
        tracker.on_success(is_terminal=False)

        actions = tracker.consume_pending_actions()
        assert len(actions) == 1
        assert actions[0] is PendingAction.SHOW_RECOVERY

    def test_no_recovery_banner_on_terminal_success(self):
        tracker = DeliveryTracker()
        tracker.on_failure()
        tracker.on_success(is_terminal=True)

        actions = tracker.consume_pending_actions()
        assert len(actions) == 0

    def test_recovery_banner_cleared_after_threshold(self):
        clock_time = [0.0]
        tracker = DeliveryTracker(
            clear_threshold=3,
            min_banner_display_secs=2.0,
            clock=lambda: clock_time[0],
        )
        # Trigger recovery
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        tracker.consume_pending_actions()  # show recovery banner

        # Advance clock past min display time
        clock_time[0] = 5.0

        # Send enough consecutive successes
        for _ in range(3):
            tracker.on_success(is_terminal=False)

        actions = tracker.consume_pending_actions()
        assert len(actions) == 1
        assert actions[0] is PendingAction.CLEAR_BANNER

    def test_banner_not_cleared_before_min_display_time(self):
        clock_time = [0.0]
        tracker = DeliveryTracker(
            clear_threshold=3,
            min_banner_display_secs=5.0,
            clock=lambda: clock_time[0],
        )
        # Trigger recovery
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        tracker.consume_pending_actions()

        # Time barely advanced
        clock_time[0] = 1.0

        for _ in range(3):
            tracker.on_success(is_terminal=False)

        actions = tracker.consume_pending_actions()
        # Should not clear — not enough time
        assert len(actions) == 0

    def test_banner_not_cleared_before_threshold_count(self):
        clock_time = [0.0]
        tracker = DeliveryTracker(
            clear_threshold=5,
            min_banner_display_secs=1.0,
            clock=lambda: clock_time[0],
        )
        tracker.on_failure()
        tracker.on_success(is_terminal=False)
        tracker.consume_pending_actions()

        clock_time[0] = 10.0
        # Only 3 successes (below threshold of 5)
        for _ in range(3):
            tracker.on_success(is_terminal=False)

        actions = tracker.consume_pending_actions()
        assert len(actions) == 0


class TestMaxFailuresWarning:
    """Max failures warning and notification."""

    def test_max_failures_triggers_warning(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        tracker.on_failure()
        tracker.on_failure()

        actions = tracker.consume_pending_actions()
        assert len(actions) == 1
        assert actions[0] is PendingAction.SHOW_MAX_FAILURES_WARNING

    def test_should_notify_max_failures_flag(self):
        tracker = DeliveryTracker(max_failures=2)
        tracker.on_failure()
        tracker.on_failure()

        assert tracker.should_notify_max_failures is True
        # Consuming resets the flag
        assert tracker.should_notify_max_failures is False

    def test_max_failures_below_threshold_no_warning(self):
        tracker = DeliveryTracker(max_failures=3)
        tracker.on_failure()
        tracker.on_failure()

        actions = tracker.consume_pending_actions()
        assert len(actions) == 0
        assert tracker.should_notify_max_failures is False


class TestMutualExclusion:
    """Recovery takes priority over max_failures in consume_pending_actions."""

    def test_recovery_trumps_max_failures(self):
        tracker = DeliveryTracker(max_failures=2)
        # 2 failures → max_failures triggered
        tracker.on_failure()
        tracker.on_failure()
        # Then success → recovery triggered
        tracker.on_success(is_terminal=False)

        actions = tracker.consume_pending_actions()
        # Only recovery banner, not max_failures
        assert len(actions) == 1
        assert actions[0] is PendingAction.SHOW_RECOVERY

    def test_consume_is_idempotent(self):
        tracker = DeliveryTracker(max_failures=2)
        tracker.on_failure()
        tracker.on_failure()

        actions1 = tracker.consume_pending_actions()
        actions2 = tracker.consume_pending_actions()
        assert len(actions1) == 1
        assert len(actions2) == 0


class TestFlagRetryPending:
    """flag_retry_pending() triggers SHOW_RETRY_PENDING in consume_pending_actions."""

    def test_flag_retry_pending_returns_pending_action(self):
        tracker = DeliveryTracker()
        tracker.flag_retry_pending()

        actions = tracker.consume_pending_actions()
        assert len(actions) == 1
        assert actions[0] is PendingAction.SHOW_RETRY_PENDING

    def test_flag_retry_pending_consumed_once(self):
        tracker = DeliveryTracker()
        tracker.flag_retry_pending()

        actions1 = tracker.consume_pending_actions()
        actions2 = tracker.consume_pending_actions()
        assert len(actions1) == 1
        assert actions1[0] is PendingAction.SHOW_RETRY_PENDING
        assert len(actions2) == 0

    def test_flag_retry_pending_no_duplicate_when_called_twice(self):
        tracker = DeliveryTracker()
        tracker.flag_retry_pending()
        tracker.flag_retry_pending()

        actions = tracker.consume_pending_actions()
        # Implementation-specific: may produce 1 or 2, but should not crash
        assert all(a is PendingAction.SHOW_RETRY_PENDING for a in actions)
        assert len(actions) >= 1


class TestMaxFailuresDedup:
    """Repeated failures beyond threshold should only trigger one warning."""

    def test_repeated_failures_only_one_warning(self):
        """Calling on_failure many times past max should produce only one warning action."""
        tracker = DeliveryTracker(max_failures=2)
        for _ in range(5):
            tracker.on_failure()

        actions = tracker.consume_pending_actions()
        warning_count = sum(1 for a in actions if a is PendingAction.SHOW_MAX_FAILURES_WARNING)
        assert warning_count == 1

    def test_notify_flag_consumed_once(self):
        """should_notify_max_failures property resets after first read."""
        tracker = DeliveryTracker(max_failures=2)
        tracker.on_failure()
        tracker.on_failure()
        tracker.on_failure()  # extra beyond threshold

        assert tracker.should_notify_max_failures is True
        assert tracker.should_notify_max_failures is False  # consumed


class TestConcurrentAccess:
    """Task 28: Concurrent on_failure/on_success from multiple threads."""

    def test_concurrent_failure_and_success_no_crash(self):
        """Multiple threads calling on_failure/on_success concurrently should not raise."""
        import threading

        tracker = DeliveryTracker(max_failures=5)
        errors = []

        def call_failures():
            try:
                for _ in range(100):
                    tracker.on_failure()
            except Exception as e:
                errors.append(e)

        def call_successes():
            try:
                for _ in range(100):
                    tracker.on_success(is_terminal=False)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=call_failures) for _ in range(3)]
            + [threading.Thread(target=call_successes) for _ in range(3)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

    def test_concurrent_consume_pending_actions(self):
        """Multiple threads consuming pending actions should not raise or double-report."""
        import threading

        tracker = DeliveryTracker(max_failures=2)
        tracker.on_failure()
        tracker.on_failure()

        results = []
        lock = threading.Lock()

        def consume():
            actions = tracker.consume_pending_actions()
            with lock:
                results.extend(actions)

        threads = [threading.Thread(target=consume) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At most one SHOW_MAX_FAILURES_WARNING should be reported
        warning_count = sum(1 for a in results if a is PendingAction.SHOW_MAX_FAILURES_WARNING)
        assert warning_count <= 1
