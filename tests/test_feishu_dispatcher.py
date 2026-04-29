import pytest
from unittest.mock import MagicMock, patch
from src.feishu.dispatcher import MessageDispatcher
from src.agent.intent_recognizer import IntentType
from src.mode.manager import InteractionMode

class TestMessageDispatcher:
    def setup_method(self):
        self.client = MagicMock()
        self.dispatcher = MessageDispatcher(self.client)

    def test_process_with_intent_deep_command(self):
        self.client._is_deep_command.return_value = True
        self.client._is_loop_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command.return_value = False
        
        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)
        
        self.dispatcher.process_with_intent("m1", "c1", "/deep task", None)
        
        self.client._handle_deep_command.assert_called_once_with("m1", "c1", "/deep task", None)
        assert self.client._add_reaction.call_count >= 1

    @patch("src.thread.get_current_thread_id", return_value=None)
    def test_process_with_intent_programming_mode_forwarding(self, mock_tid):
        self.client._is_deep_command.return_value = False
        self.client._is_loop_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command.return_value = False
        self.client._is_exit_command.return_value = False
        self.client.settings.thread_programming_enabled = False
        
        # In COCO mode
        self.client._get_effective_mode.return_value = (InteractionMode.COCO, True)
        
        mock_handler = MagicMock()
        self.client._get_mode_handler.return_value = mock_handler

        self.dispatcher.process_with_intent("m1", "c1", "hello coco", None)

        self.client._get_mode_handler.assert_called_once_with(InteractionMode.COCO)
        mock_handler.handle_message.assert_called_once_with("m1", "c1", "hello coco", None)

    def test_process_with_intent_exit_command(self):
        self.client._is_deep_command.return_value = False
        self.client._is_loop_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command.return_value = False
        self.client._is_exit_command.return_value = True
        self.client._control_plane.should_defer_exit.return_value = False
        
        self.client._get_effective_mode.return_value = (InteractionMode.COCO, True)
        
        self.dispatcher.process_with_intent("m1", "c1", "/exit", None)
        
        self.client._exit_current_mode.assert_called_once()

    def test_execute_single_task_enter_coco(self):
        task = MagicMock()
        task.intent = IntentType.ENTER_COCO
        
        self.dispatcher.execute_single_task("m1", "c1", task, "/coco", None)
        
        self.client._system_handler.handle_select_acp_tool.assert_called_once()

    def test_execute_single_task_shell(self):
        task = MagicMock()
        task.intent = IntentType.SHELL_COMMAND
        task.data = {"command": "ls"}
        self.client._get_working_dir.return_value = "/tmp"
        
        self.dispatcher.execute_single_task("m1", "c1", task, "ls", None)
        
        self.client._submit_shell_command.assert_called_once_with("m1", "c1", "ls", "/tmp", None)

    def test_execute_multi_tasks(self):
        intent_result = MagicMock()
        task1 = MagicMock(intent=IntentType.CHANGE_DIR, data={"path": "/tmp"}, description="cd /tmp")
        task2 = MagicMock(intent=IntentType.SHELL_COMMAND, data={"command": "ls"}, description="ls")
        intent_result.tasks = [task1, task2]
        
        with patch.object(self.dispatcher, "execute_task_step", return_value=True) as mock_step:
            self.dispatcher.execute_multi_tasks("m1", "c1", intent_result, None)
            assert mock_step.call_count == 2

    def test_execute_task_step_change_dir(self):
        task = MagicMock(intent=IntentType.CHANGE_DIR, data={"path": "/tmp"})
        self.client._set_working_dir.return_value = (True, "/tmp")
        
        result = self.dispatcher.execute_task_step("m1", "c1", task, 1, 1, None)
        
        assert result is True
        self.client._set_working_dir.assert_called_once_with("c1", "/tmp")

    def test_process_with_intent_smart_mode_recognition(self):
        self.client._is_deep_command.return_value = False
        self.client._is_loop_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command.return_value = False
        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)
        self.client._pending_image_lock = MagicMock()
        self.client._pending_image_only = set()
        
        intent_result = MagicMock()
        intent_result.is_multi_task = False
        self.client._intent_recognizer.recognize.return_value = intent_result
        
        with patch.object(self.dispatcher, "execute_single_task") as mock_exec:
            self.dispatcher.process_with_intent("m1", "c1", "help me", None)
            mock_exec.assert_called_once()
