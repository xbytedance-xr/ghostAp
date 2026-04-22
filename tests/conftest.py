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
