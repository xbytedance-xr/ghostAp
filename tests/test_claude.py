import pytest
import sys
import time
import uuid
from unittest.mock import patch, MagicMock
from src.session.claude import ClaudeSession, ClaudeSessionManager


class TestClaudeSession:
    def test_create_session(self):
        session = ClaudeSession(chat_id="test_chat")
        assert session.chat_id == "test_chat"
        # Session ID must be a valid UUID
        parsed = uuid.UUID(session.session_id)
        assert str(parsed) == session.session_id
        assert session.message_count == 0
        assert session.is_resumed is False

    def test_create_session_with_custom_id(self):
        session = ClaudeSession(chat_id="test_chat", session_id="custom_session_id")
        assert session.session_id == "custom_session_id"

    def test_session_id_is_unique(self):
        session1 = ClaudeSession(chat_id="test_chat")
        session2 = ClaudeSession(chat_id="test_chat")
        assert session1.session_id != session2.session_id

    def test_session_to_snapshot(self):
        session = ClaudeSession(chat_id="test_chat")
        session.message_count = 5
        session.last_query = "test query"
        
        snapshot = session.to_snapshot()
        
        assert snapshot["chat_id"] == "test_chat"
        assert snapshot["message_count"] == 5
        assert snapshot["last_query"] == "test query"
        assert "session_id" in snapshot
        assert "created_at" in snapshot

    def test_session_from_snapshot(self):
        snapshot = {
            "chat_id": "test_chat",
            "session_id": "test_session",
            "message_count": 10,
            "last_query": "hello",
            "is_resumed": True,
        }
        
        session = ClaudeSession.from_snapshot(snapshot)
        
        assert session.chat_id == "test_chat"
        assert session.session_id == "test_session"
        assert session.message_count == 10
        assert session.last_query == "hello"
        assert session.is_resumed is True

    def test_clean_output(self):
        session = ClaudeSession(chat_id="test_chat")
        
        dirty_output = "\x1b[32mHello\x1b[0m World\x1b]0;title\x07"
        clean = session._clean_output(dirty_output)
        
        assert "Hello" in clean
        assert "World" in clean
        assert "\x1b" not in clean

    def test_streaming_reads_stderr_without_deadlock(self):
        """大量 stderr 输出不应导致流式读取卡死/超时。"""
        session = ClaudeSession(chat_id="test_chat")

        # Write a lot to stderr first; old implementation could deadlock and time out.
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x'*200000); sys.stderr.flush(); print('done')",
        ]

        chunks = []
        out, err, timed_out = session._run_streaming_process(
            cmd,
            cwd=None,
            timeout=3,
            on_chunk=lambda s: chunks.append(s),
            chunk_interval=0.01,
        )

        assert timed_out is False
        assert "done" in out
        assert len(err) > 100000


class TestClaudeSessionManager:
    def test_start_session(self):
        manager = ClaudeSessionManager()
        session = manager.start_session("chat_1")
        
        assert session is not None
        assert session.chat_id == "chat_1"
        assert manager.is_in_claude_mode("chat_1") is True

    def test_start_session_with_custom_id(self):
        manager = ClaudeSessionManager()
        session = manager.start_session("chat_1", session_id="custom_id")
        
        assert session.session_id == "custom_id"

    def test_get_session(self):
        manager = ClaudeSessionManager()
        manager.start_session("chat_1")
        
        session = manager.get_session("chat_1")
        assert session is not None
        assert session.chat_id == "chat_1"

    def test_get_session_nonexistent(self):
        manager = ClaudeSessionManager()
        session = manager.get_session("nonexistent")
        assert session is None

    def test_end_session(self):
        manager = ClaudeSessionManager()
        manager.start_session("chat_1")
        
        snapshot = manager.end_session("chat_1")
        
        assert snapshot is not None
        assert snapshot["chat_id"] == "chat_1"
        assert manager.is_in_claude_mode("chat_1") is False

    def test_end_session_nonexistent(self):
        manager = ClaudeSessionManager()
        snapshot = manager.end_session("nonexistent")
        assert snapshot is None

    def test_is_in_claude_mode(self):
        manager = ClaudeSessionManager()
        
        assert manager.is_in_claude_mode("chat_1") is False
        
        manager.start_session("chat_1")
        assert manager.is_in_claude_mode("chat_1") is True
        
        manager.end_session("chat_1")
        assert manager.is_in_claude_mode("chat_1") is False

    def test_resume_session(self):
        manager = ClaudeSessionManager()
        session = manager.resume_session("chat_1", "old_session_id")
        
        assert session.session_id == "old_session_id"
        assert session.is_resumed is True

    def test_get_session_info(self):
        manager = ClaudeSessionManager()
        manager.start_session("chat_1")
        
        info = manager.get_session_info("chat_1")
        
        assert info is not None
        assert "Claude 会话信息" in info
        assert "会话ID" in info
        assert "消息数" in info

    def test_get_session_info_nonexistent(self):
        manager = ClaudeSessionManager()
        info = manager.get_session_info("nonexistent")
        assert info is None


