"""Tests for authorization whitelist (security hardening A2).

Covers:
- allowed_chat_ids non-empty: non-whitelisted chat is dropped.
- allowed_user_ids non-empty: non-whitelisted user is dropped.
- Both empty: all messages pass through.
- Config parsing: comma-separated string -> frozenset.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import Settings


# ==================================================================
# Config parsing tests
# ==================================================================


class TestWhitelistConfigParsing:
    """Test allowed_chat_ids / allowed_user_ids coerce from string to frozenset."""

    def test_empty_string_yields_empty_frozenset(self):
        s = Settings(allowed_chat_ids="", allowed_user_ids="")
        assert s.allowed_chat_ids == frozenset()
        assert s.allowed_user_ids == frozenset()

    def test_single_value(self):
        s = Settings(allowed_chat_ids="chat_abc", allowed_user_ids="user_xyz")
        assert s.allowed_chat_ids == frozenset({"chat_abc"})
        assert s.allowed_user_ids == frozenset({"user_xyz"})

    def test_comma_separated_values(self):
        s = Settings(
            allowed_chat_ids="chat_1, chat_2,chat_3",
            allowed_user_ids="u1,u2, u3 ",
        )
        assert s.allowed_chat_ids == frozenset({"chat_1", "chat_2", "chat_3"})
        assert s.allowed_user_ids == frozenset({"u1", "u2", "u3"})

    def test_list_input_normalized(self):
        s = Settings(
            allowed_chat_ids=["c1", "c2"],  # type: ignore[arg-type]
            allowed_user_ids=frozenset({"u1"}),  # type: ignore[arg-type]
        )
        assert s.allowed_chat_ids == frozenset({"c1", "c2"})
        assert s.allowed_user_ids == frozenset({"u1"})

    def test_whitespace_only_yields_empty(self):
        s = Settings(allowed_chat_ids="  ,  , ", allowed_user_ids=" ")
        assert s.allowed_chat_ids == frozenset()
        assert s.allowed_user_ids == frozenset()


# ==================================================================
# Whitelist enforcement tests (ws_client._process_message_async)
# ==================================================================


def _make_fake_data(chat_id: str = "chat_ok", sender_id: str = "user_ok"):
    """Build a minimal mock P2ImMessageReceiveV1 for _process_message_async."""
    data = MagicMock()
    data.event.message.message_id = "msg_001"
    data.event.message.chat_id = chat_id
    data.event.message.chat_type = "group"
    data.event.message.create_time = "9999999999999"
    data.event.message.message_type = "text"
    data.event.message.content = '{"text": "hello"}'
    data.event.message.parent_id = None
    data.event.message.root_id = None
    data.event.sender.sender_id.open_id = sender_id
    return data


@pytest.fixture
def _patch_settings():
    """Provide a helper to patch get_settings with custom whitelist values."""

    def _factory(allowed_chat_ids: str = "", allowed_user_ids: str = ""):
        s = Settings(
            allowed_chat_ids=allowed_chat_ids,
            allowed_user_ids=allowed_user_ids,
        )
        return s

    return _factory


class TestWhitelistEnforcement:
    """Integration-level tests: messages are dropped or passed through based on whitelist."""

    @patch("src.feishu.ws_client.FeishuWSClient.__init__", return_value=None)
    def _build_client(self, mock_init):
        from src.feishu.ws_client import FeishuWSClient

        client = FeishuWSClient.__new__(FeishuWSClient)
        return client

    def _setup_client(self, settings):
        """Set up a minimal client with mocked internals for testing _process_message_async."""
        client = self._build_client()
        client.settings = settings
        client._message_ingress_guard = MagicMock()
        client._message_ingress_guard.is_message_expired.return_value = False
        client._message_ingress_guard.is_duplicate_message.return_value = False
        client._message_cache = MagicMock()
        client._message_cache.is_duplicate.return_value = False
        client._get_image_handler = MagicMock()
        parse_result = MagicMock()
        parse_result.text = "hello"
        parse_result.image_keys = []
        client._get_image_handler.return_value.parse_message.return_value = parse_result
        client._chat_lock_gate = MagicMock()
        client._chat_lock_gate.check.return_value = False
        client._pending_image_lock = MagicMock()
        client._pending_image_lock.__enter__ = MagicMock(return_value=None)
        client._pending_image_lock.__exit__ = MagicMock(return_value=False)
        client._pending_image_keys = {}
        client._pending_image_only = set()
        client._thread_manager = MagicMock()
        client._thread_manager.get.return_value = None
        client._project_manager = MagicMock()
        client._project_manager.find_by_bound_chat_id.return_value = None
        client._project_manager.get_active_project.return_value = None
        client._message_mapper = MagicMock()
        client._message_mapper.get_project_id.return_value = None
        client._scheduler = MagicMock()
        client._mode_manager = MagicMock()
        client._image_handler = None
        client._enable_streaming = False
        # Router-bound methods (normally attached by bind_forwarding_methods)
        client._ensure_request_id = MagicMock(return_value="req_test_001")
        client._get_api_client = MagicMock()
        # Mock _dispatch_message_logic to track whether it was called
        client._dispatch_message_logic = MagicMock()
        client._show_help = MagicMock()
        client._reply_text = MagicMock()
        client._dispatch_empty_text = MagicMock()
        return client

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_chat_not_in_whitelist_dropped(self, _mock_resolve, _patch_settings):
        """Message from non-whitelisted chat is silently dropped."""
        settings = _patch_settings(allowed_chat_ids="chat_allowed")
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="chat_blocked", sender_id="user_ok")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_not_called()

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_user_not_in_whitelist_dropped(self, _mock_resolve, _patch_settings):
        """Message from non-whitelisted user is silently dropped."""
        settings = _patch_settings(allowed_user_ids="user_allowed")
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="chat_ok", sender_id="user_blocked")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_not_called()

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_chat_in_whitelist_passes(self, _mock_resolve, _patch_settings):
        """Message from whitelisted chat passes through."""
        settings = _patch_settings(allowed_chat_ids="chat_ok,chat_other")
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="chat_ok", sender_id="user_ok")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_called_once()

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_user_in_whitelist_passes(self, _mock_resolve, _patch_settings):
        """Message from whitelisted user passes through."""
        settings = _patch_settings(allowed_user_ids="user_ok")
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="chat_ok", sender_id="user_ok")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_called_once()

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_both_empty_allows_all(self, _mock_resolve, _patch_settings):
        """When both whitelists are empty, all messages pass through."""
        settings = _patch_settings(allowed_chat_ids="", allowed_user_ids="")
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="any_chat", sender_id="any_user")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_called_once()

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_both_whitelists_enforced(self, _mock_resolve, _patch_settings):
        """Both whitelists are checked: chat passes but user fails -> dropped."""
        settings = _patch_settings(
            allowed_chat_ids="chat_ok",
            allowed_user_ids="user_allowed",
        )
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="chat_ok", sender_id="user_blocked")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_not_called()

    @patch("src.feishu.user_cache.resolve_display_name", return_value="TestUser")
    def test_both_whitelists_pass(self, _mock_resolve, _patch_settings):
        """Both whitelists pass: message goes through."""
        settings = _patch_settings(
            allowed_chat_ids="chat_ok",
            allowed_user_ids="user_ok",
        )
        client = self._setup_client(settings)
        data = _make_fake_data(chat_id="chat_ok", sender_id="user_ok")

        client._process_message_async(data)

        client._dispatch_message_logic.assert_called_once()
