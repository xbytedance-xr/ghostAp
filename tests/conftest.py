"""Global test fixtures — blocks real ttadk CLI subprocess calls."""
from __future__ import annotations

import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# ACP compatibility shim — bridge renamed symbols between acp versions.
# The source code uses KillTerminalCommandResponse (acp >=0.11) but the
# installed version may only expose KillTerminalResponse (acp 0.10.x).
# ---------------------------------------------------------------------------
try:
    import acp.schema as _acp_schema

    if not hasattr(_acp_schema, "KillTerminalCommandResponse") and hasattr(_acp_schema, "KillTerminalResponse"):
        _acp_schema.KillTerminalCommandResponse = _acp_schema.KillTerminalResponse
except ImportError:
    pass

from src.card.delivery.engine import CardDelivery
from src.card.delivery.registry import delivery_registry
from src.card.session import CardSession
from src.card.state.models import CardMetadata
from src.utils.retry import RetryPolicy


@pytest.fixture(autouse=True)
def _reset_delivery_registry():
    """Reset DeliveryRegistry state between tests for isolation."""
    delivery_registry.reset()
    yield
    delivery_registry.reset()


@pytest.fixture(autouse=True)
def _force_sync_delivery(monkeypatch):
    """Force all CardSession instances to use synchronous delivery in tests.

    The production code uses a thread pool for async delivery, but tests need
    deterministic behavior. We patch __init__ to override _sync_delivery=True
    regardless of what SessionConfig.sync_delivery was set to.
    """
    import inspect

    # Defensive assertion: ensure CardSession.__init__ still sets _sync_delivery.
    # If the constructor signature changes, this will fail loudly instead of
    # silently leaving tests with async delivery (causing flaky failures).
    _init_src = inspect.getsource(CardSession.__init__)
    assert "_sync_delivery" in _init_src, (
        "CardSession.__init__ no longer sets _sync_delivery. "
        "Update this fixture to match the new constructor pattern."
    )

    _orig_init = CardSession.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        # Override: always sync in tests for determinism
        self._sync_delivery = True

    monkeypatch.setattr(CardSession, "__init__", _patched_init)
    yield


# ---------------------------------------------------------------------------
# Shared TrackingClient for adapter sequence integration tests.
# ---------------------------------------------------------------------------

class TrackingClient:
    """Mock Feishu API client that tracks all card operations.

    Uses dynamic IDs based on creation count.
    """

    def __init__(self):
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self.created.append(card_json)
        idx = len(self.created)
        return (f"msg_{idx}", f"card_{idx}")

    def update_card(self, card_id, card_json, *, sequence=0):
        self.updated.append(card_json)


@pytest.fixture
def tracking_client() -> TrackingClient:
    """Provide a fresh TrackingClient instance."""
    return TrackingClient()


@pytest.fixture
def make_card_delivery():
    """Factory fixture: create a CardDelivery from a TrackingClient.

    Teardown calls shutdown() on all created deliveries to stop background threads.
    """
    deliveries: list[CardDelivery] = []

    def _factory(client: TrackingClient | None = None) -> tuple[CardDelivery, TrackingClient]:
        if client is None:
            client = TrackingClient()
        d = CardDelivery(client)
        deliveries.append(d)
        return d, client

    yield _factory

    for d in deliveries:
        try:
            d._shutdown()
        except Exception:
            pass


@pytest.fixture
def make_card_session():
    """Factory fixture: create a CardSession wired to a delivery.

    Teardown calls close() on all created sessions and shutdown() on deliveries.
    """
    sessions: list[CardSession] = []
    deliveries: list[CardDelivery] = []

    def _factory(
        *,
        chat_id: str = "chat_test",
        engine_type: str = "deep",
        mode_name: str = "Test Agent",
        delivery: CardDelivery | None = None,
        client: TrackingClient | None = None,
        session_id: str | None = None,
    ) -> tuple[CardSession, TrackingClient]:
        if client is None:
            client = TrackingClient()
        if delivery is None:
            delivery = CardDelivery(client)
            deliveries.append(delivery)
        metadata = CardMetadata(engine_type=engine_type, mode_name=mode_name)
        from src.card.session.config import SessionCallbacks, SessionConfig
        config = SessionConfig(metadata=metadata, sync_delivery=True)
        session = CardSession(
            chat_id=chat_id,
            config=config,
            delivery=delivery,
            session_id=session_id,
            callbacks=SessionCallbacks(notify_callback=lambda _cid, _txt: None),
        )
        sessions.append(session)
        return session, client

    yield _factory

    for s in sessions:
        try:
            s.close()
        except Exception:
            pass
    for d in deliveries:
        try:
            d._shutdown()
        except Exception:
            pass


