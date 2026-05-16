from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.acp.manager import ACPSessionManager


class _FakeTTADKManager:
    def get_current_model(self):
        return "bad-model"

    def resolve_startup_model_with_diagnostics(self, *args, **kwargs):
        return {
            "tool": "coco",
            "input_model": "bad-model",
            "model": None,
            "resolved_model": None,
            "resolved_real_name": "bad-model",
            "validated": False,
            "source": "test",
            "warnings": [],
            "diagnostics": {},
        }


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
        cwd=str(Path.cwd()),
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
        cwd=str(Path.cwd()),
        model_name="default"
    )

    # Must not construct ACP session at all
    mock_acp_cls.assert_not_called()

    assert session is mock_session_instance


def test_ttadk_startup_failure_does_not_fallback_to_coco_acp(monkeypatch):
    """TTADK startup failure must surface diagnostics instead of creating coco ACP."""
    import src.acp.sync_adapter as sync_adapter
    import src.ttadk as ttadk_pkg
    from src.ttadk import startup as startup_mod

    monkeypatch.setattr(ttadk_pkg, "get_ttadk_manager", lambda: _FakeTTADKManager())

    def _fail_ttadk(*args, **kwargs):
        raise RuntimeError("ttadk cli unavailable")

    mock_sync_acp = MagicMock(name="SyncACPSession")
    mock_coco_start = MagicMock(name="start_session_with_retry")
    monkeypatch.setattr(sync_adapter, "SyncACPSession", mock_sync_acp)
    monkeypatch.setattr(sync_adapter, "start_ttadk_session_with_pty_retry", _fail_ttadk)
    monkeypatch.setattr(sync_adapter, "_call_start_session_with_retry_compat", mock_coco_start, raising=False)

    with pytest.raises(RuntimeError, match="启动 ttadk_coco ACP Server 失败"):
        startup_mod.start_agent_session(agent_type="ttadk_coco", cwd="/tmp", model_name="bad-model")

    mock_sync_acp.assert_not_called()
    mock_coco_start.assert_not_called()


def test_ttadk_engine_session_default_fallback_does_not_start_coco_acp(monkeypatch):
    """Engine-session default fallback returns degraded diagnostics without coco ACP."""
    import src.acp.sync_adapter as sync_adapter
    from src.ttadk.engine_session import start_ttadk_engine_session

    mock_coco_start = MagicMock(name="start_session_with_retry")
    monkeypatch.setattr(sync_adapter, "start_session_with_retry", mock_coco_start)

    def _fail_start(*args, **kwargs):
        raise RuntimeError("ttadk startup failed")

    result = start_ttadk_engine_session(
        agent_type="ttadk_coco",
        cwd="/tmp",
        model_intent="bad-model",
        startup_timeout=1,
        manager=_FakeTTADKManager(),
        start_ttadk_session_fn=_fail_start,
        resolve_agent_spec_fn=lambda *args, **kwargs: ("ttadk", []),
        precheck_fn=lambda _intent: {
            "tool": "coco",
            "input_model": "bad-model",
            "model": None,
            "resolved_model": None,
            "resolved_real_name": "bad-model",
            "validated": False,
            "source": "test",
            "warnings": [],
            "diagnostics": {},
        },
    )

    assert result["degraded"] is True
    assert result["result"] == (None, "")
    mock_coco_start.assert_not_called()


def test_ttadk_runtime_invalid_model_does_not_replace_session_with_coco_acp(monkeypatch):
    """Runtime self-healing may report degraded state but must not create coco ACP."""
    import src.acp.sync_adapter as sync_adapter
    import src.ttadk.models as ttadk_models
    from src.agent_session.wrappers import ModelFailureAwareSession

    class _Inner:
        session_id = "ttadk-session"
        created_at = 0.0
        last_active = 0.0
        message_count = 0
        last_query = ""
        is_resumed = False
        _agent_type = "ttadk_coco"
        _agent_args = ["-m", "bad-model"]
        _cwd = "/tmp"

        def send_prompt(self, *args, **kwargs):
            raise RuntimeError("invalid model: bad-model. available models: good-model")

        def describe_agent(self):
            return "ttadk_coco"

        def close(self):
            return None

    original_inner = _Inner()
    wrapper = ModelFailureAwareSession(original_inner)
    monkeypatch.setattr(wrapper, "_do_failover", lambda *args, **kwargs: False)
    monkeypatch.setattr(wrapper, "_do_ttadk_auto", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        ttadk_models,
        "build_invalid_model_context",
        lambda *args, **kwargs: {
            "is_invalid_model": True,
            "available_models": ["good-model"],
        },
    )
    mock_coco_start = MagicMock(name="start_session_with_retry")
    monkeypatch.setattr(sync_adapter, "start_session_with_retry", mock_coco_start)

    with pytest.raises(RuntimeError, match="ttadk_runtime_invalid_model_unrecoverable") as excinfo:
        wrapper.send_prompt("hello")

    assert wrapper._inner is original_inner
    assert getattr(excinfo.value, "attempts")[-1]["step"] == "degraded_result"
    assert getattr(excinfo.value, "attempts")[-1]["session_replaced"] is False
    mock_coco_start.assert_not_called()
