"""Tests for /new-chat intent recognition."""
import pytest

from src.agent.intent_recognizer import IntentRecognizer, IntentType


@pytest.fixture
def recognizer():
    return IntentRecognizer()


class TestNewChatIntent:
    def test_bare_new_chat(self, recognizer):
        result = recognizer.recognize("/new-chat")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {}

    def test_new_chat_with_name(self, recognizer):
        result = recognizer.recognize("/new-chat myproject")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {"name": "myproject"}

    def test_new_chat_with_name_and_suffix(self, recognizer):
        result = recognizer.recognize("/new-chat myproject staging")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {"name": "myproject", "suffix": "staging"}

    def test_new_chat_full_params(self, recognizer):
        result = recognizer.recognize("/new-chat myproject dev /home/user/code")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {"name": "myproject", "suffix": "dev", "path": "/home/user/code"}

    def test_new_chat_no_false_match(self, recognizer):
        """'/new-chatbot' should NOT match /new-chat."""
        result = recognizer.recognize("/new-chatbot")
        assert result.primary_intent != IntentType.NEW_CHAT_PROJECT

    def test_new_chat_case_insensitive(self, recognizer):
        result = recognizer.recognize("/New-Chat myproject")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
