"""Unit tests for src/utils/signing.py (HMAC command-signature utilities).

Covers both legacy v1 (HMAC-only) and new v2 (nonce+exp+chat_id) signing.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.utils.signing import (
    VerifyResult,
    _USED_NONCES,
    _compute_command_sig,
    _get_process_start_date,
    _get_signing_key,
    _is_v2_sig,
    _record_nonce,
    _verify_legacy_sha256_fallback,
    _verify_v2_sig,
    sign_command,
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


# ---------------------------------------------------------------------------
# _compute_command_sig (legacy v1)
# ---------------------------------------------------------------------------


class TestComputeCommandSig:

    def test_produces_hmac_sha256(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("hello")
        expected = hmac.new(
            _TEST_KEY.encode(), "hello".encode(), hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_different_keys_different_sigs(self):
        with patch("src.utils.signing._get_signing_key", return_value="key_a"):
            sig_a = _compute_command_sig("hello")
        with patch("src.utils.signing._get_signing_key", return_value="key_b"):
            sig_b = _compute_command_sig("hello")
        assert sig_a != sig_b

    def test_raises_on_empty_key(self):
        with patch("src.utils.signing._get_signing_key", return_value=""):
            with pytest.raises(ValueError, match="signing key is empty"):
                _compute_command_sig("any")


# ---------------------------------------------------------------------------
# sign_command (v2: nonce + exp + chat_id)
# ---------------------------------------------------------------------------


class TestSignCommand:

    def test_returns_dot_separated_payload(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_abc123")
        parts = payload.split(".")
        assert len(parts) == 4

    def test_exp_is_future_timestamp(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_abc123", ttl_seconds=3600)
        exp_str = payload.split(".")[1]
        exp = int(exp_str)
        assert exp > time.time()
        assert exp <= time.time() + 3601

    def test_different_commands_different_sigs(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            s1 = sign_command("/deploy", "chat1")
            s2 = sign_command("/rollback", "chat1")
        assert s1.split(".")[0] != s2.split(".")[0]

    def test_different_chats_different_sigs(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            s1 = sign_command("/deploy", "chat_A")
            s2 = sign_command("/deploy", "chat_B")
        assert s1.split(".")[3] != s2.split(".")[3]

    def test_raises_on_empty_key(self):
        with patch("src.utils.signing._get_signing_key", return_value=""):
            with pytest.raises(ValueError, match="signing key is empty"):
                sign_command("/deploy", "chat1")


# ---------------------------------------------------------------------------
# _is_v2_sig
# ---------------------------------------------------------------------------


class TestIsV2Sig:

    def test_v2_format_detected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/cmd", "chat1")
        assert _is_v2_sig(payload) is True

    def test_v1_format_not_v2(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("/cmd")
        assert _is_v2_sig(sig) is False

    def test_random_string_not_v2(self):
        assert _is_v2_sig("not_a_signature") is False

    def test_empty_not_v2(self):
        assert _is_v2_sig("") is False


# ---------------------------------------------------------------------------
# verify_command_sig — v2 format
# ---------------------------------------------------------------------------


class TestVerifyCommandSigV2:

    def test_valid_v2_sig_accepted(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_123")
            result = verify_command_sig("/deploy", payload, chat_id="chat_123")
        assert result is VerifyResult.OK
        assert bool(result) is True

    def test_expired_sig_rejected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            # Sign with ttl=0 (already expired)
            payload = sign_command("/deploy", "chat_123", ttl_seconds=-1)
            result = verify_command_sig("/deploy", payload, chat_id="chat_123")
        assert result is VerifyResult.EXPIRED
        assert bool(result) is False

    def test_nonce_replay_rejected(self):
        """Second verification with same payload should fail (nonce reuse)."""
        import src.utils.signing as _mod
        # Clear nonce store to avoid interference from other tests
        _mod._USED_NONCES.clear()
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_123")
            # First verify should succeed
            result1 = verify_command_sig("/deploy", payload, chat_id="chat_123")
            assert result1 is VerifyResult.OK
            # Second verify should fail (nonce already consumed)
            result2 = verify_command_sig("/deploy", payload, chat_id="chat_123")
            assert result2 is VerifyResult.NONCE_REUSED
            assert bool(result2) is False

    def test_chat_id_mismatch_rejected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_A")
            result = verify_command_sig("/deploy", payload, chat_id="chat_B")
        assert result is VerifyResult.CHAT_MISMATCH
        assert bool(result) is False

    def test_tampered_command_rejected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_123")
            result = verify_command_sig("/tampered", payload, chat_id="chat_123")
        assert result is VerifyResult.MISMATCH
        assert bool(result) is False

    def test_tampered_sig_rejected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_123")
        # Tamper with the sig portion
        parts = payload.split(".")
        parts[0] = "a" * 64
        tampered = ".".join(parts)
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            result = verify_command_sig("/deploy", tampered, chat_id="chat_123")
        assert result is VerifyResult.MISMATCH

    def test_v2_without_chat_id_still_verifies(self):
        """When chat_id is not provided, v2 sig is verified without chat check."""
        import src.utils.signing as _mod
        _mod._USED_NONCES.clear()
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/deploy", "chat_123")
            # Verify without chat_id — should still check nonce+exp+hmac
            result = verify_command_sig("/deploy", payload)
        assert result is VerifyResult.OK


# ---------------------------------------------------------------------------
# verify_command_sig — v1 format (backward compatibility)
# ---------------------------------------------------------------------------


class TestVerifyCommandSigV1Compat:

    def test_v1_sig_accepted(self):
        """Old HMAC-only format should still pass within compat window."""
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("cmd")
            result = verify_command_sig("cmd", sig)
            assert result is VerifyResult.OK
            assert bool(result) is True

    def test_v1_sig_accepted_with_chat_id(self):
        """Old format accepted even when chat_id is provided (it's not v2)."""
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            sig = _compute_command_sig("cmd")
            result = verify_command_sig("cmd", sig, chat_id="any_chat")
            assert result is VerifyResult.OK

    def test_v1_wrong_sig_rejected(self):
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            result = verify_command_sig("cmd", "deadbeef" * 8)
        assert result is VerifyResult.MISMATCH

    def test_legacy_sha256_within_window(self):
        """Plain SHA-256 accepted within compat window."""
        cmd = "/status"
        plain_sig = hashlib.sha256(cmd.encode()).hexdigest()

        mock_settings = MagicMock()
        mock_settings.sig_compat_deploy_date = date.today().isoformat()
        mock_settings.sig_compat_window_days = 7
        mock_settings.app_secret = _TEST_KEY

        with patch("src.config.get_settings", return_value=mock_settings):
            result = verify_command_sig(cmd, plain_sig)
            assert result is VerifyResult.OK

    def test_legacy_sha256_outside_window(self):
        """Plain SHA-256 rejected outside compat window → COMPAT_EXPIRED."""
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
# _record_nonce
# ---------------------------------------------------------------------------


class TestRecordNonce:

    def test_first_use_returns_false(self):
        import src.utils.signing as _mod
        _mod._USED_NONCES.clear()
        assert _record_nonce("unique_nonce_1") is False

    def test_reuse_returns_true(self):
        import src.utils.signing as _mod
        _mod._USED_NONCES.clear()
        _record_nonce("reused_nonce")
        assert _record_nonce("reused_nonce") is True

    def test_evicts_oldest_when_full(self):
        import src.utils.signing as _mod
        _mod._USED_NONCES.clear()
        # Fill to capacity
        for i in range(_mod._MAX_NONCES):
            _record_nonce(f"nonce_{i}")
        assert len(_mod._USED_NONCES) == _mod._MAX_NONCES
        # Add one more — oldest should be evicted
        _record_nonce("new_nonce")
        assert len(_mod._USED_NONCES) == _mod._MAX_NONCES
        assert "nonce_0" not in _mod._USED_NONCES
        assert "new_nonce" in _mod._USED_NONCES


# ---------------------------------------------------------------------------
# VerifyResult enum
# ---------------------------------------------------------------------------


class TestVerifyResult:

    def test_ok_is_truthy(self):
        assert bool(VerifyResult.OK) is True

    def test_mismatch_is_falsy(self):
        assert bool(VerifyResult.MISMATCH) is False


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


# ---------------------------------------------------------------------------
# Integration: full round-trip sign → verify
# ---------------------------------------------------------------------------


class TestSignVerifyRoundTrip:

    def test_sign_then_verify_same_chat(self):
        """Full round-trip: sign and verify with same command and chat_id."""
        import src.utils.signing as _mod
        _mod._USED_NONCES.clear()
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/restart", "oc_chatid_xyz")
            result = verify_command_sig("/restart", payload, chat_id="oc_chatid_xyz")
        assert result is VerifyResult.OK

    def test_sign_then_verify_wrong_chat(self):
        """Signature bound to one chat cannot be used in another."""
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/restart", "chat_A")
            result = verify_command_sig("/restart", payload, chat_id="chat_B")
        assert result is VerifyResult.CHAT_MISMATCH

    def test_expired_sig_roundtrip(self):
        """Signature with negative TTL is expired immediately."""
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/restart", "chat_A", ttl_seconds=-10)
            result = verify_command_sig("/restart", payload, chat_id="chat_A")
        assert result is VerifyResult.EXPIRED

    def test_replay_protection_roundtrip(self):
        """Same payload cannot be verified twice (nonce replay)."""
        import src.utils.signing as _mod
        _mod._USED_NONCES.clear()
        with patch("src.utils.signing._get_signing_key", return_value=_TEST_KEY):
            payload = sign_command("/restart", "chat_A")
            r1 = verify_command_sig("/restart", payload, chat_id="chat_A")
            r2 = verify_command_sig("/restart", payload, chat_id="chat_A")
        assert r1 is VerifyResult.OK
        assert r2 is VerifyResult.NONCE_REUSED
