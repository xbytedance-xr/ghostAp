import pytest

from src.agent.intent_recognizer import (
    IntentRecognizer,
    IntentResult,
    IntentType,
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

    def test_exit_keyword_in_ttadk_mode(self, recognizer):
        result = recognizer._quick_match("退出", current_mode="ttadk")
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

    def test_fallback_intent_coco_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="coco")
        assert fallback == IntentType.COCO_MESSAGE

    def test_fallback_intent_claude_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="claude")
        assert fallback == IntentType.CLAUDE_MESSAGE

    def test_fallback_intent_gemini_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="gemini")
        assert fallback == IntentType.GEMINI_MESSAGE

    def test_fallback_intent_ttadk_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="ttadk")
        assert fallback == IntentType.TTADK_MESSAGE

    def test_fallback_intent_smart_mode(self, recognizer):
        fallback = recognizer._get_fallback_intent(current_mode="smart")
        assert fallback == IntentType.SHELL_COMMAND

    def test_exact_command_tools(self, recognizer):
        result = recognizer._quick_match("/tools")
        assert result is not None
        assert result.primary_intent == IntentType.SHOW_TOOLS
        assert result.confidence == 1.0

    def test_exact_command_tools_status(self, recognizer):
        result = recognizer._quick_match("/tools_status")
        assert result is not None
        assert result.primary_intent == IntentType.TOOLS_STATUS
        assert result.confidence == 1.0

    def test_exact_command_ttadk(self, recognizer):
        result = recognizer._quick_match("/ttadk")
        assert result is not None
        assert result.primary_intent == IntentType.TTADK_MESSAGE
        assert result.confidence == 1.0


class TestIntentResult:
    def test_single_task(self):
        result = IntentResult.single(
            intent=IntentType.ENTER_COCO, confidence=0.9, original_text="帮我写代码", description="进入编程模式"
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
            original_text="去workspace目录然后帮我写代码",
        )
        assert result.is_multi_task is True
        assert len(result.tasks) == 2
        assert result.primary_intent == IntentType.CHANGE_DIR

    def test_primary_data(self):
        result = IntentResult.single(intent=IntentType.CREATE_PROJECT, data={"name": "myapp", "path": "~/workspace"})
        assert result.primary_data.get("name") == "myapp"
        assert result.primary_data.get("path") == "~/workspace"


class TestIntentTypeMapping:
    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_all_intents_mapped(self, recognizer):
        expected_intents = [
            "enter_coco",
            "exit_coco",
            "coco_message",
            "enter_claude",
            "exit_claude",
            "claude_message",
            "enter_aiden",
            "exit_aiden",
            "aiden_message",
            "enter_codex",
            "exit_codex",
            "codex_message",
            "enter_gemini",
            "exit_gemini",
            "gemini_message",
            "ttadk_message",
            "change_dir",
            "shell",
            "create_project",
            "switch_project",
            "list_projects",
            "close_project",
            "project_status",
            "enter_deep",
            "deep_status",
            "stop_deep",
            "deep_update",
            "enter_spec",
            "spec_status",
            "stop_spec",
            "spec_pause",
            "spec_resume",
            "spec_guide",
            "show_help",
            "show_tools",
            "tools_status",
            "exit_mode",
            "unknown",
        ]
        for intent_str in expected_intents:
            assert intent_str in recognizer.INTENT_MAP

    def test_intent_map_values(self, recognizer):
        assert recognizer.INTENT_MAP["enter_coco"] == IntentType.ENTER_COCO
        assert recognizer.INTENT_MAP["exit_coco"] == IntentType.EXIT_COCO
        assert recognizer.INTENT_MAP["coco_message"] == IntentType.COCO_MESSAGE
        assert recognizer.INTENT_MAP["shell"] == IntentType.SHELL_COMMAND
        assert recognizer.INTENT_MAP["unknown"] == IntentType.UNKNOWN
        assert recognizer.INTENT_MAP["show_tools"] == IntentType.SHOW_TOOLS
        assert recognizer.INTENT_MAP["tools_status"] == IntentType.TOOLS_STATUS
        assert recognizer.INTENT_MAP["ttadk_message"] == IntentType.TTADK_MESSAGE


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


# ── Boundary test cases ──────────────────────────────────────────────


class TestQuickMatchBoundaryEdgeCases:
    """≥8 boundary tests covering edge cases not previously covered."""

    @pytest.fixture
    def recognizer(self):
        return IntentRecognizer()

    def test_empty_string_returns_none(self, recognizer):
        """Empty input should not match any intent."""
        result = recognizer._quick_match("")
        assert result is None

    def test_whitespace_only_returns_none(self, recognizer):
        """Whitespace-only input should not match any intent."""
        result = recognizer._quick_match("   ")
        assert result is None

    def test_exit_keyword_exactly_20_chars_in_programming_mode(self, recognizer):
        """Exit keyword in text exactly 20 chars long should NOT match (guard is len < 20)."""
        # "退出" is 2 chars, pad to exactly 20 chars
        text = "退出" + "x" * 18  # len == 20
        assert len(text) == 20
        result = recognizer._quick_match(text, current_mode="coco")
        assert result is None  # len < 20 guard prevents match

    def test_exit_keyword_19_chars_in_programming_mode(self, recognizer):
        """Exit keyword in text of 19 chars should match (guard is len < 20)."""
        text = "退出" + "x" * 17  # len == 19
        assert len(text) == 19
        result = recognizer._quick_match(text, current_mode="coco")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

    def test_deep_update_with_content(self, recognizer):
        """/deep_update with a message extracts the message correctly."""
        result = recognizer._quick_match("/deep_update 增加错误处理")
        assert result is not None
        assert result.primary_intent == IntentType.DEEP_UPDATE
        assert result.primary_data["message"] == "增加错误处理"

    def test_spec_command_with_requirement(self, recognizer):
        result = recognizer._quick_match("/spec 重构认证模块")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_SPEC
        assert result.primary_data["requirement"] == "重构认证模块"

    def test_heuristic_shell_single_char_word_not_matched(self, recognizer):
        """Single-char first word should NOT trigger command heuristic (2 <= len <= 15)."""
        result = recognizer._quick_match("x something")
        assert result is None

    def test_heuristic_shell_long_word_not_matched(self, recognizer):
        """First word > 15 chars should NOT trigger command heuristic."""
        result = recognizer._quick_match("abcdefghijklmnop arg")  # 16 chars
        assert result is None

    def test_info_commands_all_modes(self, recognizer):
        """All mode-specific _info commands should return correct intent."""
        info_map = {
            "/coco_info": IntentType.COCO_MESSAGE,
            "/claude_info": IntentType.CLAUDE_MESSAGE,
            "/aiden_info": IntentType.AIDEN_MESSAGE,
            "/codex_info": IntentType.CODEX_MESSAGE,
            "/gemini_info": IntentType.GEMINI_MESSAGE,
        }
        for cmd, expected_intent in info_map.items():
            result = recognizer._quick_match(cmd)
            assert result is not None, f"Expected match for {cmd}"
            assert result.primary_intent == expected_intent, f"Wrong intent for {cmd}"
            assert result.primary_data.get("command") == "info"

    def test_cd_with_tilde_path(self, recognizer):
        """cd ~/workspace should capture tilde path correctly."""
        result = recognizer._quick_match("cd ~/workspace")
        assert result is not None
        assert result.primary_intent == IntentType.CHANGE_DIR
        assert result.primary_data["path"] == "~/workspace"