@pytest.fixture
def make_session_rotator(make_card_session):
    """Factory fixture: create a SessionRotator with auto-cleanup.

    Args (keyword only):
        factory: Optional callable returning a CardSession. Defaults to
                 a factory that creates sessions via make_card_session.
        engine_type: Engine type for the initial session (default "deep").
        session_id: Optional custom initial session ID.

    Returns:
        Tuple of (SessionRotator, initial_CardSession, TrackingClient).
    """
    from src.card.session.rotator import SessionRotator

    rotators: list[SessionRotator] = []

    def _factory(
        *,
        factory: object = None,
        engine_type: str = "deep",
        session_id: str | None = None,
    ):
        initial_session, client = make_card_session(
            engine_type=engine_type,
            session_id=session_id,
        )
        rotator = SessionRotator(initial_session)
        rotators.append(rotator)
        return rotator, initial_session, client

    yield _factory

    for r in rotators:
        try:
            r.close()
        except Exception:
            pass


_REAL_RUN = subprocess.run
_REAL_POPEN_INIT = subprocess.Popen.__init__


# ---------------------------------------------------------------------------
# Common pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cancel_event():
    return threading.Event()


@pytest.fixture
def fast_retry_policy():
    return RetryPolicy(max_retries=2, retry_delay=0.01, jitter_factor=0)


@pytest.fixture
def make_settings():
    """Factory fixture returning a MagicMock settings object with sane defaults.

    Usage::

        def test_something(make_settings):
            settings = make_settings(spec_execution_timeout=60)
    """
    # Sensible defaults covering every engine subsystem
    _DEFAULTS = dict(
        # ACP / agent session
        acp_startup_timeout=20,
        acp_auto_update_timeout=120,
        acp_healthcheck_timeout=2.0,
        acp_auto_update=True,
        rate_limit_retry_enabled=False,
        # Deep engine
        coco_execution_timeout=300,
        claude_execution_timeout=600,
        # Spec engine
        spec_execution_timeout=300,
        spec_persist_every_phase=False,
        spec_review_enabled=False,
        spec_discovery_enabled=False,
        spec_cycle_tasks_max=5,
        spec_max_cycles_limit=10,
        spec_max_retries=2,
        spec_persist_phase_artifacts=False,
        # Engine shared
        engine_eval_prompt_timeout=60,
        engine_timeout_warning_threshold=600,
        # Streaming
        streaming_adaptive_interval_base=0.5,
        streaming_adaptive_interval_max=3.0,
        streaming_adaptive_rate_low=10.0,
        streaming_adaptive_rate_high=100.0,
        # Card session / lock
        lock_undo_window_seconds=300,
    )

    def _factory(**overrides):
        merged = {**_DEFAULTS, **overrides}
        s = MagicMock()
        for k, v in merged.items():
            setattr(s, k, v)
        # Provide nested card config with sane defaults (avoids MagicMock in math ops)
        if not hasattr(s, "card") or isinstance(s.card, MagicMock):
            card_mock = MagicMock()
            card_mock.session_idle_timeout = 1800
            card_mock.session_idle_warn_at_remaining = 300
            card_mock.session_lock_max = 200
            card_mock.session_lock_ttl = 7200
            card_mock.session_max_rotations = 10
            card_mock.max_chars = 28000
            card_mock.button_size = "default"
            card_mock.button_layout = "responsive"
            card_mock.mobile_force_vertical = False
            card_mock.continuation_enabled = True
            card_mock.action_dedup_ttl = 5
            card_mock.action_dedup_max_size = 200
            s.card = card_mock
        return s

    return _factory


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------


def _is_ttadk_cmd(args) -> bool:
    if isinstance(args, (list, tuple)):
        return any("ttadk" in str(a) for a in args[:2])
    return "ttadk" in str(args)


def _guarded_run(args, *a, **kw):
    if _is_ttadk_cmd(args):
        raise RuntimeError(
            f"[conftest] Real ttadk subprocess.run blocked in tests: {args!r}. "
            "Inject a fake runner or mock subprocess."
        )
    return _REAL_RUN(args, *a, **kw)


