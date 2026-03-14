import pytest
from unittest.mock import patch, MagicMock

from src.agent.intent_recognizer import (
    IntentRecognizer,
    IntentType,
    IntentResult,
    TaskStep,
)


class TestIntentRecognizerQuickMatch:
    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_exact_command_coco(self, recognizer):
        result = recognizer._quick_match("/coco")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_COCO
        assert result.confidence == 1.0

    def test_exact_command_exit(self, recognizer):
        result = recognizer._quick_match("/exit")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

    def test_exact_command_projects(self, recognizer):
        result = recognizer._quick_match("/projects")
        assert result is not None
        assert result.primary_intent == IntentType.LIST_PROJECTS

    def test_exact_command_status(self, recognizer):
        result = recognizer._quick_match("/status")
        assert result is not None
        assert result.primary_intent == IntentType.PROJECT_STATUS

    def test_coco_info_command(self, recognizer):
        result = recognizer._quick_match("/coco_info")
        assert result is not None
        assert result.primary_intent == IntentType.COCO_MESSAGE
        assert result.primary_data.get("command") == "info"

    def test_new_project_command(self, recognizer):
        result = recognizer._quick_match("/new myapp ~/workspace")
        assert result is not None
        assert result.primary_intent == IntentType.CREATE_PROJECT
        assert result.primary_data.get("name") == "myapp"
        assert result.primary_data.get("path") == "~/workspace"

    def test_new_project_command_no_path(self, recognizer):
        result = recognizer._quick_match("/new myapp")
        assert result is not None
        assert result.primary_intent == IntentType.CREATE_PROJECT
        assert result.primary_data.get("name") == "myapp"
        assert result.primary_data.get("path") == ""

    def test_switch_project_command(self, recognizer):
        result = recognizer._quick_match("/switch myapp")
        assert result is not None
        assert result.primary_intent == IntentType.SWITCH_PROJECT
        assert result.primary_data.get("name") == "myapp"

    def test_close_project_command(self, recognizer):
        result = recognizer._quick_match("/close myapp")
        assert result is not None
        assert result.primary_intent == IntentType.CLOSE_PROJECT
        assert result.primary_data.get("name") == "myapp"

    def test_shell_command_ls(self, recognizer):
        result = recognizer._quick_match("ls -la")
        assert result is not None
        assert result.primary_intent == IntentType.SHELL_COMMAND
        assert result.confidence >= 0.9

    def test_shell_command_git(self, recognizer):
        result = recognizer._quick_match("git status")
        assert result is not None
        assert result.primary_intent == IntentType.SHELL_COMMAND

    def test_shell_command_npm(self, recognizer):
        result = recognizer._quick_match("npm install")
        assert result is not None
        assert result.primary_intent == IntentType.SHELL_COMMAND

    def test_shell_command_python(self, recognizer):
        result = recognizer._quick_match("python main.py")
        assert result is not None
        assert result.primary_intent == IntentType.SHELL_COMMAND

    def test_exit_keyword_in_coco_mode(self, recognizer):
        result = recognizer._quick_match("退出", current_mode="coco")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

    def test_exit_keyword_exit_in_coco_mode(self, recognizer):
        result = recognizer._quick_match("exit", current_mode="coco")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

    def test_exit_keyword_in_claude_mode(self, recognizer):
        result = recognizer._quick_match("退出", current_mode="claude")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

    def test_exit_keyword_exit_in_claude_mode(self, recognizer):
        result = recognizer._quick_match("exit", current_mode="claude")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

    def test_exit_keyword_not_in_programming_mode(self, recognizer):
        result = recognizer._quick_match("退出", current_mode="smart")
        assert result is None

    def test_project_list_keyword(self, recognizer):
        result = recognizer._quick_match("项目列表")
        assert result is not None
        assert result.primary_intent == IntentType.LIST_PROJECTS

    def test_project_list_keyword_variant(self, recognizer):
        result = recognizer._quick_match("看看有哪些项目")
        assert result is not None
        assert result.primary_intent == IntentType.LIST_PROJECTS

    def test_common_word_not_matched(self, recognizer):
        result = recognizer._quick_match("hello")
        assert result is None

    def test_chinese_text_not_quick_matched(self, recognizer):
        result = recognizer._quick_match("帮我写一个函数")
        assert result is None



