import pytest
from unittest.mock import MagicMock
from src.feishu.action_dispatcher import ActionDispatcher

class TestActionDispatcher:
    def test_register_and_dispatch_exact(self):
        dispatcher = ActionDispatcher()
        handler = MagicMock()
        
        dispatcher.register(handler, exact="test_action")
        
        # Test successful dispatch
        value = {"foo": "bar"}
        result = dispatcher.dispatch("test_action", "msg1", "chat1", "proj1", value)
        
        assert result is True
        handler.assert_called_once_with("msg1", "chat1", "proj1", value)
        
        # Test unsuccessful dispatch (wrong action_type)
        result_fail = dispatcher.dispatch("wrong_action", "msg1", "chat1", "proj1", value)
        assert result_fail is False

    def test_register_and_dispatch_prefix(self):
        dispatcher = ActionDispatcher()
        handler = MagicMock()
        
        dispatcher.register(handler, prefix="prefix_")
        
        # Test successful dispatch
        value = {"foo": "bar"}
        result = dispatcher.dispatch("prefix_test", "msg1", "chat1", "proj1", value)
        
        assert result is True
        handler.assert_called_once_with("msg1", "chat1", "proj1", value, type="prefix_test")

    def test_dispatch_order_exact_first(self):
        dispatcher = ActionDispatcher()
        exact_handler = MagicMock()
        prefix_handler = MagicMock()
        
        dispatcher.register(exact_handler, exact="action_test")
        dispatcher.register(prefix_handler, prefix="action_")
        
        result = dispatcher.dispatch("action_test", "msg1", "chat1", "proj1", {})
        
        assert result is True
        exact_handler.assert_called_once()
        prefix_handler.assert_not_called()

    def test_handler_signature_mismatch_raises(self):
        dispatcher = ActionDispatcher()
        
        # Handler with wrong signature (too few arguments)
        def bad_handler(mid, cid):
            pass
            
        dispatcher.register(bad_handler, exact="bad_action")
        
        with pytest.raises(TypeError):
            dispatcher.dispatch("bad_action", "m", "c", "p", {})

    def test_prefix_handler_signature_mismatch_raises(self):
        dispatcher = ActionDispatcher()
        
        # Prefix handler missing 'type' kwarg or having wrong signature
        def bad_prefix_handler(mid, cid, pid, val):
            pass
            
        dispatcher.register(bad_prefix_handler, prefix="bad_")
        
        with pytest.raises(TypeError):
            dispatcher.dispatch("bad_test", "m", "c", "p", {})