def _guarded_popen_init(self, args, *a, **kw):
    if _is_ttadk_cmd(args):
        raise RuntimeError(
            f"[conftest] Real ttadk subprocess.Popen blocked in tests: {args!r}. "
            "Inject a fake runner or mock subprocess."
        )
    return _REAL_POPEN_INIT(self, args, *a, **kw)


@pytest.fixture(autouse=True)
def _block_real_ttadk_subprocess(request):
    if request.node.get_closest_marker("allow_real_ttadk"):
        yield
        return
    with patch("subprocess.run", side_effect=_guarded_run), \
         patch.object(subprocess.Popen, "__init__", _guarded_popen_init):
        yield


@pytest.fixture(autouse=True)
def _block_real_slock_storage(monkeypatch, tmp_path):
    """Prevent tests from accidentally writing to ~/.ghostap/slock/.

    Any test that creates a SlockEngine or MemoryManager without explicitly
    passing memory_base_path/base_path will get a RuntimeError instead of
    silently polluting the real storage directory.
    """
    def _guarded_default():
        raise RuntimeError(
            "[conftest] Real default_slock_storage_base() called in tests! "
            "Pass memory_base_path=str(tmp_path) to SlockEngine or "
            "base_path=str(tmp_path) to MemoryManager."
        )
    try:
        monkeypatch.setattr(
            "src.slock_engine.memory_manager.default_slock_storage_base",
            _guarded_default,
        )
    except Exception:
        pass  # Module not yet imported — safe to skip


# ---------------------------------------------------------------------------
# lru_cache hygiene — clear module-level caches between tests.
# MAINTAIN: when adding a new @lru_cache in src/, register its cache_clear here.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_hook_executor_between_tests():
    """Reset the global hook executor to prevent cross-test state leakage."""
    yield
    from src.card.hooks import _reset_hook_executor
    _reset_hook_executor()


@pytest.fixture
def failing_handler_session():
    """Configurable fixture for simulating handler method failures.

    Returns a factory that patches handler methods to return failure values.

    Usage::

        def test_card_failure(failing_handler_session):
            handler = make_handler()
            with failing_handler_session(handler, reply_card=None, update_card=False):
                # handler.reply_card(...) returns None
                # handler.update_card(...) returns False
                ...
    """
    from contextlib import contextmanager

    @contextmanager
    def _configure(handler, *, reply_card=None, update_card=False):
        original_reply_card = handler.reply_card
        original_update_card = handler.update_card

        handler.reply_card = MagicMock(return_value=reply_card)
        handler.update_card = MagicMock(return_value=update_card)
        try:
            yield handler
        finally:
            handler.reply_card = original_reply_card
            handler.update_card = original_update_card

    return _configure


@pytest.fixture(autouse=True)
def _clear_all_lru_caches():
    """Clear all known module-level lru_caches after each test."""
    yield
    # sync_adapter caches
    try:
        from src.acp.sync_adapter import _probe_acp_serve_help, _supports_acp_serve
        _probe_acp_serve_help.cache_clear()
        _supports_acp_serve.cache_clear()
    except Exception:
        pass
    # diagnostics caches
    try:
        from src.acp.diagnostics import _compile_redaction_patterns
        _compile_redaction_patterns.cache_clear()
    except Exception:
        pass
    # renderer signature cache
    try:
        from src.card.render.renderer import _compute_sig_cached
        _compute_sig_cached.cache_clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Singleton hygiene — reset global singletons so tests start from clean state.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_all_singletons():
    """Reset all known global singletons after each test."""
    yield
    try:
        from src.config import _reset_settings_for_testing
        _reset_settings_for_testing()
    except Exception:
        pass
    try:
        from src.coco_model.manager import _reset_coco_model_manager_for_testing
        _reset_coco_model_manager_for_testing()
    except Exception:
        pass
    try:
        from src.ttadk.manager import _reset_ttadk_manager_for_testing
        _reset_ttadk_manager_for_testing()
    except Exception:
        pass
    try:
        from src.thread.manager import _reset_thread_manager_for_testing
        _reset_thread_manager_for_testing()
    except Exception:
        pass
    try:
        from src.acp.providers import _reset_providers_for_testing
        _reset_providers_for_testing()
    except Exception:
        pass
    try:
        from src.utils.env import _reset_env_for_testing
        _reset_env_for_testing()
    except Exception:
        pass