class TestModeManagerClaude:
    def test_enter_claude_mode(self):
        from src.mode import ModeManager, InteractionMode
        
        manager = ModeManager()
        old_mode = manager.enter_claude_mode("chat_1")
        
        assert old_mode == InteractionMode.SMART
        assert manager.is_claude_mode("chat_1") is True
        assert manager.is_coco_mode("chat_1") is False
        assert manager.is_smart_mode("chat_1") is False

    def test_is_programming_mode(self):
        from src.mode import ModeManager, InteractionMode
        
        manager = ModeManager()
        
        assert manager.is_programming_mode("chat_1") is False
        
        manager.enter_coco_mode("chat_1")
        assert manager.is_programming_mode("chat_1") is True
        
        manager.exit_to_smart("chat_1")
        manager.enter_claude_mode("chat_1")
        assert manager.is_programming_mode("chat_1") is True
        
        manager.exit_to_smart("chat_1")
        assert manager.is_programming_mode("chat_1") is False

    def test_mode_switch_between_coco_and_claude(self):
        from src.mode import ModeManager, InteractionMode
        
        manager = ModeManager()
        
        manager.enter_coco_mode("chat_1")
        assert manager.get_mode("chat_1") == InteractionMode.COCO
        
        manager.enter_claude_mode("chat_1")
        assert manager.get_mode("chat_1") == InteractionMode.CLAUDE
        
        manager.exit_to_smart("chat_1")
        assert manager.get_mode("chat_1") == InteractionMode.SMART


