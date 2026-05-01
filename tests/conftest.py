"""Global test fixtures — blocks real ttadk CLI subprocess calls."""
from __future__ import annotations

import subprocess
import threading
from unittest.mock import patch, MagicMock

import pytest

from src.utils.retry import RetryPolicy

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
            settings = make_settings(loop_max_iterations=5)
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
        # Loop engine
        loop_max_iterations=15,
        loop_convergence_window=3,
        loop_execution_timeout=300,
        loop_max_context_tokens=200000,
        loop_review_enabled=False,
        loop_review_extra_iterations=3,
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
    )

    def _factory(**overrides):
        merged = {**_DEFAULTS, **overrides}
        s = MagicMock()
        for k, v in merged.items():
            setattr(s, k, v)
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


# ---------------------------------------------------------------------------
# lru_cache hygiene — clear module-level caches between tests.
# MAINTAIN: when adding a new @lru_cache in src/, register its cache_clear here.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_direct_session():
    """Mock _create_direct_session to return a mock DirectCardSession.

    The mock session tracks send/close calls and delegates to handler methods
    for test assertions compatibility.
    """

    def _make_mock_session(handler, chat_id, message_id, *, session_id=None):
        session = MagicMock()
        session._handler = handler
        session._message_id = None
        session._closed = False
        session.closed = False

        def _send(card_content):
            if isinstance(card_content, str):
                import json
                try:
                    card_content = json.loads(card_content)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Delegate to handler for test assertion compatibility
            if session._message_id:
                # Update path
                content_str = card_content if isinstance(card_content, str) else \
                    __import__("json").dumps(card_content, ensure_ascii=False)
                handler.update_card(session._message_id, content_str)
            else:
                # Create path
                content_str = card_content if isinstance(card_content, str) else \
                    __import__("json").dumps(card_content, ensure_ascii=False)
                result = handler.reply_card(message_id, content_str)
                session._message_id = result or "mock_reply_id"
            return session._message_id

        def _close():
            session._closed = True
            session.closed = True
            session._message_id = None

        session.send.side_effect = _send
        session.close.side_effect = _close
        type(session).message_id = property(lambda s: s._message_id)
        return session

    with patch("src.feishu.renderers.deep_renderer._create_direct_session", side_effect=_make_mock_session), \
         patch("src.feishu.renderers.loop_renderer._create_direct_session", side_effect=_make_mock_session), \
         patch("src.feishu.renderers.spec_renderer._create_direct_session", side_effect=_make_mock_session):
        yield


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
