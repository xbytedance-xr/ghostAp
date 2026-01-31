import pytest
import time
import uuid
from unittest.mock import patch, MagicMock
from src.claude.session import ClaudeSession, ClaudeSessionManager


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
