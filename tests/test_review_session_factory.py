"""Tests for create_review_session + EphemeralReviewSession context manager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Import acp.models first to break the agent_session ⇄ acp.manager circular.
from src.acp.models import ACPEvent  # noqa: F401
from src.agent_session import EphemeralReviewSession, create_review_session


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.started = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


def test_create_review_session_claude(tmp_path):
    with patch("src.agent_session.factory.SyncClaudeCLISession") as Cls:
        fake = _FakeSession()
        Cls.return_value = fake
        s = create_review_session("claude", str(tmp_path))
        assert s is fake
        assert fake.started is True


def test_create_review_session_ttadk(tmp_path):
    with patch("src.ttadk.startup_common.precheck_ttadk_startup_model") as pre, \
         patch("src.agent_session.factory.SyncTTADKCLISession") as Cls:
        pre.return_value = {"model": "gpt-5.2"}
        fake = _FakeSession()
        Cls.return_value = fake
        s = create_review_session("ttadk_coco", str(tmp_path), model_name="gpt-5.2")
        assert s is fake
        assert fake.started is True
        # TTADK CLI session got the resolved model from precheck
        _, kwargs = Cls.call_args
        assert kwargs.get("model_name") == "gpt-5.2"


def test_create_review_session_acp(tmp_path):
    with patch("src.acp.sync_adapter.start_session_with_retry") as start, \
         patch("src.coco_model.get_coco_model_manager") as mgr:
        fake = _FakeSession()
        start.return_value = fake
        mgr.return_value.get_current_model.return_value = "default-model"
        s = create_review_session("coco", str(tmp_path))
        assert s is fake
        _, kwargs = start.call_args
        assert kwargs["agent_type"] == "coco"
        assert kwargs["model_name"] == "default-model"


def test_create_review_session_skips_wrappers(tmp_path):
    """Review session must NOT be wrapped with rate_limit / model_failure."""
    with patch("src.acp.sync_adapter.start_session_with_retry") as start, \
         patch("src.coco_model.get_coco_model_manager") as mgr, \
         patch("src.agent_session.factory.RateLimitAwareSession") as RLAS, \
         patch("src.agent_session.factory.ModelFailureAwareSession") as MFAS:
        fake = _FakeSession()
        start.return_value = fake
        mgr.return_value.get_current_model.return_value = "m"
        s = create_review_session("coco", str(tmp_path))
        assert s is fake
        RLAS.assert_not_called()
        MFAS.assert_not_called()


def test_ephemeral_closes_on_exit(tmp_path):
    fake = _FakeSession()
    with patch("src.agent_session.factory.create_review_session", return_value=fake):
        with EphemeralReviewSession("coco", str(tmp_path)) as s:
            assert s is fake
        assert fake.closed is True


def test_ephemeral_closes_on_exception(tmp_path):
    fake = _FakeSession()
    with patch("src.agent_session.factory.create_review_session", return_value=fake):
        with pytest.raises(RuntimeError, match="boom"):
            with EphemeralReviewSession("coco", str(tmp_path)) as s:
                assert s is fake
                raise RuntimeError("boom")
        assert fake.closed is True


def test_ephemeral_swallows_close_failure(tmp_path):
    class BadCloseSession(_FakeSession):
        def close(self):
            raise RuntimeError("close-failed")
    fake = BadCloseSession()
    with patch("src.agent_session.factory.create_review_session", return_value=fake):
        with EphemeralReviewSession("coco", str(tmp_path)):
            pass
    # exit must not raise despite close() failure
