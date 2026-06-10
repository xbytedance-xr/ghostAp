"""Claude provider capability flags (1M context support).

Centralised so probe / env / provider modules agree on the same notion of
"this Anthropic model supports the 1 000 000-token context window".

Anthropic's Claude Code CLI accepts the ``[1m]`` suffix on ``--model`` to
opt into the 1M-context beta; the wrapper additionally honours the
``ANTHROPIC_BETAS=context-1m-2025-08-07`` env variable.  We use the suffix
as the primary path (preserves the raw model id for ``session/setModel``
hot-swap and persistence) and the env as a defensive fallback.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Model-id prefixes whose *base* model accepts the 1M context beta.  We
#: match by prefix so date-stamped releases (e.g. ``claude-opus-4-8-20260101``)
#: are covered without a code change.
CLAUDE_1M_PREFIXES: tuple[str, ...] = (
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-opus-4-8",
    "claude-opus-4-5",
)

#: Beta token expected in ``ANTHROPIC_BETAS``.
CONTEXT_1M_BETA = "context-1m-2025-08-07"

#: Suffix Claude Code CLI uses on ``--model`` to enable the 1M variant.
SUFFIX_1M = "[1m]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_1m_suffix(model_id: str) -> str:
    """Return *model_id* with the ``[1m]`` suffix stripped, if present.

    Idempotent on inputs that don't carry the suffix.  Whitespace is
    preserved as-is so callers retain control over normalisation.
    """
    s = str(model_id or "")
    if s.endswith(SUFFIX_1M):
        return s[: -len(SUFFIX_1M)]
    return s


def is_1m_variant(model_id: str) -> bool:
    """True iff *model_id* is the 1M-suffixed variant of a Claude model."""
    return str(model_id or "").endswith(SUFFIX_1M)


def with_1m_suffix(model_id: str) -> str:
    """Return *model_id* with ``[1m]`` appended (idempotent)."""
    s = str(model_id or "")
    if s.endswith(SUFFIX_1M):
        return s
    return s + SUFFIX_1M


def model_supports_1m(model_id: str) -> bool:
    """True iff the *base* of *model_id* is in :data:`CLAUDE_1M_PREFIXES`.

    The ``[1m]`` suffix is stripped before matching so callers may pass
    either form.  Matching is by prefix to cover date-stamped releases.
    """
    base = strip_1m_suffix(str(model_id or "")).strip()
    if not base:
        return False
    return any(base.startswith(p) for p in CLAUDE_1M_PREFIXES)


__all__ = [
    "CLAUDE_1M_PREFIXES",
    "CONTEXT_1M_BETA",
    "SUFFIX_1M",
    "is_1m_variant",
    "model_supports_1m",
    "strip_1m_suffix",
    "with_1m_suffix",
]
