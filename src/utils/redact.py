"""Sensitive data redaction utility for sandbox output.

Applies regex-based redaction patterns to strip tokens, keys, and secrets
from command stdout/stderr before surfacing to users.
"""

from __future__ import annotations

import re
from typing import List

# Default patterns matching common sensitive values in CLI output.
_DEFAULT_PATTERNS: List[str] = [
    r"(?i)authorization\s*:\s*[^\s]+",
    r"(?i)bearer\s+[^\s]+",
    r"sk-[A-Za-z0-9]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"(?i)api[_-]?key\s*[:=]\s*[^\s]+",
    r"(?i)secret\s*[:=]\s*[^\s]+",
    r"(?i)token\s*[:=]\s*[^\s]+",
    r"(?i)password\s*[:=]\s*[^\s]+",
]

_REPLACEMENT = "***REDACTED***"

_compiled_patterns = [re.compile(p) for p in _DEFAULT_PATTERNS]


def redact_sensitive(text: str) -> str:
    """Replace sensitive patterns in *text* with a redaction placeholder.

    Returns the original string unchanged if it is empty or None-ish.
    """
    if not text:
        return text
    result = text
    for pattern in _compiled_patterns:
        result = pattern.sub(_REPLACEMENT, result)
    return result
