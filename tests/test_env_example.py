"""AC-17: Verify .env.example only contains user-facing variables.

Asserts that the 6 removed internal variables are not present in .env.example,
and that the 6 required user-facing variables are present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Internal variables that should have been removed (Task 12 / FS-08).
REMOVED_VARS = frozenset({
    "SIG_COMPAT_DEPLOY_DATE",
    "SIG_COMPAT_WINDOW_DAYS",
    "LOCK_BACKEND",
})

# User-facing variables that must be retained.
REQUIRED_VARS = frozenset({
    "ADMIN_USER_IDS",
    "REPO_LOCK_IDLE_TIMEOUT",
    "REPO_LOCK_HARD_TIMEOUT",
    "REPO_LOCK_CLEANUP_INTERVAL",
    "CHAT_LOCK_CLEANUP_INTERVAL",
    "CHAT_LOCK_MAX_DURATION",
    "SANDBOX_STRICT_LOCK_MODE",
    "MAX_ALLOWED_CHAT_IDS",
    "MAX_EVICTED_CACHE",
    "LOCK_CONFIRM_TIMEOUT",
    "SPEC_MAX_CYCLES",
    "SPEC_REVIEW_PARSE_FAILURE_DEFAULT",
})


def _parse_env_keys(text: str) -> set[str]:
    """Extract variable names (KEY=... lines, ignoring comments and blank lines)."""
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key:
                keys.add(key)
    return keys


class TestEnvExampleContents:
    """Validate .env.example file contents."""

    @pytest.fixture(autouse=True)
    def _load_env(self):
        assert _ENV_EXAMPLE.exists(), f".env.example not found at {_ENV_EXAMPLE}"
        self._text = _ENV_EXAMPLE.read_text(encoding="utf-8")
        self._keys = _parse_env_keys(self._text)

    def test_removed_vars_not_present(self):
        """6 internal variables must not appear in .env.example."""
        present = REMOVED_VARS & self._keys
        assert not present, f"Internal vars still in .env.example: {present}"

    def test_removed_vars_not_in_comments_as_active(self):
        """Removed vars should not appear as active KEY= lines even commented out."""
        for var in REMOVED_VARS:
            # Check no active (uncommented) line starts with the var name
            for line in self._text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith(f"{var}="):
                    pytest.fail(f"Removed var {var} found as active line: {stripped}")

    def test_required_vars_present(self):
        """6 user-facing variables must be present in .env.example."""
        missing = REQUIRED_VARS - self._keys
        assert not missing, f"Required vars missing from .env.example: {missing}"

    def test_sandbox_strict_lock_mode_has_description(self):
        """SANDBOX_STRICT_LOCK_MODE should have a behavior description in comment."""
        lines = self._text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("SANDBOX_STRICT_LOCK_MODE"):
                # Check preceding comment line(s) for description
                if i > 0:
                    prev = lines[i - 1].strip()
                    assert prev.startswith("#"), (
                        f"SANDBOX_STRICT_LOCK_MODE missing description comment"
                    )
                    assert "true" in prev.lower() or "拒绝" in prev or "冲突" in prev, (
                        f"SANDBOX_STRICT_LOCK_MODE comment doesn't describe behavior: {prev}"
                    )
                return
        pytest.fail("SANDBOX_STRICT_LOCK_MODE not found in .env.example")
