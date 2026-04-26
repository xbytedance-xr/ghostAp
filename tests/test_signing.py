"""Unit tests for src/utils/signing.py (HMAC command-signature utilities)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.utils.signing import (
    VerifyResult,
    _compute_command_sig,
    _get_process_start_date,
    _get_signing_key,
    _verify_legacy_sha256_fallback,
    verify_command_sig,
)

_TEST_KEY = "test_secret_signing_key"


# ---------------------------------------------------------------------------
# _get_signing_key
# ---------------------------------------------------------------------------


class TestGetSigningKey:

    def test_returns_app_secret(self):
        mock_settings = MagicMock()
        mock_settings.app_secret = "my_secret"
        with patch("src.config.get_settings", return_value=mock_settings):
            assert _get_signing_key() == "my_secret"

    def test_fallback_empty_on_exception(self):
        with patch("src.config.get_settings", side_effect=RuntimeError("boom")):
            assert _get_signing_key() == ""

    def test_logs_warning_on_exception(self):
        with patch("src.config.get_settings", side_effect=RuntimeError("boom")), \
             patch("src.utils.signing.logger") as mock_logger:
            _get_signing_key()
            mock_logger.warning.assert_called_once()
            assert "signing key" in mock_logger.warning.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# _compute_command_sig
# ---------------------------------------------------------------------------


class TestComputeCommandSig:

    def test_produces_hmac_sha256(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("hello")
        expected = hmac.new(
            _TEST_KEY.encode(), "hello".encode(), hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_output_is_64_char_hex(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("test")
        assert len(sig) == 64
        int(sig, 16)  # valid hex

    def test_differs_from_plain_sha256(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("hello")
        plain = hashlib.sha256("hello".encode()).hexdigest()
        assert sig != plain

    def test_different_keys_different_sigs(self):
        with patch("src.utils.signing._get_signing_key", return_value="key_a"):
            sig_a = _compute_command_sig("hello")
        with patch("src.utils.signing._get_signing_key", return_value="key_b"):
            sig_b = _compute_command_sig("hello")
        assert sig_a != sig_b

    def test_deterministic(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            assert _compute_command_sig("/status") == _compute_command_sig("/status")

    def test_raises_on_empty_key(self):
        with patch("src.utils.signing._get_signing_key", return_value=""):
            with pytest.raises(ValueError, match="signing key is empty"):
                _compute_command_sig("any")


# ---------------------------------------------------------------------------
# verify_command_sig
# ---------------------------------------------------------------------------


class TestVerifyCommandSig:

    def test_valid_sig_accepted(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("cmd")
            result = verify_command_sig("cmd", sig)
            assert result is VerifyResult.OK
            assert bool(result) is True

    def test_tampered_text_rejected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("original")
            result = verify_command_sig("tampered", sig)
            assert result is VerifyResult.MISMATCH
            assert bool(result) is False

    def test_empty_sig_rejected(self):
        result = verify_command_sig("cmd", "")
        assert result is VerifyResult.MISMATCH
        assert bool(result) is False

    def test_wrong_sig_rejected(self):
        result = verify_command_sig("cmd", "deadbeef" * 8)
        assert result is VerifyResult.MISMATCH
        assert bool(result) is False


# ---------------------------------------------------------------------------
# _verify_legacy_sha256_fallback
# ---------------------------------------------------------------------------


class TestLegacySha256Fallback:

    def test_accepted_within_window(self):
        cmd = "/status"
        plain_sig = hashlib.sha256(cmd.encode()).hexdigest()

        mock_settings = MagicMock()
        mock_settings.sig_compat_deploy_date = date.today().isoformat()
        mock_settings.sig_compat_window_days = 7

        with patch("src.config.get_settings", return_value=mock_settings):
            assert _verify_legacy_sha256_fallback(cmd, plain_sig) is VerifyResult.OK

    def test_rejected_outside_window(self):
        cmd = "/status"
        plain_sig = hashlib.sha256(cmd.encode()).hexdigest()

        mock_settings = MagicMock()
        past = date.today() - timedelta(days=30)
        mock_settings.sig_compat_deploy_date = past.isoformat()
        mock_settings.sig_compat_window_days = 7

        with patch("src.config.get_settings", return_value=mock_settings):
            result = _verify_legacy_sha256_fallback(cmd, plain_sig)
            assert result is VerifyResult.COMPAT_EXPIRED

    def test_rejected_on_settings_exception(self):
        cmd = "/test"
        plain_sig = hashlib.sha256(cmd.encode()).hexdigest()
        with patch("src.config.get_settings", side_effect=RuntimeError("no settings")):
            # Legacy matches but settings fail → window_open=False → COMPAT_EXPIRED
            result = _verify_legacy_sha256_fallback(cmd, plain_sig)
            assert result is VerifyResult.COMPAT_EXPIRED

    def test_wrong_sig_returns_mismatch(self):
        """When even legacy SHA-256 doesn't match, return MISMATCH."""
        cmd = "/test"
        with patch("src.config.get_settings", side_effect=RuntimeError("no settings")):
            result = _verify_legacy_sha256_fallback(cmd, "not_a_valid_sig")
            assert result is VerifyResult.MISMATCH

    def test_empty_deploy_date_uses_process_start(self):
        """When sig_compat_deploy_date is empty, _PROCESS_START_DATE is used."""
        cmd = "/test"
        plain_sig = hashlib.sha256(cmd.encode()).hexdigest()

        mock_settings = MagicMock()
        mock_settings.sig_compat_deploy_date = ""  # empty → fallback
        mock_settings.sig_compat_window_days = 7

        with patch("src.config.get_settings", return_value=mock_settings):
            # _PROCESS_START_DATE is today at import time, so within 7-day window
            assert _verify_legacy_sha256_fallback(cmd, plain_sig) is VerifyResult.OK


