"""Tests for ACP sync_adapter auto-update logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.acp import sync_adapter as sa


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset module-level state between tests."""
    sa._update_attempted.clear()
    try:
        sa._supports_acp_serve.cache_clear()
    except Exception:
        pass
    yield
    sa._update_attempted.clear()
    try:
        sa._supports_acp_serve.cache_clear()
    except Exception:
        pass


def _fake_settings(**overrides):
    defaults = {
        "acp_auto_update": True,
        "acp_startup_retries": 2,
    }
    defaults.update(overrides)

    class FakeSettings:
        def __getattr__(self, name):
            if name in defaults:
                return defaults[name]
            if name == "get_acp_command":
                return lambda agent_type: ("", [])
            raise AttributeError(name)

    return FakeSettings()


# ── _auto_update_agent ──────────────────────────────────────────────


class TestAutoUpdateAgent:
    def test_successful_update(self, monkeypatch):
        """Auto-update returns True when subprocess exits 0."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(
            sa.subprocess, "run",
            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="Updated to v1.2.3", stderr=""),
        )
        assert sa._auto_update_agent("coco") is True

    def test_failed_update(self, monkeypatch):
        """Auto-update returns False when subprocess exits non-zero."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(
            sa.subprocess, "run",
            lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="network error"),
        )
        assert sa._auto_update_agent("coco") is False

    def test_update_exception(self, monkeypatch):
        """Auto-update returns False when subprocess raises."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(
            sa.subprocess, "run",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("coco not found")),
        )
        assert sa._auto_update_agent("coco") is False

    def test_dedup_skips_second_attempt(self, monkeypatch):
        """Same command is only updated once per process lifecycle."""
        call_count = {"n": 0}

        def fake_run(*a, **kw):
            call_count["n"] += 1
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(sa.subprocess, "run", fake_run)

        assert sa._auto_update_agent("coco") is True
        assert sa._auto_update_agent("coco") is False  # deduped
        assert call_count["n"] == 1

    def test_config_disabled(self, monkeypatch):
        """Auto-update respects acp_auto_update=False."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings(acp_auto_update=False))
        call_count = {"n": 0}

        def fake_run(*a, **kw):
            call_count["n"] += 1
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(sa.subprocess, "run", fake_run)
        assert sa._auto_update_agent("coco") is False
        assert call_count["n"] == 0  # subprocess never called


# ── _resolve_with_auto_update ────────────────────────────────────────


class TestResolveWithAutoUpdate:
    def test_already_supported(self, monkeypatch):
        """No update attempted when ACP is already supported."""
        monkeypatch.setattr(sa, "_supports_acp_serve", lambda cmd: True)
        # Patch _auto_update_agent to track if it was called
        called = {"n": 0}
        orig = sa._auto_update_agent

        def tracking(*a, **kw):
            called["n"] += 1
            return orig(*a, **kw)

        monkeypatch.setattr(sa, "_auto_update_agent", tracking)
        assert sa._resolve_with_auto_update("coco") is True
        assert called["n"] == 0

    def test_update_fixes_support(self, monkeypatch):
        """After auto-update, re-probe succeeds."""
        probe_results = iter([False, True])  # first call False, second True

        # Need to work with the lru_cache-decorated function
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())

        call_idx = {"n": 0}

        def fake_run(cmd, **kw):
            call_idx["n"] += 1
            if cmd[1:] == ["update"]:
                return SimpleNamespace(returncode=0, stdout="updated", stderr="")
            # acp serve -h probe
            if call_idx["n"] <= 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="unknown command")
            return SimpleNamespace(returncode=0, stdout="Start the ACP server", stderr="")

        monkeypatch.setattr(sa.subprocess, "run", fake_run)
        assert sa._resolve_with_auto_update("coco") is True

    def test_update_fails_still_unsupported(self, monkeypatch):
        """Auto-update fails → returns False."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(
            sa.subprocess, "run",
            lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="fail"),
        )
        assert sa._resolve_with_auto_update("coco") is False


# ── resolve_agent_spec ───────────────────────────────────────────────


class TestResolveAgentSpec:
    def test_coco_with_auto_update(self, monkeypatch):
        """resolve_agent_spec returns coco spec after successful auto-update."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(sa, "_resolve_with_auto_update", lambda cmd: cmd == "coco")
        assert sa.resolve_agent_spec("coco") == ("coco", ["acp", "serve"])

    def test_coco_fails_after_update(self, monkeypatch):
        """resolve_agent_spec raises after auto-update fails."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(sa, "_resolve_with_auto_update", lambda cmd: False)
        with pytest.raises(RuntimeError, match="does not appear to support ACP server mode"):
            sa.resolve_agent_spec("coco")

    def test_claude_with_auto_update(self, monkeypatch):
        """resolve_agent_spec returns claude spec after successful auto-update."""
        monkeypatch.setattr(sa, "get_settings", lambda: _fake_settings())
        monkeypatch.setattr(sa, "_resolve_with_auto_update", lambda cmd: cmd == "claude")
        assert sa.resolve_agent_spec("claude") == ("claude", ["acp", "serve"])

    def test_config_override_bypasses_detection(self, monkeypatch):
        """Config overrides skip detection and auto-update entirely."""
        settings = _fake_settings()
        settings.get_acp_command = lambda agent_type: ("/custom/coco", ["serve"])
        monkeypatch.setattr(sa, "get_settings", lambda: settings)
        assert sa.resolve_agent_spec("coco") == ("/custom/coco", ["serve"])
