import time
import unittest
from unittest.mock import MagicMock, patch
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from src.feishu.ws_client import FeishuWSClient


class TestCardDeduplication(unittest.TestCase):
    def setUp(self):
        self.mock_callback = MagicMock()
        self.client = FeishuWSClient(self.mock_callback)
        # Mock internal components to avoid side effects
        self.client._project_manager = MagicMock()
        self.client._message_linker = MagicMock()
        self.client._scheduler = MagicMock()
        self.client._ensure_request_id = MagicMock(return_value="req_123")

    def tearDown(self):
        self.client.close()

    def _create_mock_event(self, event_id: str):
        mock_event = MagicMock(spec=P2CardActionTrigger)
        mock_event.header = MagicMock()
        mock_event.header.event_id = event_id
        mock_event.header.event_type = "card.action.trigger"
        
        mock_event.event = MagicMock()
        mock_event.event.action = MagicMock()
        mock_event.event.action.tag = "button"
        mock_event.event.action.value = {"action": "test_action"}
        mock_event.event.context = MagicMock()
        mock_event.event.context.open_message_id = "msg_123"
        mock_event.event.context.open_chat_id = "chat_123"
        
        return mock_event

    def test_deduplication(self):
        event_id = "evt_unique_1"
        event1 = self._create_mock_event(event_id)
        
        # First call: should be processed
        self.client._handle_card_action(event1)
        # Verify scheduler submit was called
        self.assertEqual(self.client._scheduler.submit.call_count, 1)
        
        # Reset mock
        self.client._scheduler.submit.reset_mock()
        
        # Second call with same event_id: should be skipped
        event2 = self._create_mock_event(event_id)
        self.client._handle_card_action(event2)
        # Verify scheduler submit was NOT called
        self.assertEqual(self.client._scheduler.submit.call_count, 0)
        
        # Third call with different event_id: should be processed
        event3 = self._create_mock_event("evt_unique_2")
        self.client._handle_card_action(event3)
        # Verify scheduler submit was called again
        self.assertEqual(self.client._scheduler.submit.call_count, 1)

    def test_cache_cleanup(self):
        # Override ttl and cleanup interval for fast testing
        self.client._card_event_cache._ttl = 0.1
        self.client._card_event_cache._cleanup_interval = 0.1
        
        event_id = "evt_expire_1"
        event1 = self._create_mock_event(event_id)
        
        # First call
        self.client._handle_card_action(event1)
        self.assertEqual(self.client._scheduler.submit.call_count, 1)
        self.client._scheduler.submit.reset_mock()
        
        # Immediate retry: duplicate
        self.client._handle_card_action(event1)
        self.assertEqual(self.client._scheduler.submit.call_count, 0)
        
        # Wait for expiration
        time.sleep(0.2)
        
        # Retry after expiration: should be processed again (cache cleared logic dependent)
        # Note: MessageCache checks timestamp on is_duplicate call
        self.client._handle_card_action(event1)
        self.assertEqual(self.client._scheduler.submit.call_count, 1)
