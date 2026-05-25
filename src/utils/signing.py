"""HMAC command-signature utilities.

Provides tamper-detection signing and verification for Feishu card action
payloads.  Extracted from ``card.builders.lock`` to keep cryptographic
logic separate from UI card construction (Single Responsibility Principle).

v2 adds nonce + expiry + chat_id binding to prevent replay attacks.
The old HMAC-only format is supported within the compatibility window.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from datetime import date as _date
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-initialized on first use — avoids capturing a stale date at import time
# when the module is loaded during early bootstrap (e.g. pre-fork workers).
_PROCESS_START_DATE: Optional[_date] = None

# Anti-replay nonce store (bounded, evicts oldest entries)
_USED_NONCES: OrderedDict[str, float] = OrderedDict()
_MAX_NONCES = 10000


def _get_process_start_date() -> _date:
    """Return the process start date, initializing lazily on first call."""
    global _PROCESS_START_DATE
    if _PROCESS_START_DATE is None:
        _PROCESS_START_DATE = _date.today()
        logger.info("Signing compat window: _PROCESS_START_DATE initialized to %s", _PROCESS_START_DATE)
    return _PROCESS_START_DATE


class VerifyResult(enum.Enum):
    """Outcome of :func:`verify_command_sig`.

    * ``OK`` — signature valid (truthy).
    * ``MISMATCH`` — signature invalid, no special context (falsy).
    * ``COMPAT_EXPIRED`` — the signature *would* have matched the legacy
      SHA-256 scheme, but the compatibility window has closed (falsy).
    * ``EXPIRED`` — new-format signature has expired (falsy).
    * ``NONCE_REUSED`` — nonce was already consumed (replay attempt, falsy).
    * ``CHAT_MISMATCH`` — chat_id does not match the signed chat (falsy).
    """

    OK = "ok"
    MISMATCH = "mismatch"
    COMPAT_EXPIRED = "compat_expired"
    EXPIRED = "expired"
    NONCE_REUSED = "nonce_reused"
    CHAT_MISMATCH = "chat_mismatch"

    def __bool__(self) -> bool:  # noqa: D105
        return self is VerifyResult.OK


def _get_signing_key() -> str:
    """Return the HMAC signing key from settings.

    Falls back to an empty string only when settings are unavailable
    (e.g. during import-time or early bootstrap).  At runtime the key
    is ``settings.app_secret`` which is rejected by
    ``validate_feishu_config()`` at startup — process exits if empty.
    """
    try:
        from src.config import get_settings
        return get_settings().app_secret
    except Exception:
        logger.warning("Failed to retrieve signing key, falling back to empty string", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Legacy (v1) signing — retained for backward compatibility
# ---------------------------------------------------------------------------


def _compute_command_sig(command_text: str) -> str:
    """Return an HMAC-SHA256 hex digest of *command_text* for tamper detection.

    Uses ``settings.app_secret`` as the HMAC key so that external
    parties cannot forge a valid signature even though *command_text*
    is visible in the card payload.

    .. deprecated::
        Use :func:`sign_command` for new code. This function is retained
        for backward compatibility during the transition window.
    """
    key = _get_signing_key()
    if not key:
        raise ValueError("signing key is empty")
    return hmac.new(key.encode("utf-8"), command_text.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# New (v2) signing with nonce + expiry + chat_id binding
# ---------------------------------------------------------------------------


def sign_command(command_text: str, chat_id: str, ttl_seconds: int = 3600) -> str:
    """Sign command with nonce, expiry, and chat_id binding.

    Returns a dot-separated payload: ``sig.exp.nonce.chat_hash``

    Parameters
    ----------
    command_text : str
        The command text to sign.
    chat_id : str
        The chat_id to bind the signature to.
    ttl_seconds : int
        Time-to-live in seconds (default 1 hour).

    Returns
    -------
    str
        Dot-separated payload containing sig, expiry, nonce, and chat_id hash.
    """
    key = _get_signing_key()
    if not key:
        raise ValueError("signing key is empty")

    nonce = secrets.token_urlsafe(16)
    exp = int(time.time()) + ttl_seconds
    chat_hash = hashlib.sha256(chat_id.encode()).hexdigest()[:8]
    message = f"{command_text}|{chat_hash}|{exp}|{nonce}"
    sig = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    # Pack as: sig.exp.nonce.chat_hash
    return f"{sig}.{exp}.{nonce}.{chat_hash}"


def _is_v2_sig(sig_payload: str) -> bool:
    """Return True if the signature payload is in v2 dot-separated format."""
    parts = sig_payload.split(".")
    # v2 format: sig(64 hex).exp(digits).nonce(url-safe base64).chat_hash(8 hex)
    return len(parts) == 4 and len(parts[0]) == 64 and parts[1].isdigit()


def _record_nonce(nonce: str) -> bool:
    """Record a nonce and return True if it was already seen (replay).

    Maintains a bounded OrderedDict of recently seen nonces.
    """
    if nonce in _USED_NONCES:
        return True  # replay detected
    _USED_NONCES[nonce] = time.time()
    # Evict oldest entries if over capacity
    while len(_USED_NONCES) > _MAX_NONCES:
        _USED_NONCES.popitem(last=False)
    return False


def _verify_v2_sig(command_text: str, chat_id: str, sig_payload: str) -> VerifyResult:
    """Verify a v2 signature payload.

    Checks:
    1. Structural validity (4 dot-separated parts)
    2. Expiry (exp >= now)
    3. Nonce not reused (anti-replay)
    4. HMAC recomputation
    5. chat_id hash match
    """
    parts = sig_payload.split(".")
    if len(parts) != 4:
        return VerifyResult.MISMATCH

    sig_hex, exp_str, nonce, payload_chat_hash = parts

    # Validate expiry
    try:
        exp = int(exp_str)
    except ValueError:
        return VerifyResult.MISMATCH

    if time.time() > exp:
        return VerifyResult.EXPIRED

    # Check chat_id hash matches
    chat_hash = hashlib.sha256(chat_id.encode()).hexdigest()[:8]
    if not hmac.compare_digest(payload_chat_hash, chat_hash):
        return VerifyResult.CHAT_MISMATCH

    # Recompute HMAC
    key = _get_signing_key()
    if not key:
        return VerifyResult.MISMATCH

    message = f"{command_text}|{payload_chat_hash}|{exp_str}|{nonce}"
    expected_sig = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(sig_hex, expected_sig):
        return VerifyResult.MISMATCH

    # Check nonce not reused (do this AFTER sig verification to avoid
    # poisoning the nonce store with forged payloads)
    if _record_nonce(nonce):
        return VerifyResult.NONCE_REUSED

    return VerifyResult.OK


# ---------------------------------------------------------------------------
# Unified verification interface
# ---------------------------------------------------------------------------


def verify_command_sig(command_text: str, sig: str, *, chat_id: str = "") -> VerifyResult:
    """Verify *sig* against the command text, with optional chat_id binding.

    Supports both v2 (nonce+exp+chat_id) and v1 (HMAC-only) formats:

    1. If *sig* is in v2 format and *chat_id* is provided, verify as v2.
    2. If *sig* matches v1 HMAC-SHA256 format, accept (within compat window).
    3. Fall back to legacy plain-SHA256 check (within compat window).

    Returns :attr:`VerifyResult.OK` when valid.  ``VerifyResult`` implements
    ``__bool__`` so that ``if verify_command_sig(...)`` continues to work
    as a truthy/falsy check for backward compatibility.
    """
    if not sig:
        return VerifyResult.MISMATCH

    # Try v2 format first
    if _is_v2_sig(sig):
        if chat_id:
            return _verify_v2_sig(command_text, chat_id, sig)
        # v2 sig but no chat_id provided — extract chat_hash from payload
        # and skip chat_id check (caller doesn't have it)
        # This shouldn't normally happen but handles edge cases gracefully
        parts = sig.split(".")
        if len(parts) == 4:
            # Verify without chat_id constraint (still checks nonce+exp+hmac)
            return _verify_v2_sig_without_chat_check(command_text, sig)
        return VerifyResult.MISMATCH

    # Try v1 HMAC-SHA256 format
    try:
        expected_hmac = _compute_command_sig(command_text)
        if hmac.compare_digest(sig, expected_hmac):
            return VerifyResult.OK
    except ValueError:
        pass  # empty key — skip HMAC check

    # --- Legacy plain SHA-256 fallback (time-limited) ---
    return _verify_legacy_sha256_fallback(command_text, sig)


def _verify_v2_sig_without_chat_check(command_text: str, sig_payload: str) -> VerifyResult:
    """Verify a v2 signature without enforcing chat_id match.

    Used when the verifier does not have access to chat_id (edge case).
    Still checks expiry, nonce, and HMAC.
    """
    parts = sig_payload.split(".")
    if len(parts) != 4:
        return VerifyResult.MISMATCH

    sig_hex, exp_str, nonce, payload_chat_hash = parts

    # Validate expiry
    try:
        exp = int(exp_str)
    except ValueError:
        return VerifyResult.MISMATCH

    if time.time() > exp:
        return VerifyResult.EXPIRED

    # Recompute HMAC using the chat_hash from the payload itself
    key = _get_signing_key()
    if not key:
        return VerifyResult.MISMATCH

    message = f"{command_text}|{payload_chat_hash}|{exp_str}|{nonce}"
    expected_sig = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(sig_hex, expected_sig):
        return VerifyResult.MISMATCH

    # Check nonce not reused
    if _record_nonce(nonce):
        return VerifyResult.NONCE_REUSED

    return VerifyResult.OK


def _verify_legacy_sha256_fallback(command_text: str, sig: str) -> VerifyResult:
    """Check *sig* against plain SHA-256 if within the compatibility window.

    The window is defined by ``sig_compat_deploy_date`` (ISO date string)
    plus ``sig_compat_window_days``.  If the deploy date is empty, the
    process start date (``_PROCESS_START_DATE``) is used as fallback so
    that late deployers still get a valid compatibility window.

    Returns ``VerifyResult.OK`` if within the window and sig matches,
    ``VerifyResult.COMPAT_EXPIRED`` if sig matches legacy but window closed,
    ``VerifyResult.MISMATCH`` otherwise (conservative).
    """
    # First check if the sig matches legacy SHA-256 at all.
    plain_sig = hashlib.sha256(command_text.encode("utf-8")).hexdigest()
    legacy_matches = hmac.compare_digest(sig, plain_sig)

    # Then check the compat window.
    try:
        from datetime import date, timedelta

        from src.config import get_settings
        settings = get_settings()
        deploy_str = settings.sig_compat_deploy_date.strip()
        deploy = date.fromisoformat(deploy_str) if deploy_str else _get_process_start_date()
        window = timedelta(days=settings.sig_compat_window_days)
        window_open = date.today() <= deploy + window
    except Exception:
        # Unparseable date or unavailable settings → window closed (safe default)
        window_open = False

    if legacy_matches and window_open:
        return VerifyResult.OK
    if legacy_matches and not window_open:
        return VerifyResult.COMPAT_EXPIRED
    return VerifyResult.MISMATCH
