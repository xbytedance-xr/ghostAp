"""HMAC command-signature utilities.

Provides tamper-detection signing and verification for Feishu card action
payloads.  Extracted from ``card.builders.lock`` to keep cryptographic
logic separate from UI card construction (Single Responsibility Principle).
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import logging
from datetime import date as _date
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-initialized on first use — avoids capturing a stale date at import time
# when the module is loaded during early bootstrap (e.g. pre-fork workers).
_PROCESS_START_DATE: Optional[_date] = None


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
    """

    OK = "ok"
    MISMATCH = "mismatch"
    COMPAT_EXPIRED = "compat_expired"

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


def _compute_command_sig(command_text: str) -> str:
    """Return an HMAC-SHA256 hex digest of *command_text* for tamper detection.

    Uses ``settings.app_secret`` as the HMAC key so that external
    parties cannot forge a valid signature even though *command_text*
    is visible in the card payload.
    """
    key = _get_signing_key()
    if not key:
        raise ValueError("signing key is empty")
    return hmac.new(key.encode("utf-8"), command_text.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_command_sig(command_text: str, sig: str) -> VerifyResult:
    """Verify *sig* against the HMAC-SHA256 of *command_text*.

    Returns :attr:`VerifyResult.OK` when valid.  ``VerifyResult`` implements
    ``__bool__`` so that ``if verify_command_sig(...)`` continues to work
    as a truthy/falsy check for backward compatibility.

    Returns :attr:`VerifyResult.MISMATCH` when the signature is simply wrong.

    Returns :attr:`VerifyResult.COMPAT_EXPIRED` when the signature matches
    the legacy plain-SHA256 scheme but the compatibility window has closed,
    allowing callers to show a specialised "upgrade expired" message.
    """
    if not sig:
        return VerifyResult.MISMATCH
    if hmac.compare_digest(sig, _compute_command_sig(command_text)):
        return VerifyResult.OK
    # --- Legacy plain SHA-256 fallback (time-limited) ---
    return _verify_legacy_sha256_fallback(command_text, sig)


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
