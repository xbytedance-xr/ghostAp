"""Tests for CardSessionFactory: creation, defaults, and snapshot mode."""

import logging
from unittest.mock import MagicMock

from src.card.render.budget import RenderBudget
from src.card.session.config import SessionCallbacks
from src.card.session.factory import CardSessionFactory
from src.card.state.models import CardMetadata

# Default callbacks for tests (satisfies the notify_callback requirement)
_TEST_CALLBACKS = SessionCallbacks(notify_callback=lambda _cid, _txt: None)


def _make_factory(**kwargs):
    delivery = MagicMock()
    return CardSessionFactory(delivery=delivery, **kwargs)


class TestCardSessionFactoryCreate:
    """Tests for CardSessionFactory.create()."""

    def test_create_returns_card_session(self):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="chat_1", metadata=metadata, callbacks=_TEST_CALLBACKS)
        assert session._chat_id == "chat_1"
        assert not session.closed

    def test_create_passes_hooks(self):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="loop", mode_name="Loop", mode_emoji="🔄")
        hook = MagicMock()
        cbs = SessionCallbacks(notify_callback=lambda _c, _t: None, hooks=(hook,))
        session = factory.create(chat_id="chat_2", metadata=metadata, callbacks=cbs)
        assert hook in session._hooks

    def test_create_uses_factory_budget_as_default(self):
        default_budget = RenderBudget(engine_cmd="/deep")
        factory = _make_factory(budget=default_budget)
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="chat_3", metadata=metadata, callbacks=_TEST_CALLBACKS)
        assert session._budget is default_budget

    def test_create_override_budget(self):
        default_budget = RenderBudget(engine_cmd="/deep")
        override_budget = RenderBudget(engine_cmd="/loop")
        factory = _make_factory(budget=default_budget)
        metadata = CardMetadata(engine_type="loop", mode_name="Loop", mode_emoji="🔄")
        session = factory.create(chat_id="chat_4", metadata=metadata, budget=override_budget, callbacks=_TEST_CALLBACKS)
        assert session._budget is override_budget

    def test_create_custom_session_id(self):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="spec", mode_name="Spec", mode_emoji="📋")
        session = factory.create(chat_id="chat_5", metadata=metadata, session_id="custom-id", callbacks=_TEST_CALLBACKS)
        assert session.session_id == "custom-id"

    def test_create_warns_on_empty_engine_type(self, caplog):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="", mode_name="Unknown", mode_emoji="❓")
        with caplog.at_level(logging.WARNING):
            factory.create(chat_id="chat_6", metadata=metadata, callbacks=_TEST_CALLBACKS)
        assert "engine_type is not set" in caplog.text


class TestCardSessionFactorySnapshot:
    """Tests for CardSessionFactory.create_snapshot()."""

    def test_snapshot_has_empty_chat_id(self):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create_snapshot(metadata=metadata)
        assert session._chat_id == ""

    def test_snapshot_session_id_prefix(self):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create_snapshot(metadata=metadata)
        assert session.session_id.startswith("snapshot-")

    def test_snapshot_has_no_hooks(self):
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create_snapshot(metadata=metadata)
        assert session._hooks == ()


class TestCardSessionFactoryErrorPaths:
    """Error-path tests for CardSessionFactory.create()."""

    def test_create_with_empty_chat_id(self):
        """Creating a session with empty chat_id should succeed (degraded mode)."""
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="", metadata=metadata)
        assert session._chat_id == ""

    def test_budget_clamping_applied(self, caplog):
        """When budget.visible_chars exceeds card_max_chars, factory clamps it."""
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        # Use a very large budget that should exceed max_chars
        huge_budget = RenderBudget(visible_chars=999_999)
        with caplog.at_level(logging.WARNING):
            session = factory.create(chat_id="c1", metadata=metadata, budget=huge_budget, callbacks=_TEST_CALLBACKS)
        # Budget should have been clamped (card_max_chars defaults to 28000)
        assert session._budget.visible_chars < 999_999
        assert "exceeds" in caplog.text.lower() or "clamping" in caplog.text.lower()