# ---------------------------------------------------------------------------
# VerifyResult enum
# ---------------------------------------------------------------------------


class TestVerifyResult:

    def test_ok_is_truthy(self):
        assert bool(VerifyResult.OK) is True

    def test_mismatch_is_falsy(self):
        assert bool(VerifyResult.MISMATCH) is False

    def test_compat_expired_is_falsy(self):
        assert bool(VerifyResult.COMPAT_EXPIRED) is False

    def test_if_idiom_ok(self):
        """VerifyResult.OK works in plain `if` checks."""
        assert VerifyResult.OK  # truthy
        assert not VerifyResult.MISMATCH  # falsy
        assert not VerifyResult.COMPAT_EXPIRED  # falsy

    def test_verify_compat_expired_via_verify_command_sig(self):
        """Full flow: legacy sig with expired window → COMPAT_EXPIRED."""
        cmd = "/test"
        plain_sig = hashlib.sha256(cmd.encode()).hexdigest()

        mock_settings = MagicMock()
        past = date.today() - timedelta(days=30)
        mock_settings.sig_compat_deploy_date = past.isoformat()
        mock_settings.sig_compat_window_days = 7
        mock_settings.app_secret = _TEST_KEY

        with patch("src.config.get_settings", return_value=mock_settings):
            result = verify_command_sig(cmd, plain_sig)
            assert result is VerifyResult.COMPAT_EXPIRED


# ---------------------------------------------------------------------------
# _get_process_start_date (lazy initialization)
# ---------------------------------------------------------------------------


class TestGetProcessStartDate:

    def test_lazy_init_returns_date(self):
        """_get_process_start_date returns a date object."""
        import src.utils.signing as _mod
        original = _mod._PROCESS_START_DATE
        try:
            _mod._PROCESS_START_DATE = None
            result = _get_process_start_date()
            assert isinstance(result, date)
            assert result == date.today()
        finally:
            _mod._PROCESS_START_DATE = original

    def test_lazy_init_caches(self):
        """Second call returns the same cached value without re-computing."""
        import src.utils.signing as _mod
        original = _mod._PROCESS_START_DATE
        try:
            _mod._PROCESS_START_DATE = None
            first = _get_process_start_date()
            second = _get_process_start_date()
            assert first is second
            assert _mod._PROCESS_START_DATE is first
        finally:
            _mod._PROCESS_START_DATE = original

    def test_lazy_init_logs_info(self):
        """First initialization logs an info message."""
        import src.utils.signing as _mod
        original = _mod._PROCESS_START_DATE
        try:
            _mod._PROCESS_START_DATE = None
            with patch("src.utils.signing.logger") as mock_logger:
                _get_process_start_date()
                mock_logger.info.assert_called_once()
                assert "_PROCESS_START_DATE" in mock_logger.info.call_args[0][0]
        finally:
            _mod._PROCESS_START_DATE = original

    def test_no_reinit_when_already_set(self):
        """When _PROCESS_START_DATE is already set, no re-initialization."""
        import src.utils.signing as _mod
        original = _mod._PROCESS_START_DATE
        sentinel = date(2020, 1, 1)
        try:
            _mod._PROCESS_START_DATE = sentinel
            result = _get_process_start_date()
            assert result is sentinel
        finally:
            _mod._PROCESS_START_DATE = original