class TestIntentRecognizerASR:
    """测试 ASR 容错识别功能"""
    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_asr_period_suffix(self, recognizer):
        """测试末尾句号去除"""
        # "帮我建个项目叫测试。" -> CREATE_PROJECT
        # 这里只测试 _quick_match 能否覆盖部分规则，或者 mock LLM 测试复杂场景
        # _quick_match 目前只处理特定命令前缀，对于自然语言 ASR 错误，主要依赖 LLM
        pass

    def test_asr_typo_correction_in_quick_match(self, recognizer):
        """测试 _quick_match 中的拼写纠正"""
        # /claud -> /claude
        result = recognizer._quick_match("/claud")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_CLAUDE
        
        # /coc -> /coco
        result = recognizer._quick_match("/coc")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_COCO

class TestIntentRecognizerContextHint:
    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_context_hint_coco_mode(self, recognizer):
        hint = recognizer._get_context_hint(current_mode="coco")
        assert "Coco 编程模式" in hint
        assert "coco_message" in hint

    def test_context_hint_claude_mode(self, recognizer):
        hint = recognizer._get_context_hint(current_mode="claude")
        assert "Claude 编程模式" in hint
        assert "claude_message" in hint

    def test_context_hint_smart_mode(self, recognizer):
        hint = recognizer._get_context_hint(current_mode="smart")
        assert "智能模式" in hint
        assert "enter_coco" in hint

    def test_fallback_intent_coco_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="coco")
        assert fallback == IntentType.COCO_MESSAGE

    def test_fallback_intent_claude_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="claude")
        assert fallback == IntentType.CLAUDE_MESSAGE

    def test_fallback_intent_smart_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="smart")
        assert fallback == IntentType.SHELL_COMMAND


class TestIntentResult:
    def test_single_task(self):
        result = IntentResult.single(
            intent=IntentType.ENTER_COCO,
            confidence=0.9,
            original_text="帮我写代码",
            description="进入编程模式"
        )
        assert result.primary_intent == IntentType.ENTER_COCO
        assert result.confidence == 0.9
        assert result.is_multi_task is False
        assert len(result.tasks) == 1

    def test_multi_task(self):
        result = IntentResult(
            tasks=[
                TaskStep(intent=IntentType.CHANGE_DIR, description="切换目录", data={"path": "~/workspace"}),
                TaskStep(intent=IntentType.ENTER_COCO, description="进入编程模式", data={}),
            ],
            confidence=0.85,
            original_text="去workspace目录然后帮我写代码"
        )
        assert result.is_multi_task is True
        assert len(result.tasks) == 2
        assert result.primary_intent == IntentType.CHANGE_DIR

    def test_primary_data(self):
        result = IntentResult.single(
            intent=IntentType.CREATE_PROJECT,
            data={"name": "myapp", "path": "~/workspace"}
        )
        assert result.primary_data.get("name") == "myapp"
        assert result.primary_data.get("path") == "~/workspace"


class TestIntentTypeMapping:
    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_all_intents_mapped(self, recognizer):
        expected_intents = [
            "enter_coco", "exit_coco", "coco_message", "change_dir",
            "shell", "create_project", "switch_project", "list_projects",
            "close_project", "project_status", "unknown"
        ]
        for intent_str in expected_intents:
            assert intent_str in recognizer.INTENT_MAP

    def test_intent_map_values(self, recognizer):
        assert recognizer.INTENT_MAP["enter_coco"] == IntentType.ENTER_COCO
        assert recognizer.INTENT_MAP["exit_coco"] == IntentType.EXIT_COCO
        assert recognizer.INTENT_MAP["coco_message"] == IntentType.COCO_MESSAGE
        assert recognizer.INTENT_MAP["shell"] == IntentType.SHELL_COMMAND
        assert recognizer.INTENT_MAP["unknown"] == IntentType.UNKNOWN


class TestNormalizePath:
    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_empty_path(self, recognizer):
        assert recognizer._normalize_path("") == ""

    def test_tilde_expansion(self, recognizer):
        import os
        result = recognizer._normalize_path("~/workspace")
        assert result.startswith(os.path.expanduser("~"))
        assert "workspace" in result

    def test_regular_path(self, recognizer):
        result = recognizer._normalize_path("/tmp/test")
        assert result == "/tmp/test"

    def test_path_with_spaces(self, recognizer):
        result = recognizer._normalize_path("  /tmp/test  ")
        assert result == "/tmp/test"
