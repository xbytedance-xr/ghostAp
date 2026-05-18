"""Unit tests for slock_engine/engine.py and slock_engine/slash_commands.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ------------------------------------------------------------------
# Slash commands have zero heavy dependencies — safe to import directly.
# ------------------------------------------------------------------
from src.slock_engine.slash_commands import (
    SlockCommand,
    SlockCommandAction,
    is_slock_command,
    parse_slock_command,
)


# ============================================================
# Slash Commands
# ============================================================


class TestIsSLockCommand:
    """Test is_slock_command with context-aware scoping."""

    @pytest.mark.parametrize("text", [
        "/slock",
        "/slock status",
        "/new-team Alpha",
    ])
    def test_globally_recognized(self, text):
        """These commands are always captured regardless of chat context."""
        assert is_slock_command(text) is True

    @pytest.mark.parametrize("text", [
        "/new-role Coder",
        "/role list",
        "/task assign something coder",
        "/team dissolve Alpha",
    ])
    def test_recognized_in_managed_chat(self, text):
        """These commands are only captured in managed slock chats."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = True
        assert is_slock_command(text, chat_id="chat_123", manager=manager) is True

    @pytest.mark.parametrize("text", [
        "/new-role Coder",
        "/role list",
        "/task assign something coder",
        "/team dissolve Alpha",
    ])
    def test_not_captured_in_unmanaged_chat(self, text):
        """Team commands passthrough in unmanaged chats."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = False
        assert is_slock_command(text, chat_id="chat_456", manager=manager) is False

    @pytest.mark.parametrize("text", [
        "",
        "/deep",
        "/spec",
        "hello",
        "/exit",
    ])
    def test_not_recognized(self, text):
        assert is_slock_command(text) is False


class TestParseSlockCommand:
    def test_slock_bare(self):
        cmd = parse_slock_command("/slock")
        assert cmd.action == SlockCommandAction.ACTIVATE

    def test_slock_status(self):
        cmd = parse_slock_command("/slock status")
        assert cmd.action == SlockCommandAction.STATUS

    def test_slock_help(self):
        cmd = parse_slock_command("/slock help")
        assert cmd.action == SlockCommandAction.HELP

    def test_slock_with_args(self):
        cmd = parse_slock_command("/slock build a web app")
        assert cmd.action == SlockCommandAction.ACTIVATE
        assert cmd.args == "build a web app"

    def test_new_team(self):
        cmd = parse_slock_command("/new-team Alpha")
        assert cmd.action == SlockCommandAction.NEW_TEAM
        assert cmd.args == "Alpha"

    def test_new_role(self):
        cmd = parse_slock_command("/new-role Frontend-Dev")
        assert cmd.action == SlockCommandAction.NEW_ROLE
        assert cmd.args == "Frontend-Dev"

    def test_role_list(self):
        cmd = parse_slock_command("/role list")
        assert cmd.action == SlockCommandAction.ROLE_LIST

    def test_role_bare(self):
        cmd = parse_slock_command("/role")
        assert cmd.action == SlockCommandAction.ROLE_LIST

    def test_role_remove(self):
        cmd = parse_slock_command("/role remove Coder")
        assert cmd.action == SlockCommandAction.ROLE_REMOVE
        assert cmd.target == "Coder"

    def test_role_info(self):
        cmd = parse_slock_command("/role info Alice")
        assert cmd.action == SlockCommandAction.ROLE_INFO
        assert cmd.target == "Alice"

    def test_role_shorthand(self):
        cmd = parse_slock_command("/role Bob")
        assert cmd.action == SlockCommandAction.ROLE_INFO
        assert cmd.target == "bob"

    def test_task_list(self):
        cmd = parse_slock_command("/task list")
        assert cmd.action == SlockCommandAction.TASK_LIST

    def test_task_bare(self):
        cmd = parse_slock_command("/task")
        assert cmd.action == SlockCommandAction.TASK_LIST

    def test_task_status(self):
        cmd = parse_slock_command("/task status")
        assert cmd.action == SlockCommandAction.TASK_STATUS

    def test_task_assign_with_role(self):
        cmd = parse_slock_command("/task assign implement login coder")
        assert cmd.action == SlockCommandAction.TASK_ASSIGN
        assert cmd.args == "implement login"
        assert cmd.target == "coder"

    def test_task_assign_without_role(self):
        cmd = parse_slock_command("/task assign something")
        assert cmd.action == SlockCommandAction.TASK_ASSIGN
        assert cmd.args == "something"

    def test_team_list(self):
        cmd = parse_slock_command("/team list")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_team_bare(self):
        cmd = parse_slock_command("/team")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_team_status(self):
        cmd = parse_slock_command("/team status Alpha")
        assert cmd.action == SlockCommandAction.TEAM_STATUS
        assert cmd.target == "Alpha"

    def test_team_dissolve(self):
        cmd = parse_slock_command("/team dissolve Beta")
        assert cmd.action == SlockCommandAction.TEAM_DISSOLVE
        assert cmd.target == "Beta"

    def test_team_shorthand(self):
        cmd = parse_slock_command("/team Gamma")
        assert cmd.action == SlockCommandAction.TEAM_STATUS
        assert cmd.target == "gamma"

    def test_empty_input(self):
        cmd = parse_slock_command("")
        assert cmd.action == SlockCommandAction.UNKNOWN

    def test_unknown_command(self):
        cmd = parse_slock_command("/other stuff")
        assert cmd.action == SlockCommandAction.UNKNOWN


# ============================================================
# SlockEngine — requires `acp` package (skip if unavailable)
# ============================================================

_acp_available = pytest.importorskip("acp", reason="acp package not installed")

from src.slock_engine.engine import SlockEngine, SlockEngineCallbacks  # noqa: E402
from src.slock_engine.models import (  # noqa: E402
    AgentIdentity,
    AgentStatus,
    SlockChannel,
    SlockTask,
    TaskStatus,
)


class TestSlockEngine:
    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None, **kwargs):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock"
        return SlockEngine(
            chat_id="chat1",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
            **kwargs,
        )

    def test_construction(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.chat_id == "chat1"
        assert engine.channel is None
        assert engine.tasks == []

    def test_activate_channel(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch1", team_name="Alpha")
        engine.activate_channel(ch)
        assert engine.channel is not None
        assert engine.channel.channel_id == "ch1"

    def test_activate_channel_creates_workspace(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        # Override root_path to use tmp_path for assertions
        engine.root_path = str(tmp_path / "project_root")
        ch = SlockChannel(channel_id="ch_ws", name="ws-group", team_name="WS Team")
        engine.activate_channel(ch)

        import json, os
        workspace_dir = os.path.join(str(tmp_path / "project_root"), "slock", "ch_ws")
        assert os.path.isdir(workspace_dir), "workspace directory should exist"

        marker_path = os.path.join(workspace_dir, ".slock_channel.json")
        assert os.path.isfile(marker_path), "marker file should exist"

        with open(marker_path, "r", encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["channel_id"] == "ch_ws"
        assert marker["team_name"] == "WS Team"
        assert marker["name"] == "ws-group"
        assert "activated_at" in marker

    def test_add_task(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Implement feature X")
        assert task.content == "Implement feature X"
        assert task.status == TaskStatus.TODO
        assert len(engine.tasks) == 1

    def test_claim_task(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Build API")
        result = engine.claim_task(task.task_id, "agent-1")
        assert result is True
        claimed = engine.tasks[0]
        assert claimed.status == TaskStatus.IN_PROGRESS
        assert claimed.claimed_by == "agent-1"

    def test_claim_task_double_claim_fails(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Build API")
        engine.claim_task(task.task_id, "agent-1")
        result = engine.claim_task(task.task_id, "agent-2")
        assert result is False

    def test_complete_task(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Write tests")
        engine.claim_task(task.task_id, "agent-1")
        result = engine.complete_task(task.task_id, "agent-1")
        assert result is True
        assert engine.tasks[0].status == TaskStatus.DONE

    def test_complete_task_wrong_agent(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Task A")
        engine.claim_task(task.task_id, "agent-1")
        result = engine.complete_task(task.task_id, "agent-2")
        assert result is False


class TestSlockEngineStateMachine:
    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock"
        return SlockEngine(
            chat_id="chat1",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_initial_status_is_idle(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.get_agent_status("a1") == AgentStatus.IDLE

    def test_valid_transition_idle_to_waking(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.transition_agent("a1", AgentStatus.WAKING) is True
        assert engine.get_agent_status("a1") == AgentStatus.WAKING

    def test_full_lifecycle_transition(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        agent_id = "a1"
        assert engine.transition_agent(agent_id, AgentStatus.WAKING) is True
        assert engine.transition_agent(agent_id, AgentStatus.THINKING) is True
        assert engine.transition_agent(agent_id, AgentStatus.RUNNING) is True
        assert engine.transition_agent(agent_id, AgentStatus.CHECKING) is True
        assert engine.transition_agent(agent_id, AgentStatus.SENDING) is True
        assert engine.transition_agent(agent_id, AgentStatus.IDLE) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.IDLE

    def test_invalid_transition_rejected(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        # IDLE → RUNNING (skipping WAKING) should fail
        assert engine.transition_agent("a1", AgentStatus.RUNNING) is False
        assert engine.get_agent_status("a1") == AgentStatus.IDLE

    def test_any_state_can_return_to_idle(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        engine.transition_agent("a1", AgentStatus.WAKING)
        engine.transition_agent("a1", AgentStatus.THINKING)
        # THINKING → IDLE (abort)
        assert engine.transition_agent("a1", AgentStatus.IDLE) is True

    def test_get_status_card(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch1", team_name="Test")
        engine.activate_channel(ch)
        card = engine.get_status_card(team_name="Test")
        assert card["schema"] == "2.0"
        assert "Test" in card["header"]["title"]["content"]


class TestSlockEngineCallbacks:
    def test_callbacks_dataclass(self):
        cb = SlockEngineCallbacks()
        assert cb.on_agent_wake is None
        assert cb.on_error is None

    def test_callbacks_with_values(self):
        fn = MagicMock()
        cb = SlockEngineCallbacks(on_error=fn)
        cb.on_error("test error")
        fn.assert_called_once_with("test error")
