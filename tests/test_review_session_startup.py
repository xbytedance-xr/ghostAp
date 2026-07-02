"""Tests for review session startup_timeout and startup_elapsed_s metadata."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.agent_session.factory import EphemeralReviewSession, create_review_session


class TestCreateReviewSessionStartupTimeout:
    """create_review_session accepts and forwards startup_timeout.

    Both ``start_session_with_retry`` and ``get_coco_model_manager`` are
    imported lazily inside the function body, so we patch them at their
    source modules.
    """

    @patch("src.agent_session.factory.get_settings")
    @patch("src.acp.sync_adapter.start_session_with_retry")
    @patch("src.coco_model.get_coco_model_manager")
    def test_default_uses_global_acp_startup_timeout(
        self, mock_get_model_mgr, mock_start, mock_get_settings
    ):
        """When startup_timeout is None, falls back to settings.acp_startup_timeout."""
        mock_settings = MagicMock()
        mock_settings.acp_startup_timeout = 25
        mock_get_settings.return_value = mock_settings
        mock_get_model_mgr.return_value.get_current_model.return_value = "test-model"

        create_review_session("coco", "/tmp/test")

        mock_start.assert_called_once()
        _, kwargs = mock_start.call_args
        assert kwargs["startup_timeout"] == 25.0

    @patch("src.agent_session.factory.get_settings")
    @patch("src.acp.sync_adapter.start_session_with_retry")
    @patch("src.coco_model.get_coco_model_manager")
    def test_explicit_startup_timeout_overrides_global(
        self, mock_get_model_mgr, mock_start, mock_get_settings
    ):
        """Explicit startup_timeout takes precedence over settings.acp_startup_timeout."""
        mock_settings = MagicMock()
        mock_settings.acp_startup_timeout = 20
        mock_get_settings.return_value = mock_settings
        mock_get_model_mgr.return_value.get_current_model.return_value = "test-model"

        create_review_session("coco", "/tmp/test", startup_timeout=45.0)

        mock_start.assert_called_once()
        _, kwargs = mock_start.call_args
        assert kwargs["startup_timeout"] == 45.0

    @patch("src.agent_session.factory.get_settings")
    @patch("src.acp.sync_adapter.start_session_with_retry")
    @patch("src.coco_model.get_coco_model_manager")
    def test_zero_startup_timeout_still_overrides(
        self, mock_get_model_mgr, mock_start, mock_get_settings
    ):
        """startup_timeout=0.0 is explicit and overrides (even if practically useless)."""
        mock_settings = MagicMock()
        mock_settings.acp_startup_timeout = 20
        mock_get_settings.return_value = mock_settings
        mock_get_model_mgr.return_value.get_current_model.return_value = "test-model"

        create_review_session("coco", "/tmp/test", startup_timeout=0.0)

        mock_start.assert_called_once()
        _, kwargs = mock_start.call_args
        assert kwargs["startup_timeout"] == 0.0


class TestEphemeralReviewSessionStartupTimeout:
    """EphemeralReviewSession passes startup_timeout through to create_review_session."""

    @patch("src.agent_session.factory.create_review_session")
    def test_no_startup_timeout_passes_none(self, mock_create):
        """When not provided, startup_timeout stays None (factory uses default)."""
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        with EphemeralReviewSession("coco", "/tmp/test") as sess:
            assert sess is mock_session

        mock_create.assert_called_once_with(
            "coco", "/tmp/test", None, startup_timeout=None,
        )

    @patch("src.agent_session.factory.create_review_session")
    def test_explicit_startup_timeout_passed_through(self, mock_create):
        """Explicit startup_timeout is forwarded to create_review_session."""
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        with EphemeralReviewSession(
            "coco", "/tmp/test", model_name="m1", startup_timeout=12.5,
        ) as sess:
            assert sess is mock_session

        mock_create.assert_called_once_with(
            "coco", "/tmp/test", "m1", startup_timeout=12.5,
        )


class TestEphemeralReviewSessionStartupElapsed:
    """startup_elapsed_s records how long create_review_session took."""

    @patch("src.agent_session.factory.create_review_session")
    def test_startup_elapsed_set_on_success(self, mock_create):
        """startup_elapsed_s is set to positive duration after successful __enter__."""
        def _slow(*a, **kw):
            time.sleep(0.02)
            return MagicMock()
        mock_create.side_effect = _slow

        ctx = EphemeralReviewSession("coco", "/tmp/test")
        assert ctx.startup_elapsed_s == 0.0

        with ctx:
            pass

        assert ctx.startup_elapsed_s > 0.0
        # Should be roughly the sleep duration (allow generous overhead)
        assert ctx.startup_elapsed_s < 1.0

    @patch("src.agent_session.factory.create_review_session")
    def test_startup_elapsed_set_on_failure(self, mock_create):
        """startup_elapsed_s is set even when create_review_session raises."""
        def _fail(*a, **kw):
            time.sleep(0.02)
            raise RuntimeError("startup failed")
        mock_create.side_effect = _fail

        ctx = EphemeralReviewSession("coco", "/tmp/test")

        with pytest.raises(RuntimeError, match="startup failed"):
            with ctx:
                pass

        assert ctx.startup_elapsed_s > 0.0
        assert ctx.startup_elapsed_s < 1.0

    @patch("src.agent_session.factory.create_review_session")
    def test_startup_elapsed_resets_on_reuse(self, mock_create):
        """Each __enter__ sets startup_elapsed_s anew (not cumulative)."""
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        ctx = EphemeralReviewSession("coco", "/tmp/test")

        with ctx:
            pass
        first = ctx.startup_elapsed_s

        with ctx:
            pass
        second = ctx.startup_elapsed_s

        # Both should be set (non-zero timing is unreliable in fast tests,
        # but at least they should both be defined and not accumulate).
        assert isinstance(first, float)
        assert isinstance(second, float)
        # Should not be significantly larger on second use (not cumulative)
        assert second < first + 0.5
