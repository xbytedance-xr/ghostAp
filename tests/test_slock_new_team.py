"""Tests for /new-team command: create Feishu group + activate slock runtime."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

_acp_available = pytest.importorskip("acp", reason="acp package not installed")

from src.slock_engine.manager import SlockEngineManager  # noqa: E402
from src.slock_engine.models import SlockChannel  # noqa: E402

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@dataclass
class _FakeCreateChatResult:
    chat_id: str
    name: str


def _make_handler_ctx(tmp_path, *, api_client_factory=None, settings=None):
    """Build a minimal HandlerContext-like object for SlockHandler."""
    ctx = MagicMock()
    ctx.api_client_factory = api_client_factory or MagicMock()
    ctx.project_manager = MagicMock()

    # Settings
    s = MagicMock()
    s.slock_team_name_prefix = ""
    s.slock_workspace_base = ""
    s.slock_memory_base_path = str(tmp_path / "slock_memory")
    s.slock_execution_timeout = 3600
    s.slock_max_agents_per_team = 10
    s.slock_task_claim_ttl = 3600.0
    if settings:
        for k, v in settings.items():
            setattr(s, k, v)
    ctx.settings = s

    # Slock engine manager — use real instance
    manager = SlockEngineManager()
    ctx.slock_engine_manager = manager

    # Handler registry (minimal)
    ctx.handlers = {}
    ctx.mode_manager = MagicMock()
    ctx.context_manager = MagicMock()
    ctx.scheduler = MagicMock()
    return ctx


def _make_slock_handler(ctx):
    """Instantiate SlockHandler with mocked IM client."""
    with patch("src.feishu.handlers.base.FeishuIMClient"):
        from src.feishu.handlers.slock import SlockHandler

        handler = SlockHandler(ctx)
        # Mock messaging helpers
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_text_to_chat = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/workdir")
        handler.create_static_card_session = MagicMock()
        return handler


# ==================================================================
# Test: Happy path — group created, engine activated, messages sent
# ==================================================================


class TestCreateTeamHappyPath:
    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.thread.manager.get_current_sender_id", return_value="ou_sender123")
    @patch("src.project_chat.lark_chat_client.LarkChatClient")
    def test_create_team_full_flow(
        self, MockLarkChatClient, mock_sender, mock_session, tmp_path
    ):
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)

        # Set up root_path for workspace verification
        root_path = str(tmp_path / "project_root")
        handler.get_working_dir.return_value = root_path

        # Mock LarkChatClient
        mock_lark = MockLarkChatClient.return_value
        mock_lark.create_chat.return_value = _FakeCreateChatResult(
            chat_id="oc_new_group_id", name="TestTeam"
        )

        # Execute
        handler.create_team("msg1", "oc_origin_chat", "TestTeam", project=None)

        # 1. Group created with correct params
        mock_lark.create_chat.assert_called_once()
        call_kwargs = mock_lark.create_chat.call_args
        assert call_kwargs.kwargs["name"] == "TestTeam"
        assert "ou_sender123" in call_kwargs.kwargs["user_id_list"]

        # 2. Sender promoted to manager
        mock_lark.add_managers.assert_called_once_with("oc_new_group_id", ["ou_sender123"])

        # 3. Engine activated on new chat_id
        manager = ctx.slock_engine_manager
        engine = manager.get_activated_engine("oc_new_group_id")
        assert engine is not None
        assert engine.channel is not None
        assert engine.channel.channel_id == "oc_new_group_id"
        assert engine.channel.team_name == "TestTeam"

        # 4. Workspace directory exists
        workspace_dir = os.path.join(root_path, "slock", "oc_new_group_id")
        assert os.path.isdir(workspace_dir)
        marker_path = os.path.join(workspace_dir, ".slock_channel.json")
        assert os.path.isfile(marker_path)
        with open(marker_path, "r") as f:
            marker = json.load(f)
        assert marker["channel_id"] == "oc_new_group_id"

        # 5. Managed chat registered for event routing
        assert manager.is_managed_chat("oc_new_group_id") is True

        # 6. Welcome sent to new group
        handler.send_text_to_chat.assert_called_once()
        new_group_text = handler.send_text_to_chat.call_args[0]
        assert new_group_text[0] == "oc_new_group_id"
        assert "TestTeam" in new_group_text[1]

        # 7. Confirmation sent to original group
        handler.reply_text.assert_called_once()
        reply_args = handler.reply_text.call_args[0]
        assert reply_args[0] == "msg1"
        assert "TestTeam" in reply_args[1]
        assert "事件监听" in reply_args[1]

    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.thread.manager.get_current_sender_id", return_value="ou_sender123")
    @patch("src.project_chat.lark_chat_client.LarkChatClient")
    def test_create_team_with_prefix(
        self, MockLarkChatClient, mock_sender, mock_session, tmp_path
    ):
        ctx = _make_handler_ctx(tmp_path, settings={"slock_team_name_prefix": "[Slock] "})
        handler = _make_slock_handler(ctx)
        handler.get_working_dir.return_value = str(tmp_path / "root")

        mock_lark = MockLarkChatClient.return_value
        mock_lark.create_chat.return_value = _FakeCreateChatResult(
            chat_id="oc_prefixed", name="[Slock] Alpha"
        )

        handler.create_team("msg2", "oc_origin", "Alpha")

        call_kwargs = mock_lark.create_chat.call_args.kwargs
        assert call_kwargs["name"] == "[Slock] Alpha"


# ==================================================================
# Test: Failure & rollback
# ==================================================================


class TestCreateTeamRollback:
    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.thread.manager.get_current_sender_id", return_value="ou_sender123")
    @patch("src.project_chat.lark_chat_client.LarkChatClient")
    def test_create_chat_failure_shows_error(
        self, MockLarkChatClient, mock_sender, mock_session, tmp_path
    ):
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)

        mock_lark = MockLarkChatClient.return_value
        mock_lark.create_chat.side_effect = RuntimeError("API limit exceeded")

        handler.create_team("msg1", "oc_origin", "FailTeam")

        # Error reported to user
        handler.reply_text.assert_called_once()
        err_text = handler.reply_text.call_args[0][1]
        assert "创建团队群失败" in err_text

        # No group to delete (never created)
        mock_lark.delete_chat.assert_not_called()

    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.thread.manager.get_current_sender_id", return_value="ou_sender123")
    @patch("src.project_chat.lark_chat_client.LarkChatClient")
    def test_activation_failure_rolls_back_group(
        self, MockLarkChatClient, mock_sender, mock_session, tmp_path
    ):
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)

        mock_lark = MockLarkChatClient.return_value
        mock_lark.create_chat.return_value = _FakeCreateChatResult(
            chat_id="oc_to_delete", name="BadTeam"
        )

        # Force engine activation to fail
        ctx.slock_engine_manager.get_or_create = MagicMock(
            side_effect=RuntimeError("Engine init failed")
        )

        handler.create_team("msg1", "oc_origin", "BadTeam")

        # Group should be rolled back
        mock_lark.delete_chat.assert_called_once_with("oc_to_delete")

        # Error reported to user
        handler.reply_text.assert_called_once()
        err_text = handler.reply_text.call_args[0][1]
        assert "激活失败已回滚" in err_text

    def test_empty_name_shows_usage(self, tmp_path):
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)

        handler.create_team("msg1", "oc_origin", "")

        handler.reply_text.assert_called_once()
        assert "用法" in handler.reply_text.call_args[0][1]

    @patch("src.thread.manager.get_current_sender_id", return_value="")
    def test_no_sender_shows_error(self, mock_sender, tmp_path):
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)

        handler.create_team("msg1", "oc_origin", "Team")

        handler.reply_text.assert_called_once()
        assert "无法获取发送者" in handler.reply_text.call_args[0][1]


# ==================================================================
# Test: Dispatcher routes managed chat messages to slock engine
# ==================================================================


class TestDispatcherSlockRouting:
    def test_managed_chat_detected(self):
        """SlockEngineManager.is_managed_chat returns True after registration."""
        manager = SlockEngineManager()
        assert manager.is_managed_chat("oc_test") is False
        manager.register_managed_chat("oc_test")
        assert manager.is_managed_chat("oc_test") is True

    def test_unregister_managed_chat(self):
        manager = SlockEngineManager()
        manager.register_managed_chat("oc_test")
        manager.unregister_managed_chat("oc_test")
        assert manager.is_managed_chat("oc_test") is False


class TestTeamAdminCommands:
    @patch("src.slock_engine.engine.create_engine_session")
    def test_list_teams_lists_all_activated_slock_teams_from_admin_chat(self, mock_session, tmp_path):
        """Admin `/team list` should show every active team, not only the current chat."""
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)
        manager = ctx.slock_engine_manager
        root_path = str(tmp_path / "project_root")

        for chat_id, team_name in [("oc_alpha", "Alpha"), ("oc_beta", "Beta")]:
            engine = manager.get_or_create(chat_id, root_path, engine_name="Slock")
            engine.activate_channel(SlockChannel(channel_id=chat_id, name=team_name, team_name=team_name))
            manager.register_managed_chat(chat_id)

        handler.list_teams("msg_admin", "oc_admin")

        handler.reply_text.assert_called_once()
        text = handler.reply_text.call_args[0][1]
        assert "Alpha" in text
        assert "Beta" in text
        assert "oc_alpha" in text
        assert "oc_beta" in text

    @patch("src.slock_engine.engine.create_engine_session")
    def test_team_status_finds_team_by_name(self, mock_session, tmp_path):
        """Admin `/team status <name>` resolves a team by name and renders its status card."""
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)
        root_path = str(tmp_path / "project_root")
        engine = ctx.slock_engine_manager.get_or_create("oc_alpha", root_path, engine_name="Slock")
        engine.activate_channel(SlockChannel(channel_id="oc_alpha", name="Alpha Chat", team_name="Alpha"))
        engine.get_status_card = MagicMock(return_value={"header": {"title": {"content": "Alpha"}}})

        handler.show_team_status("msg_status", "oc_admin", "Alpha")

        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        assert card["header"]["title"]["content"] == "Alpha"

    @patch("src.slock_engine.engine.create_engine_session")
    def test_team_dissolve_unregisters_and_removes_named_team(self, mock_session, tmp_path):
        """Admin `/team dissolve <name>` stops the named team runtime."""
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)
        manager = ctx.slock_engine_manager
        root_path = str(tmp_path / "project_root")
        engine = manager.get_or_create("oc_beta", root_path, engine_name="Slock")
        engine.activate_channel(SlockChannel(channel_id="oc_beta", name="Beta Chat", team_name="Beta"))
        manager.register_managed_chat("oc_beta")

        handler.dissolve_team("msg_dissolve", "oc_admin", "Beta")

        assert manager.is_managed_chat("oc_beta") is False
        assert manager.get_activated_engine("oc_beta") is None
        handler.reply_text.assert_called_once()
        assert "Beta" in handler.reply_text.call_args[0][1]


class TestActivateSlockManagedChat:
    @patch("src.slock_engine.engine.create_engine_session")
    def test_activate_slock_registers_current_chat_for_team_commands(self, mock_session, tmp_path):
        """After `/slock`, team-local commands such as `/new-role` must be captured."""
        from src.slock_engine.slash_commands import is_slock_command

        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)
        project_root = str(tmp_path / "project_root")
        handler._ensure_project = MagicMock(return_value=MagicMock(
            root_path=project_root,
            project_name="CurrentTeam",
            project_id="p-current",
        ))
        handler.get_engine_name = MagicMock(return_value="Slock-current")
        session = MagicMock()
        handler.create_static_card_session.return_value = session

        handler.activate_slock("msg_current", "oc_current", "", project=None)

        manager = ctx.slock_engine_manager
        assert manager.is_managed_chat("oc_current") is True
        assert is_slock_command("/new-role Coder", chat_id="oc_current", manager=manager) is True


# ==================================================================
# Test: Restart survival — create team, simulate restart, verify restore
# ==================================================================


class TestRestartSurvival:
    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.thread.manager.get_current_sender_id", return_value="ou_sender123")
    @patch("src.project_chat.lark_chat_client.LarkChatClient")
    def test_new_team_survives_restart(
        self, MockLarkChatClient, mock_sender, mock_session, tmp_path
    ):
        """After create_team, a fresh manager.restore_from_disk should recover state."""
        ctx = _make_handler_ctx(tmp_path)
        handler = _make_slock_handler(ctx)

        root_path = str(tmp_path / "project_root")
        handler.get_working_dir.return_value = root_path

        mock_lark = MockLarkChatClient.return_value
        mock_lark.create_chat.return_value = _FakeCreateChatResult(
            chat_id="oc_survive", name="SurviveTeam"
        )

        # Step 1: Create team (writes marker to disk)
        handler.create_team("msg1", "oc_origin", "SurviveTeam", project=None)

        # Verify original manager has the state
        original_manager = ctx.slock_engine_manager
        assert original_manager.is_managed_chat("oc_survive") is True
        assert original_manager.get_activated_engine("oc_survive") is not None

        # Step 2: Simulate restart — create a brand new manager
        new_manager = SlockEngineManager()
        assert new_manager.is_managed_chat("oc_survive") is False

        # Step 3: Restore from disk
        restored = new_manager.restore_from_disk(root_path)

        assert restored == 1
        assert new_manager.is_managed_chat("oc_survive") is True
        engine = new_manager.get_activated_engine("oc_survive")
        assert engine is not None
        assert engine.channel is not None
        assert engine.channel.channel_id == "oc_survive"
        assert engine.channel.team_name == "SurviveTeam"
