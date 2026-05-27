import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agent.intent_recognizer import IntentType
from src.feishu.dispatcher import MessageDispatcher
from src.feishu.slash_command_parser import SlashCommandParser
from src.mode.manager import InteractionMode


class TestMessageDispatcher:
    def setup_method(self):
        self.client = MagicMock()
        self.client._is_slock_command.return_value = False
        self.client._is_slock_active.return_value = False
        self.client._is_slock_managed_chat.return_value = False
        self.dispatcher = MessageDispatcher(self.client)

    def test_process_with_intent_deep_command(self):
        self.client._is_deep_command.return_value = True
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False

        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)

        self.dispatcher.process_with_intent(
            "m1",
            "c1",
            "/deep task",
            None,
            command_match=SlashCommandParser.parse("/deep task"),
        )

        self.client._handle_deep_command.assert_called_once_with("m1", "c1", "/deep task", None)
        assert self.client._add_reaction.call_count >= 1

    @patch("src.thread.get_current_thread_id", return_value=None)
    def test_process_with_intent_programming_mode_forwarding(self, mock_tid):
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False
        self.client._is_exit_command.return_value = False
        self.client.settings.thread_programming_enabled = False

        # In COCO mode
        self.client._get_effective_mode.return_value = (InteractionMode.COCO, True)

        mock_handler = MagicMock()
        self.client._get_mode_handler.return_value = mock_handler

        self.dispatcher.process_with_intent(
            "m1",
            "c1",
            "hello coco",
            None,
            command_match=SlashCommandParser.parse("hello coco"),
        )

        self.client._get_mode_handler.assert_called_once_with(InteractionMode.COCO)
        mock_handler.handle_message.assert_called_once_with("m1", "c1", "hello coco", None)

    @patch("src.thread.get_current_thread_id", return_value=None)
    def test_process_with_request_context_programming_mode_forwarding(self, mock_tid):
        from src.feishu.dispatcher import FeishuRequestContext

        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False
        self.client._is_exit_command.return_value = False
        self.client.settings.thread_programming_enabled = False
        self.client._get_effective_mode.return_value = (InteractionMode.COCO, True)
        mock_handler = MagicMock()
        self.client._get_mode_handler.return_value = mock_handler

        req = FeishuRequestContext(message_id="m1", chat_id="c1", text="hello coco", project=None)
        self.dispatcher.process_request(req)

        self.client._get_mode_handler.assert_called_once_with(InteractionMode.COCO)
        mock_handler.handle_message.assert_called_once_with("m1", "c1", "hello coco", None)

    def test_process_with_intent_exit_command(self):
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False
        self.client._is_exit_command.return_value = True
        self.client._control_plane.should_defer_exit.return_value = False

        self.client._get_effective_mode.return_value = (InteractionMode.COCO, True)

        self.dispatcher.process_with_intent(
            "m1",
            "c1",
            "/exit",
            None,
            command_match=SlashCommandParser.parse("/exit"),
        )

        self.client._exit_current_mode.assert_called_once()

    def test_execute_single_task_enter_coco(self):
        task = MagicMock()
        task.intent = IntentType.ENTER_COCO
        self.client._mode_manager.is_coco_mode.return_value = False

        self.dispatcher.execute_single_task("m1", "c1", task, "/coco", None)

        self.client._system_handler.handle_select_acp_tool.assert_called_once()

    def test_execute_single_task_enter_codex_shows_model_select(self):
        task = MagicMock()
        task.intent = IntentType.ENTER_CODEX
        task.data = {}
        project = MagicMock()
        project.project_id = "pid1"
        self.client._mode_manager.is_codex_mode.return_value = False

        self.dispatcher.execute_single_task("m1", "c1", task, "/codex", project)

        self.client._system_handler.handle_select_acp_tool.assert_called_once_with(
            "m1", "c1", "codex", project_id="pid1", pending_prompt=None
        )
        self.client._enter_codex_mode.assert_not_called()

    def test_process_with_intent_reparses_codex_slash_and_bypasses_intent(self):
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.side_effect = lambda m: bool(m and m.command == "/codex")
        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)

        self.dispatcher.process_with_intent("m1", "c1", "/codex", None)

        self.client._handle_intercepted_command.assert_called_once()
        args, kwargs = self.client._handle_intercepted_command.call_args
        assert args[:4] == ("m1", "c1", "/codex", None)
        assert kwargs["command_match"].command == "/codex"
        self.client._intent_recognizer.recognize.assert_not_called()

    def test_process_with_intent_unknown_slash_still_bypasses_intent(self):
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False
        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)

        self.dispatcher.process_with_intent("m1", "c1", "/unknown_command", None)

        self.client._handle_intercepted_command.assert_called_once()
        args, kwargs = self.client._handle_intercepted_command.call_args
        assert args[:4] == ("m1", "c1", "/unknown_command", None)
        assert kwargs["command_match"].command == "/unknown_command"
        self.client._intent_recognizer.recognize.assert_not_called()

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
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False
        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)
        self.client._pending_image_lock = MagicMock()
        self.client._pending_image_only = set()

        intent_result = MagicMock()
        intent_result.is_multi_task = False
        self.client._intent_recognizer.recognize.return_value = intent_result

        with patch.object(self.dispatcher, "execute_single_task") as mock_exec:
            self.dispatcher.process_with_intent(
                "m1",
                "c1",
                "help me",
                None,
                command_match=SlashCommandParser.parse("help me"),
            )
            mock_exec.assert_called_once()

    def test_dispatcher_classifies_startup_and_dispatch_failures(self):
        from src.feishu.dispatcher import DispatchErrorAction, classify_dispatch_error

        intent_failure = classify_dispatch_error(RuntimeError("llm down"), phase="intent_recognition")
        assert intent_failure.action == DispatchErrorAction.FALLBACK_TO_SHELL
        assert intent_failure.user_reachable is True

        model_card_failure = classify_dispatch_error(RuntimeError("card failed"), phase="coco_model_card")
        assert model_card_failure.action == DispatchErrorAction.FALLBACK_TO_DIRECT_ENTER
        assert model_card_failure.user_reachable is True

        forward_failure = classify_dispatch_error(RuntimeError("forward failed"), phase="pending_prompt_forward")
        assert forward_failure.action == DispatchErrorAction.LOG_AND_CONTINUE

        task_failure = classify_dispatch_error(RuntimeError("step failed"), phase="multi_task_step")
        assert task_failure.action == DispatchErrorAction.STOP_MULTI_TASK

    def test_dispatcher_recoverable_intent_error_falls_back_to_shell_with_log(self, caplog):
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False
        self.client._get_effective_mode.return_value = (InteractionMode.SMART, False)
        self.client._pending_image_lock = MagicMock()
        self.client._pending_image_only = set()
        self.client._intent_recognizer.recognize.side_effect = TimeoutError("llm timeout")
        self.client._get_working_dir.return_value = "/repo"
        self.client.settings.slock_passive_mode = False

        with caplog.at_level(logging.WARNING, logger="src.feishu.dispatcher"):
            self.dispatcher.process_with_intent(
                "m1",
                "c1",
                "ls -la",
                None,
                command_match=SlashCommandParser.parse("ls -la"),
            )

        self.client._submit_shell_command.assert_called_once_with("m1", "c1", "ls -la", "/repo", None)
        assert "意图识别异常" in caplog.text

    def test_dispatcher_degraded_coco_card_error_enters_directly_with_warning(self, caplog):
        self.client._mode_manager.is_coco_mode.return_value = False
        self.client._system_handler.handle_select_acp_tool.side_effect = RuntimeError("card failed")

        with caplog.at_level(logging.WARNING, logger="src.feishu.dispatcher"):
            self.dispatcher._handle_enter_coco("m1", "c1", project=None)

        self.client._enter_coco_mode.assert_called_once_with("m1", "c1", project=None)
        assert "回退直接进入" in caplog.text

    def test_dispatcher_fatal_programming_error_propagates_without_success_reply(self):
        self.client._get_effective_mode.side_effect = AssertionError("programming invariant broken")

        with pytest.raises(AssertionError):
            self.dispatcher.process_with_intent(
                "m1",
                "c1",
                "hello",
                None,
                command_match=SlashCommandParser.parse("hello"),
            )

        self.client._submit_shell_command.assert_not_called()
        self.client._reply_text.assert_not_called()

    def test_dispatcher_key_paths_have_no_uncategorized_broad_catches(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "src" / "feishu" / "dispatcher.py").read_text(encoding="utf-8"))
        key_functions = {"process_with_intent", "_handle_enter_coco", "execute_task_step"}
        broad_by_function: dict[str, list[int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in key_functions:
                broad_by_function[node.name] = [
                    handler.lineno
                    for handler in ast.walk(node)
                    if isinstance(handler, ast.ExceptHandler)
                    and isinstance(handler.type, ast.Name)
                    and handler.type.id == "Exception"
                ]

        assert set(broad_by_function) == key_functions
        # process_with_intent has one intentional broad catch for autonomous resolver
        allowed = {"process_with_intent": 1, "_handle_enter_coco": 0, "execute_task_step": 0}
        for name in key_functions:
            assert len(broad_by_function[name]) <= allowed[name], (
                f"{name} has unexpected broad except Exception at lines {broad_by_function[name]}"
            )