class TestIntentRecognizerClaude:
    def test_claude_command(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        result = recognizer._quick_match("/claude")
        
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_CLAUDE

    def test_exit_claude_command(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        result = recognizer._quick_match("/exit_claude")
        
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_CLAUDE

    def test_claude_info_command(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        result = recognizer._quick_match("/claude_info")
        
        assert result is not None
        assert result.primary_intent == IntentType.CLAUDE_MESSAGE
        assert result.primary_data.get("command") == "info"

    def test_help_command(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        result = recognizer._quick_match("/help")
        
        assert result is not None
        assert result.primary_intent == IntentType.SHOW_HELP

    def test_help_command_chinese(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        result = recognizer._quick_match("/帮助")
        
        assert result is not None
        assert result.primary_intent == IntentType.SHOW_HELP

    def test_enter_claude_keywords(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        
        result = recognizer._quick_match("进入claude模式")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_CLAUDE
        
        result = recognizer._quick_match("使用claude")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_CLAUDE

    def test_claude_typo_correction(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        
        recognizer = IntentRecognizer()
        
        result = recognizer._quick_match("/calude")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_CLAUDE
        assert "纠正拼写" in result.reasoning
        
        result = recognizer._quick_match("/cluade")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_CLAUDE

    def test_other_typo_corrections(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType

        recognizer = IntentRecognizer()

        result = recognizer._quick_match("/cooc")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_COCO

        result = recognizer._quick_match("/exti")
        assert result is not None
        assert result.primary_intent == IntentType.EXIT_MODE

        result = recognizer._quick_match("/hlep")
        assert result is not None
        assert result.primary_intent == IntentType.SHOW_HELP


class TestClaudeProjectContext:
    def test_claude_fields_default(self):
        from src.project.context import ProjectContext

        ctx = ProjectContext(
            project_id="test",
            project_name="Test Project",
            root_path="/tmp/test"
        )
        assert ctx.claude_mode is False
        assert ctx.claude_session_snapshot is None

    def test_set_claude_mode(self):
        from src.project.context import ProjectContext

        ctx = ProjectContext(
            project_id="test",
            project_name="Test Project",
            root_path="/tmp/test"
        )
        ctx.set_claude_mode(True, "uuid-session-id", 0)

        assert ctx.claude_mode is True
        assert ctx.claude_session_snapshot is not None
        assert ctx.claude_session_snapshot.session_id == "uuid-session-id"
        assert ctx.claude_session_snapshot.is_resumable is True

    def test_set_claude_mode_disable(self):
        from src.project.context import ProjectContext

        ctx = ProjectContext(
            project_id="test",
            project_name="Test Project",
            root_path="/tmp/test"
        )
        ctx.set_claude_mode(True, "uuid-session-id", 0)
        ctx.set_claude_mode(False)

        assert ctx.claude_mode is False
        assert ctx.claude_session_snapshot is not None
        assert ctx.claude_session_snapshot.is_resumable is True

    def test_update_claude_snapshot(self):
        from src.project.context import ProjectContext

        ctx = ProjectContext(
            project_id="test",
            project_name="Test Project",
            root_path="/tmp/test"
        )
        ctx.set_claude_mode(True, "uuid-session-id", 0)
        ctx.update_claude_snapshot("test query", 5)

        assert ctx.claude_session_snapshot.last_query == "test query"
        assert ctx.claude_session_snapshot.query_count == 5

    def test_claude_snapshot_serialization(self):
        from src.project.context import ProjectContext

        ctx = ProjectContext(
            project_id="test",
            project_name="Test Project",
            root_path="/tmp/test"
        )
        ctx.set_claude_mode(True, "uuid-session-id", 3)
        ctx.update_claude_snapshot("last query", 3)

        snapshot = ctx.to_snapshot()
        assert snapshot["claude_mode"] is True
        assert snapshot["claude_session_snapshot"]["session_id"] == "uuid-session-id"
        assert snapshot["claude_session_snapshot"]["query_count"] == 3

        restored = ProjectContext.from_snapshot(snapshot)
        assert restored.claude_mode is True
        assert restored.claude_session_snapshot.session_id == "uuid-session-id"
        assert restored.claude_session_snapshot.query_count == 3
        assert restored.claude_session_snapshot.last_query == "last query"

    def test_backward_compatible_snapshot(self):
        """Old snapshots without claude fields should still load."""
        from src.project.context import ProjectContext

        old_snapshot = {
            "project_id": "test",
            "project_name": "Test Project",
            "root_path": "/tmp/test",
            "status": "idle",
            "coco_mode": False,
            "theme_color": "green",
            "emoji_prefix": "🟢",
            "env_vars": {},
        }

        ctx = ProjectContext.from_snapshot(old_snapshot)
        assert ctx.claude_mode is False
        assert ctx.claude_session_snapshot is None


class TestCardButtonsClaude:
    def test_claude_mode_buttons(self):
        from src.card.builder import CardBuilder

        buttons = CardBuilder._build_footer_buttons(None, is_coco_mode=False, is_claude_mode=True)
        assert len(buttons) == 2
        assert "退出Claude" in buttons[0]["text"]["content"]
        assert buttons[0]["behaviors"][0]["value"]["action"] == "exit_claude"
        assert "切换项目" in buttons[1]["text"]["content"]

    def test_coco_mode_buttons(self):
        from src.card.builder import CardBuilder

        buttons = CardBuilder._build_footer_buttons(None, is_coco_mode=True, is_claude_mode=False)
        assert len(buttons) == 2
        assert "退出Coco" in buttons[0]["text"]["content"]
        assert buttons[0]["behaviors"][0]["value"]["action"] == "exit_coco"

    def test_smart_mode_buttons_have_both(self):
        from src.card.builder import CardBuilder

        buttons = CardBuilder._build_footer_buttons(None, is_coco_mode=False, is_claude_mode=False)
        assert len(buttons) == 2
        assert "Coco" in buttons[0]["text"]["content"]
        assert buttons[0]["behaviors"][0]["value"]["action"] == "enter_coco"
        assert "Claude" in buttons[1]["text"]["content"]
        assert buttons[1]["behaviors"][0]["value"]["action"] == "enter_claude"

    def test_header_title_claude_mode(self):
        from src.card.builder import CardBuilder

        title = CardBuilder._build_header_title(None, is_claude_mode=True)
        assert "🔮" in title
        assert "Claude" in title

    def test_header_title_coco_mode(self):
        from src.card.builder import CardBuilder

        title = CardBuilder._build_header_title(None, is_coco_mode=True)
        assert "🤖" in title

    def test_header_title_smart_mode(self):
        from src.card.builder import CardBuilder

        title = CardBuilder._build_header_title(None)
        assert "🧠" in title


class TestCommandInterceptionInProgrammingMode:
    """测试编程模式（Coco/Claude）中系统命令的拦截。

    在 Coco/Claude 模式中，/help, /帮助, /projects 等系统命令
    不应被发送给 AI，而应由系统直接处理。
    """

    def _create_mock_client(self):
        """创建带有完整 mock 的 FeishuWSClient 实例。"""
        from unittest.mock import patch, MagicMock

        patches = {
            'settings': patch('src.feishu.ws_client.get_settings'),
            'coco': patch('src.feishu.ws_client.CocoSessionManager'),
            'claude': patch('src.feishu.ws_client.ClaudeSessionManager'),
            'intent': patch('src.feishu.ws_client.IntentRecognizer'),
            'project': patch('src.feishu.ws_client.ProjectManager'),
            'mapper': patch('src.feishu.ws_client.MessageProjectMapper'),
            'deep': patch('src.feishu.ws_client.DeepEngineManager'),
            'reporter': patch('src.feishu.ws_client.ProgressReporter'),
            'mode': patch('src.mode.ModeManager'),
        }

        mocks = {}
        for name, p in patches.items():
            mocks[name] = p.start()

        mock_settings = MagicMock()
        mock_settings.app_id = "test"
        mock_settings.app_secret = "test"
        mock_settings.streaming_enabled = False
        mock_settings.task_scheduler_max_concurrent = 2
        mock_settings.task_scheduler_per_key_concurrency = 1
        mocks['settings'].return_value = mock_settings

        from src.feishu.ws_client import FeishuWSClient
        client = FeishuWSClient(MagicMock())
        client._scheduler = MagicMock()

        return client, patches

    def _stop_patches(self, patches):
        for p in patches.values():
            p.stop()

    def test_is_interceptable_command_help(self):
        client, patches = self._create_mock_client()
        try:
            assert client._is_interceptable_command("/help") is True
            assert client._is_interceptable_command("/帮助") is True
            assert client._is_interceptable_command("/Help") is True
        finally:
            self._stop_patches(patches)

    def test_is_interceptable_command_info(self):
        client, patches = self._create_mock_client()
        try:
            assert client._is_interceptable_command("/coco_info") is True
            assert client._is_interceptable_command("/claude_info") is True
        finally:
            self._stop_patches(patches)

    def test_is_interceptable_command_project(self):
        client, patches = self._create_mock_client()
        try:
            assert client._is_interceptable_command("/projects") is True
            assert client._is_interceptable_command("/status") is True
            assert client._is_interceptable_command("/switch myproject") is True
            assert client._is_interceptable_command("/new myproject /tmp") is True
        finally:
            self._stop_patches(patches)

    def test_is_interceptable_command_false_for_regular_text(self):
        client, patches = self._create_mock_client()
        try:
            assert client._is_interceptable_command("hello") is False
            assert client._is_interceptable_command("帮我写个函数") is False
            assert client._is_interceptable_command("/deep do something") is False
            assert client._is_interceptable_command("/exit") is False
        finally:
            self._stop_patches(patches)

    def test_is_interceptable_command_false_for_exit(self):
        """退出命令不应被当作拦截命令，它有专门的处理路径。"""
        client, patches = self._create_mock_client()
        try:
            assert client._is_interceptable_command("/exit") is False
            assert client._is_interceptable_command("/quit") is False
            assert client._is_interceptable_command("/exit_claude") is False
        finally:
            self._stop_patches(patches)

    def test_handle_intercepted_command_help(self):
        from unittest.mock import MagicMock
        client, patches = self._create_mock_client()
        try:
            client._system_handler.show_full_help = MagicMock()
            client._handle_intercepted_command("msg1", "chat1", "/帮助", None)
            client._system_handler.show_full_help.assert_called_once_with("msg1", "chat1", None)
        finally:
            self._stop_patches(patches)

    def test_handle_intercepted_command_claude_info(self):
        from unittest.mock import MagicMock
        client, patches = self._create_mock_client()
        try:
            client._claude_handler.show_info = MagicMock()
            client._handle_intercepted_command("msg1", "chat1", "/claude_info", None)
            client._claude_handler.show_info.assert_called_once_with("msg1", "chat1", None)
        finally:
            self._stop_patches(patches)

    def test_handle_intercepted_command_projects(self):
        from unittest.mock import MagicMock
        client, patches = self._create_mock_client()
        try:
            client._project_handler.show_project_board = MagicMock()
            client._handle_intercepted_command("msg1", "chat1", "/projects", None)
            client._project_handler.show_project_board.assert_called_once_with("msg1", "chat1")
        finally:
            self._stop_patches(patches)

    def test_handle_intercepted_command_switch(self):
        from unittest.mock import MagicMock
        client, patches = self._create_mock_client()
        try:
            client._project_handler.switch_project = MagicMock()
            client._handle_intercepted_command("msg1", "chat1", "/switch myproject", None)
            client._project_handler.switch_project.assert_called_once_with(
                "msg1", "chat1", "myproject",
                coco_handler=client._coco_handler,
                claude_handler=client._claude_handler,
            )
        finally:
            self._stop_patches(patches)
