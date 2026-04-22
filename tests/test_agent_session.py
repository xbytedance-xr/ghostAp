"""Tests for src/agent_session.py.

Covers:
- SyncClaudeCLISession: lifecycle, send_prompt, cancel, snapshot
- SyncTTADKCLISession: lifecycle, send_prompt, cancel, snapshot
- Helper functions: _is_ttadk_preamble_line, _build_ttadk_passthrough_prompt
- _JSONTextExtractor: incremental JSON parsing
- classify_model_failure: compaction/loop/failover detection
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

# Import acp.models first to break circular import chain
from src.acp.models import ACPEvent, ACPEventType

from src.agent_session import (
    SyncClaudeCLISession,
    SyncTTADKCLISession,
    _build_ttadk_passthrough_prompt,
    _is_ttadk_preamble_line,
    _JSONTextExtractor,
    classify_model_failure,
)


# ── SyncClaudeCLISession ─────────────────────────────────────────────


class TestSyncClaudeCLISession:
    def test_init_defaults(self):
        sess = SyncClaudeCLISession(cwd="/tmp")
        assert sess.session_id == ""
        assert sess.message_count == 0
        assert sess.is_resumed is False
        assert sess._cwd == "/tmp"

    def test_start_assigns_session_id(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            sess = SyncClaudeCLISession(cwd="/tmp")
            sid = sess.start()
            assert sid == sess.session_id
            assert len(sid) > 0
            uuid.UUID(sid)  # validates UUID format

    def test_start_raises_when_no_executable(self):
        with patch("shutil.which", return_value=None):
            sess = SyncClaudeCLISession(cwd="/tmp")
            with pytest.raises(RuntimeError, match="未找到 Claude CLI"):
                sess.start()

    def test_load_session_sets_resumed(self):
        sess = SyncClaudeCLISession(cwd="/tmp")
        sess.load_session("test-session-123")
        assert sess.session_id == "test-session-123"
        assert sess.is_resumed is True

    def test_load_local_history_returns_empty(self):
        sess = SyncClaudeCLISession(cwd="/tmp")
        assert sess.load_local_history() == []

    def test_is_server_running_always_true(self):
        sess = SyncClaudeCLISession(cwd="/tmp")
        assert sess.is_server_running() is True
        assert sess.is_server_healthy() is True

    def test_describe_agent(self):
        sess = SyncClaudeCLISession(cwd="/workspace")
        desc = sess.describe_agent()
        assert "claude" in desc
        assert "cli" in desc
        assert "/workspace" in desc

    def test_to_snapshot(self):
        sess = SyncClaudeCLISession(cwd="/workspace")
        sess.session_id = "snap-id"
        sess.message_count = 5
        snap = sess.to_snapshot()
        assert snap["session_id"] == "snap-id"
        assert snap["agent_type"] == "claude"
        assert snap["backend"] == "cli"
        assert snap["message_count"] == 5

    def test_cancel_sets_event(self):
        sess = SyncClaudeCLISession(cwd="/tmp")
        assert not sess._cancel_event.is_set()
        sess.cancel()
        assert sess._cancel_event.is_set()

    def test_close_is_noop(self):
        sess = SyncClaudeCLISession(cwd="/tmp")
        assert sess.close() is None

    def test_send_prompt_auto_starts(self):
        """send_prompt auto-calls start() when session_id is empty."""
        sess = SyncClaudeCLISession(cwd="/tmp")
        assert sess.session_id == ""

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["hello world\n"])
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("src.utils.env.build_clean_env", return_value={}),
        ):
            result = sess.send_prompt("test")
            assert sess.session_id != ""
            assert result.stop_reason == "end_turn"
            assert "hello world" in result.text

    def test_send_prompt_collects_events(self):
        """on_event callback receives TEXT_CHUNK events."""
        sess = SyncClaudeCLISession(cwd="/tmp")
        sess.session_id = "test-id"

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["line1\n", "line2\n"])
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()

        events: list[ACPEvent] = []

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("src.utils.env.build_clean_env", return_value={}),
        ):
            sess.send_prompt("test", on_event=events.append)

        assert len(events) == 2
        assert all(e.event_type == ACPEventType.TEXT_CHUNK for e in events)
        assert sess.message_count == 1


# ── SyncTTADKCLISession ──────────────────────────────────────────────


class TestSyncTTADKCLISession:
    def test_init_extracts_tool_name(self):
        sess = SyncTTADKCLISession(agent_type="ttadk_claude", cwd="/tmp")
        assert sess._tool_name == "claude"

    def test_init_non_ttadk_prefix(self):
        sess = SyncTTADKCLISession(agent_type="custom", cwd="/tmp")
        assert sess._tool_name == "unknown"

    def test_start_raises_when_no_executable(self):
        with patch("shutil.which", return_value=None):
            sess = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
            with pytest.raises(RuntimeError, match="未找到 ttadk"):
                sess.start()

    def test_to_snapshot_includes_model(self):
        sess = SyncTTADKCLISession(agent_type="ttadk_gemini", cwd="/w", model_name="gemini-2.5-pro")
        sess.session_id = "s1"
        snap = sess.to_snapshot()
        assert snap["model_name"] == "gemini-2.5-pro"
        assert snap["agent_type"] == "ttadk_gemini"

    def test_describe_agent(self):
        sess = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/ws", model_name="gpt-5.2")
        desc = sess.describe_agent()
        assert "coco" in desc
        assert "gpt-5.2" in desc
        assert "/ws" in desc

    def test_cancel_terminates_proc(self):
        sess = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
        mock_proc = MagicMock()
        sess._proc = mock_proc
        sess.cancel()
        assert sess._cancel_event.is_set()
        mock_proc.terminate.assert_called_once()


# ── Helper functions ─────────────────────────────────────────────────


class TestIsTTADKPreambleLine:
    def test_empty_is_preamble(self):
        assert _is_ttadk_preamble_line("") is True
        assert _is_ttadk_preamble_line("   ") is True
        assert _is_ttadk_preamble_line(None) is True

    def test_version_line(self):
        assert _is_ttadk_preamble_line("Version 1.2.3") is True

    def test_emoji_prefix(self):
        assert _is_ttadk_preamble_line("🚀 Starting...") is True
        assert _is_ttadk_preamble_line("👋 Welcome") is True

    def test_normal_text_not_preamble(self):
        assert _is_ttadk_preamble_line("Here is your code output") is False

    def test_login_successful(self):
        assert _is_ttadk_preamble_line("Login successful!") is True

    def test_launching_line(self):
        assert _is_ttadk_preamble_line("Launching coco agent...") is True


class TestBuildTTADKPassthroughPrompt:
    def test_print_mode_tools(self):
        for tool in ("coco", "claude", "gemini"):
            result = _build_ttadk_passthrough_prompt(tool, "hello world")
            assert "-p" in result
            assert "hello world" in result

    def test_non_print_mode_tool(self):
        result = _build_ttadk_passthrough_prompt("codex", "fix the bug")
        assert "-p" not in result
        assert "fix the bug" in result

    def test_empty_tool_name(self):
        result = _build_ttadk_passthrough_prompt("", "test")
        assert "test" in result


# ── _JSONTextExtractor ───────────────────────────────────────────────


class TestJSONTextExtractor:
    def test_simple_json_object(self):
        ext = _JSONTextExtractor()
        result = ext.feed('{"key": "value"}')
        assert len(result) == 1
        assert '"key"' in result[0]

    def test_simple_json_array(self):
        ext = _JSONTextExtractor()
        result = ext.feed("[1, 2, 3]")
        assert len(result) == 1

    def test_incremental_feed(self):
        ext = _JSONTextExtractor()
        assert ext.feed('{"k') == []
        assert ext.has_json_candidate() is True
        result = ext.feed('ey": 1}')
        assert len(result) == 1

    def test_mixed_text_and_json(self):
        ext = _JSONTextExtractor()
        result = ext.feed('some text {"a":1} more text')
        assert len(result) == 1
        assert '"a"' in result[0]

    def test_no_json(self):
        ext = _JSONTextExtractor()
        assert ext.feed("plain text") == []
        assert ext.has_json_candidate() is False


# ── classify_model_failure ───────────────────────────────────────────


class TestClassifyModelFailure:
    def test_need_compaction(self):
        err = RuntimeError("Model failed: model 'gpt-5.2': receive message: need compaction")
        result = classify_model_failure(error=err)
        assert result["fail_phase"] == "model_compaction"
        assert result["reason"] == "need_compaction"
        assert result["failed_model"] == "gpt-5.2"

    def test_loop_detected(self):
        err = RuntimeError("loop detected in conversation")
        result = classify_model_failure(error=err)
        assert result["fail_phase"] == "model_loop"
        assert result["reason"] == "loop_detected"

    def test_failover(self):
        err = RuntimeError("Model failed: model 'gpt-5.2'. Failing over to: gpt-5.1")
        result = classify_model_failure(error=err)
        assert result["failed_model"] == "gpt-5.2"
        assert result["failover_to"] == "gpt-5.1"

    def test_unknown_error(self):
        err = RuntimeError("something else entirely")
        result = classify_model_failure(error=err)
        assert result["fail_phase"] == "unknown"
        assert result["reason"] == "unknown"
