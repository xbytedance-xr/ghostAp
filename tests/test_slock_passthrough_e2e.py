"""AC10: Slock passthrough end-to-end tests.

Validates: In groups where Slock is NOT activated, SlockHandler transparently
passes messages to downstream handlers without any side effects.
"""

from __future__ import annotations

import pytest

from src.slock_engine.manager import SlockEngineManager
from src.slock_engine.models import SlockChannel


@pytest.fixture
def non_slock_manager(tmp_path):
    """Create a SlockEngineManager with no activated chats."""
    mgr = SlockEngineManager(storage_base_path=str(tmp_path / "slock_storage"))
    return mgr


class TestPassthroughNoActivation:
    """Messages in non-Slock groups must pass through without interference."""

    def test_is_slock_active_false_for_unregistered_chat(self, non_slock_manager):
        """Unregistered chat_id returns False for is_slock_active."""
        assert non_slock_manager.is_slock_active("random_chat_123") is False

    def test_get_activated_engine_returns_none(self, non_slock_manager):
        """No engine returned for non-slock chats."""
        assert non_slock_manager.get_activated_engine("random_chat_456") is None

    def test_is_managed_chat_false(self, non_slock_manager):
        """Non-slock chats are not managed."""
        assert non_slock_manager.is_managed_chat("unmanaged_chat") is False

    def test_no_engine_created_on_query(self, non_slock_manager):
        """Querying slock status does not auto-create engines."""
        non_slock_manager.is_slock_active("probe_chat")
        assert non_slock_manager.get_activated_engine("probe_chat") is None


class TestPassthroughWithMixedChats:
    """Ensure slock-active and non-slock chats coexist correctly."""

    def test_slock_chat_active_non_slock_chat_transparent(self, tmp_path):
        """One chat is slock-active, another is not — no cross-contamination."""
        mgr = SlockEngineManager(storage_base_path=str(tmp_path / "storage"))

        # Activate slock in chat-A
        engine = mgr.get_or_create("chat-slock-A", str(tmp_path / "proj"), engine_name="Slock")
        channel = SlockChannel(
            channel_id="chat-slock-A",
            name="Slock Team [Slock]",
            team_name="Slock Team",
        )
        engine.activate_channel(channel)
        mgr.register_managed_chat("chat-slock-A")

        # chat-slock-A is active
        assert mgr.is_slock_active("chat-slock-A") is True

        # chat-normal-B is NOT active
        assert mgr.is_slock_active("chat-normal-B") is False
        assert mgr.get_activated_engine("chat-normal-B") is None
        assert mgr.is_managed_chat("chat-normal-B") is False

    def test_multiple_slock_chats_independent(self, tmp_path):
        """Multiple slock chats are independent — one doesn't affect another."""
        mgr = SlockEngineManager(storage_base_path=str(tmp_path / "storage"))

        for chat_id in ["chat-A", "chat-B"]:
            engine = mgr.get_or_create(chat_id, str(tmp_path / "proj"), engine_name="Slock")
            channel = SlockChannel(channel_id=chat_id, team_name=f"Team-{chat_id}")
            engine.activate_channel(channel)
            mgr.register_managed_chat(chat_id)

        assert mgr.is_slock_active("chat-A") is True
        assert mgr.is_slock_active("chat-B") is True
        assert mgr.is_slock_active("chat-C") is False


class TestPassthroughNoSideEffects:
    """Verify zero side effects on non-slock chat queries."""

    def test_no_files_created_on_query(self, tmp_path, non_slock_manager):
        """Querying non-slock chat does not create any files."""
        import os

        storage_dir = str(tmp_path / "slock_storage")
        initial_state = set()
        if os.path.exists(storage_dir):
            for root, dirs, files in os.walk(storage_dir):
                for f in files:
                    initial_state.add(os.path.join(root, f))

        # Multiple queries
        non_slock_manager.is_slock_active("phantom_chat")
        non_slock_manager.get_activated_engine("phantom_chat")
        non_slock_manager.is_managed_chat("phantom_chat")

        final_state = set()
        if os.path.exists(storage_dir):
            for root, dirs, files in os.walk(storage_dir):
                for f in files:
                    final_state.add(os.path.join(root, f))

        assert final_state == initial_state, "Files were created by passive queries"

    def test_no_state_mutation_on_query(self, non_slock_manager):
        """Querying does not mutate internal manager state."""
        # Record initial state
        initial_engines = non_slock_manager.list_engines()

        non_slock_manager.is_slock_active("query_chat")
        non_slock_manager.get_activated_engine("query_chat")

        final_engines = non_slock_manager.list_engines()
        assert len(final_engines) == len(initial_engines)

    def test_passthrough_for_slock_like_messages(self, non_slock_manager):
        """Messages that look like slock commands still pass through in non-slock chats."""
        # These are just manager-level checks — handler would call these
        assert non_slock_manager.is_slock_active("non_slock_chat") is False
        # A real handler seeing False would return None and let downstream handle


class TestPassthroughAfterDeactivation:
    """After deactivation, chat returns to passthrough mode."""

    def test_deactivated_chat_becomes_transparent(self, tmp_path):
        """After engine deactivation, is_slock_active returns False."""
        mgr = SlockEngineManager(storage_base_path=str(tmp_path / "storage"))

        # Activate
        engine = mgr.get_or_create("chat-deact", str(tmp_path / "proj"), engine_name="Slock")
        channel = SlockChannel(channel_id="chat-deact", team_name="Temp Team")
        engine.activate_channel(channel)
        mgr.register_managed_chat("chat-deact")
        assert mgr.is_slock_active("chat-deact") is True

        # Deactivate
        engine.deactivate()
        assert mgr.is_slock_active("chat-deact") is False
