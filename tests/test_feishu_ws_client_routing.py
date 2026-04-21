from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.feishu.ws_client import FeishuWSClient
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import WorktreeRuntimeState, WorktreeJourneyState, WorktreeJourneyStatus


def _dummy_callback(message_id: str, chat_id: str, text: str, parent_id: str | None) -> None:
    # 测试用占位回调，不做任何实际发送
    return None


class TestFeishuWSClientWorktreeHelpers:
    def test_is_worktree_awaiting_goal_delegates_to_worktree_manager(self):
        """WS 层的 _is_worktree_awaiting_goal 应直接委托给 WorktreeManager.is_awaiting_goal。"""

        client = FeishuWSClient(message_callback=_dummy_callback)

        state = WorktreeRuntimeState()
        state.journey = WorktreeJourneyState(status=WorktreeJourneyStatus.PENDING)
        project = SimpleNamespace(worktree_state=state)

        with patch.object(WorktreeManager, "is_awaiting_goal", return_value=True) as mock:
            assert client._is_worktree_awaiting_goal(project) is True
            mock.assert_called_once_with(state)

    def test_is_worktree_awaiting_goal_safe_when_state_missing(self):
        """当 project.worktree_state 缺失或为 None 时，应安全返回 False。"""

        client = FeishuWSClient(message_callback=_dummy_callback)
        project = SimpleNamespace()  # 无 worktree_state 属性

        with patch.object(WorktreeManager, "is_awaiting_goal", wraps=WorktreeManager.is_awaiting_goal) as mock:
            # getattr(project, "worktree_state", None) 将返回 None，委托层应返回 False
            assert client._is_worktree_awaiting_goal(project) is False
            mock.assert_called_once_with(None)


class TestFeishuWSClientSessionManagers:
    def test_uses_default_acp_session_managers(self):
        """构造 FeishuWSClient 时应仍然能够无感初始化各 ACPSessionManager。"""

        client = FeishuWSClient(message_callback=_dummy_callback)

        # 仅做存在性与类型层面的 smoke 检查，避免真正启动 ACP/CLI 会话
        assert client._coco_manager is not None
        assert client._ttadk_manager is not None

