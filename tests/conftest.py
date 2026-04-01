"""Global test fixtures — blocks real ttadk CLI subprocess calls."""
from __future__ import annotations

import subprocess
import threading
from unittest.mock import patch

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
