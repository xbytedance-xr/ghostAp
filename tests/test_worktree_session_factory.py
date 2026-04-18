"""Tests for create_sync_session_for_worktree()."""

from unittest.mock import MagicMock, patch


def test_acp_provider_maps_to_tool_name():
    """provider='acp', tool_name='coco' → agent_type='coco'."""
    with patch("src.agent_session.create_sync_session") as mock_create:
        mock_create.return_value = MagicMock()
        from src.agent_session import create_sync_session_for_worktree

        session = create_sync_session_for_worktree(
            provider="acp", tool_name="coco", working_dir="/tmp/wt1"
        )
        mock_create.assert_called_once_with(agent_type="coco", cwd="/tmp/wt1", model_name=None)
        assert session is mock_create.return_value


def test_cli_provider_maps_to_claude():
    """provider='cli', tool_name='claude' → agent_type='claude'."""
    with patch("src.agent_session.create_sync_session") as mock_create:
        mock_create.return_value = MagicMock()
        from src.agent_session import create_sync_session_for_worktree

        create_sync_session_for_worktree(
            provider="cli", tool_name="claude", working_dir="/tmp/wt2"
        )
        mock_create.assert_called_once_with(agent_type="claude", cwd="/tmp/wt2", model_name=None)


def test_ttadk_provider_maps_to_prefixed_agent_type():
    """provider='ttadk', tool_name='codex' → agent_type='ttadk_codex'."""
    with patch("src.agent_session.create_sync_session") as mock_create:
        mock_create.return_value = MagicMock()
        from src.agent_session import create_sync_session_for_worktree

        create_sync_session_for_worktree(
            provider="ttadk", tool_name="codex", working_dir="/tmp/wt3", model_name="gpt-4.1"
        )
        mock_create.assert_called_once_with(agent_type="ttadk_codex", cwd="/tmp/wt3", model_name="gpt-4.1")


def test_working_dir_passed_as_cwd():
    """working_dir should be forwarded as cwd parameter."""
    with patch("src.agent_session.create_sync_session") as mock_create:
        mock_create.return_value = MagicMock()
        from src.agent_session import create_sync_session_for_worktree

        create_sync_session_for_worktree(
            provider="acp", tool_name="gemini", working_dir="/my/worktree/path"
        )
        _, kwargs = mock_create.call_args
        assert kwargs["cwd"] == "/my/worktree/path"


def test_model_name_forwarded():
    """model_name should pass through."""
    with patch("src.agent_session.create_sync_session") as mock_create:
        mock_create.return_value = MagicMock()
        from src.agent_session import create_sync_session_for_worktree

        create_sync_session_for_worktree(
            provider="ttadk", tool_name="trae", working_dir="/tmp", model_name="claude-3.7-sonnet"
        )
        _, kwargs = mock_create.call_args
        assert kwargs["model_name"] == "claude-3.7-sonnet"


def test_empty_provider_defaults_to_tool_name():
    """Empty provider → fallback to tool_name as agent_type."""
    with patch("src.agent_session.create_sync_session") as mock_create:
        mock_create.return_value = MagicMock()
        from src.agent_session import create_sync_session_for_worktree

        create_sync_session_for_worktree(provider="", tool_name="coco", working_dir="/tmp")
        mock_create.assert_called_once_with(agent_type="coco", cwd="/tmp", model_name=None)
