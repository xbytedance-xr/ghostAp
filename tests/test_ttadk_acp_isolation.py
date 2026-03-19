import pytest
from unittest.mock import patch, MagicMock

from src.acp.manager import ACPSessionManager
from src.agent_session import SyncTTADKCLISession


@patch('src.acp.manager.SyncACPSession')
@patch('src.acp.manager.SyncTTADKCLISession')
@patch('src.ttadk.startup_common.precheck_ttadk_startup_model')
def test_manager_ttadk_force_cli_session(mock_precheck, mock_session_cls, mock_acp_cls):
    """Verify that using a ttadk_ prefix forces CLI session, even for tools that might support ACP."""
    
    # Setup mocks
    mock_precheck.return_value = {"model": "gpt-4"}
    
    mock_session_instance = MagicMock()
    mock_session_instance.start.return_value = "test-session-123"
    mock_session_instance.describe_agent.return_value = "ttadk_aiden"
    mock_session_cls.return_value = mock_session_instance
    
    manager = ACPSessionManager("ttadk")
    
    # Test with aiden (which we know supports ACP through the provider)
    # The key is that the prefix 'ttadk_' forces it through the TTADK CLI path
    session = manager.start_session(
        chat_id="chat123",
        agent_type_override="ttadk_aiden"
    )
    
    # Verify SyncTTADKCLISession was used
    mock_session_cls.assert_called_once_with(
        agent_type="ttadk_aiden", 
        cwd=".", 
        model_name="gpt-4"
    )

    # Must not construct ACP session at all
    mock_acp_cls.assert_not_called()

    # Check session wasn't started with ACP sync adapter
    assert session is mock_session_instance


@patch('src.acp.manager.SyncACPSession')
@patch('src.acp.manager.SyncTTADKCLISession')
@patch('src.ttadk.startup_common.precheck_ttadk_startup_model')
def test_manager_ttadk_codex_force_cli_session(mock_precheck, mock_session_cls, mock_acp_cls):
    """Verify TTADK codex requests force CLI session."""
    
    # Setup mocks
    mock_precheck.return_value = {"model": "default"}
    
    mock_session_instance = MagicMock()
    mock_session_instance.start.return_value = "test-session-456"
    mock_session_cls.return_value = mock_session_instance
    
    manager = ACPSessionManager("ttadk")
    
    # Test with codex
    session = manager.start_session(
        chat_id="chat456",
        agent_type_override="ttadk_codex"
    )
    
    # Verify SyncTTADKCLISession was used
    mock_session_cls.assert_called_once_with(
        agent_type="ttadk_codex", 
        cwd=".", 
        model_name="default"
    )

    # Must not construct ACP session at all
    mock_acp_cls.assert_not_called()
    
    assert session is mock_session_instance
