import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from src.feishu.handlers.system import SystemHandler
from src.worktree_engine.models import WorktreeRuntimeState

class TestWorktreeGoalPersistence(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        # Mock handlers dict for SystemHandler init
        self.ctx.handlers = {}
        self.handler = SystemHandler(self.ctx)
        self.project = SimpleNamespace(
            project_id="p1",
            worktree_state=WorktreeRuntimeState()
        )
        self.handler.project_manager.get_project.return_value = self.project
        self.handler.project_manager.get_active_project.return_value = self.project

    def test_handle_worktree_select_tool_persists_goal(self):
        # 初始目标为空
        self.assertEqual(self.project.worktree_state.selection.pending_goal, "")
        
        # 模拟选择工具，并在输入框中填入了目标 (goal)
        value = {
            "tool_name": "coco",
            "goal": "Refactor login"
        }
        
        with patch.object(self.handler, "_get_available_worktree_tools", return_value=[]), \
             patch.object(self.handler, "_get_models_for_tool", return_value=[]), \
             patch.object(self.handler, "patch_message"):
            self.handler.handle_worktree_select_tool("m1", "c1", "p1", value)
            
        # 验证目标已持久化
        self.assertEqual(self.project.worktree_state.selection.pending_goal, "Refactor login")

    def test_handle_worktree_select_model_persists_goal(self):
        # 预设一个目标
        self.project.worktree_state.selection.pending_goal = "Old goal"
        
        # 模拟选择模型，并更改了目标 (goal)
        value = {
            "model_name": "gpt-4",
            "goal": "New goal"
        }
        
        with patch.object(self.handler, "patch_message"), \
             patch.object(self.handler, "handle_finish_worktree_selection"):
            self.handler.handle_worktree_select_model("m1", "c1", "p1", value)
            
        # 验证目标已更新
        self.assertEqual(self.project.worktree_state.selection.pending_goal, "New goal")

    def test_handle_worktree_confirm_start_uses_persisted_goal(self):
        # 预设目标
        self.project.worktree_state.selection.pending_goal = "Final goal"
        
        # 模拟点击确认按钮，value 中传 worktree_goal（来自卡片输入框）
        value = {"action": "worktree_confirm_start", "worktree_goal": "Final goal"}
        
        # 模拟 ensure_worktrees 成功
        mock_state = self.project.worktree_state
        mock_state.last_error = None
        mock_state.units = []
        
        with patch.object(self.handler, "_worktree_manager") as mock_mgr_factory, \
             patch.object(self.handler, "handle_worktree_execute") as mock_exec, \
             patch.object(self.handler, "patch_message"):
            mock_mgr = mock_mgr_factory.return_value
            mock_mgr.ensure_worktrees.return_value = mock_state
            mock_mgr.get_state.return_value = mock_state
            
            self.handler.handle_worktree_confirm_start("m1", "c1", "p1", value)
            
        # 验证 handle_worktree_execute 被调用且使用了目标
        mock_exec.assert_called_once_with("m1", "c1", "Final goal", project=self.project)

if __name__ == "__main__":
    unittest.main()