class TestSessionConfigBoundaryValues:
    """Boundary-value tests for SessionConfig via factory.create()."""

    def test_ttl_seconds_zero_raises(self):
        """ttl_seconds=0 should raise ValueError (use None for no TTL)."""
        import pytest
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        with pytest.raises(ValueError, match="ttl_seconds must be > 0"):
            factory.create(chat_id="c_ttl0", metadata=metadata, ttl_seconds=0, callbacks=_TEST_CALLBACKS)

    def test_ttl_seconds_none_disables_ttl(self):
        """ttl_seconds=None means TTL falls back to config default."""
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="c_ttl_none", metadata=metadata, ttl_seconds=None, callbacks=_TEST_CALLBACKS)
        # None gets resolved in core.py to config default (1800)
        assert session._ttl_seconds is not None

    def test_retry_delay_negative(self):
        """retry_delay=-1 should be passed through (no factory-level validation)."""
        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="c_retry", metadata=metadata, retry_delay=-1, callbacks=_TEST_CALLBACKS)
        assert session._timers._retry_delay == -1

    def test_clock_none_defaults_to_monotonic(self):
        """clock=None should resolve to time.monotonic in SessionConfig."""
        import time

        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="c_clock", metadata=metadata, clock=None, callbacks=_TEST_CALLBACKS)
        assert session._clock is time.monotonic


class TestWeakRefGC:
    """Verify WeakValueDictionary GC behavior in factory._sessions."""

    def test_session_removed_from_active_sessions_after_gc(self):
        """When all external references to a session are dropped, it should
        disappear from factory.active_sessions after GC."""
        import gc

        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")
        session = factory.create(chat_id="gc_test", metadata=metadata, callbacks=_TEST_CALLBACKS)
        session_id = session.session_id

        # Session should be in active_sessions
        assert session_id in factory.active_sessions

        # Close and delete the session
        session.close()
        del session
        gc.collect()
        gc.collect()
        gc.collect()

        # Should be removed from active_sessions via weak reference
        assert session_id not in factory.active_sessions

    def test_active_sessions_tracks_multiple_sessions(self):
        """Factory should track multiple sessions and remove only the GC'd one."""
        import gc

        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")

        s1 = factory.create(chat_id="gc_multi_1", metadata=metadata, callbacks=_TEST_CALLBACKS)
        s2 = factory.create(chat_id="gc_multi_2", metadata=metadata, callbacks=_TEST_CALLBACKS)
        s1_id = s1.session_id
        s2_id = s2.session_id

        assert s1_id in factory.active_sessions
        assert s2_id in factory.active_sessions

        # Remove s1 only
        s1.close()
        del s1
        gc.collect()
        gc.collect()

        assert s1_id not in factory.active_sessions
        assert s2_id in factory.active_sessions

        # Cleanup
        s2.close()


class TestFactoryRetryOnCapacityExhaustion:
    """Tests for retry logic when CardSession construction raises capacity errors."""

    def test_retry_success_on_second_attempt(self):
        """Factory retries on capacity exhausted, succeeds on 2nd attempt."""
        from unittest.mock import patch

        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")

        call_count = {"n": 0}
        _original_init = None

        def _flaky_init(self_cs, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("session lock capacity exhausted (10000/10000)")
            return _original_init(self_cs, *args, **kwargs)

        from src.card.session.core import CardSession

        _original_init = CardSession.__init__

        with patch.object(CardSession, "__init__", _flaky_init):
            with patch("src.card.session.factory.time.sleep") as mock_sleep:
                session = factory.create(chat_id="retry_ok", metadata=metadata, callbacks=_TEST_CALLBACKS)

        assert session is not None
        assert call_count["n"] == 2
        mock_sleep.assert_called_once()  # 1 retry → 1 sleep

    def test_retry_all_fail_raises(self):
        """Factory raises after all retry attempts exhausted."""
        import pytest
        from unittest.mock import patch

        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")

        from src.card.session.core import CardSession

        def _always_fail(self_cs, *args, **kwargs):
            raise RuntimeError("session lock capacity exhausted (10000/10000)")

        with patch.object(CardSession, "__init__", _always_fail):
            with patch("src.card.session.factory.time.sleep"):
                with pytest.raises(RuntimeError, match="capacity exhausted"):
                    factory.create(chat_id="retry_fail", metadata=metadata, callbacks=_TEST_CALLBACKS)

    def test_non_capacity_error_no_retry(self):
        """Non-capacity RuntimeError is raised immediately without retry."""
        import pytest
        from unittest.mock import patch

        factory = _make_factory()
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔵")

        from src.card.session.core import CardSession

        def _other_error(self_cs, *args, **kwargs):
            raise RuntimeError("some other error")

        with patch.object(CardSession, "__init__", _other_error):
            with patch("src.card.session.factory.time.sleep") as mock_sleep:
                with pytest.raises(RuntimeError, match="some other error"):
                    factory.create(chat_id="no_retry", metadata=metadata, callbacks=_TEST_CALLBACKS)
        mock_sleep.assert_not_called()
