import unittest
from unittest.mock import MagicMock, patch
import json
from src.card.builders.system import SystemBuilder
from src.feishu.handlers.system import SystemHandler
from src.feishu.ws_client import FeishuWSClient

class TestRefactorRobustness(unittest.TestCase):

    def test_shell_output_truncation(self):
        """Test that long shell output is truncated in the card."""
        from src.sandbox.executor import ExecutionResult
        
        # Create a long output string (> 2000 chars)
        long_output = "a" * 3000
        result = ExecutionResult(
            return_code=0,
            stdout=long_output,
            stderr="",
            success=True
        )
        
        msg_type, content_json = SystemBuilder.build_shell_result_card("echo long", result)
        content = json.loads(content_json)
        
        # Find the code block with output
        found_output = False
        for element in content["body"]["elements"]:
            if element["tag"] == "markdown" and "```BASH" in element["content"]:
                text = element["content"]
                if "truncated" in text:
                    found_output = True
                    # Check that it's actually shorter than the original
                    self.assertLess(len(text), 3000)
                    # Check for truncation marker
                    self.assertIn("...(truncated)...", text)
        
        self.assertTrue(found_output, "Did not find truncated output in card")

    def test_system_handler_dispatch(self):
        """Test that SystemHandler correctly dispatches commands using the new registry."""
        mock_ctx = MagicMock()
        handler = SystemHandler(mock_ctx)
        
        # Mock handlers
        handler.coco_handler = MagicMock()
        handler.project_handler = MagicMock()
        handler.diagnostics_handler = MagicMock()
        
        # Test exact match
        handler.handle_intercepted_command("mid", "cid", "/coco_info")
        handler.coco_handler.show_info.assert_called_with("mid", "cid", None)
        
        # Test prefix match
        handler.handle_intercepted_command("mid", "cid", "/status detail")
        handler.diagnostics_handler.show_unified_status.assert_called_with("mid", "cid", "/status detail", None)
        
        # Test fallback
        with patch.object(handler, 'show_full_help') as mock_help:
            handler.handle_intercepted_command("mid", "cid", "/unknown_cmd")
            mock_help.assert_called_with("mid", "cid", None)

    def test_ws_client_refactor_structure(self):
        """Test that the refactored WSClient methods exist and validate basic message."""
        # We can't easily instantiate WSClient fully without many mocks, 
        # but we can test the specific logic by partial mocking or just checking attributes if we could.
        # Instead, let's use the ExitStack approach from before to instantiate it safely.
        from contextlib import ExitStack
        
        with ExitStack() as stack:
            stack.enter_context(patch('src.feishu.ws_client.get_settings'))
            stack.enter_context(patch('src.feishu.ws_client.ACPSessionManager'))
            stack.enter_context(patch('src.feishu.ws_client.IntentRecognizer'))
            stack.enter_context(patch('src.feishu.ws_client.TaskScheduler'))
            stack.enter_context(patch('src.feishu.ws_client.ProjectManager'))
            stack.enter_context(patch('src.feishu.ws_client.MessageProjectMapper'))
            stack.enter_context(patch('src.feishu.ws_client.MessageLinker'))
            stack.enter_context(patch('src.mode.ModeManager'))
            stack.enter_context(patch('src.feishu.ws_client.ProjectContextManager'))
            stack.enter_context(patch('src.feishu.ws_client.DeepEngineManager'))
            stack.enter_context(patch('src.feishu.ws_client.LoopEngineManager'))
            stack.enter_context(patch('src.feishu.ws_client.SpecEngineManager'))
            stack.enter_context(patch('src.feishu.ws_client.HandlerContext'))
            stack.enter_context(patch('src.feishu.ws_client.ActionDispatcher'))
            stack.enter_context(patch('src.feishu.ws_client.CocoModeHandler'))
            stack.enter_context(patch('src.feishu.ws_client.ClaudeModeHandler'))
            stack.enter_context(patch('src.feishu.ws_client.TTADKModeHandler'))
            stack.enter_context(patch('src.feishu.ws_client.DeepHandler'))
            stack.enter_context(patch('src.feishu.ws_client.LoopHandler'))
            stack.enter_context(patch('src.feishu.ws_client.SpecHandler'))
            stack.enter_context(patch('src.feishu.ws_client.ProjectHandler'))
            stack.enter_context(patch('src.feishu.ws_client.SystemHandler'))
            stack.enter_context(patch('src.feishu.ws_client.DiagnosticsHandler'))

            client = FeishuWSClient(lambda *args: None)
            
            # Test _clean_at_text
            self.assertEqual(client._clean_at_text("hello"), "hello")
            self.assertEqual(client._clean_at_text("@bot hello"), "hello")
            self.assertEqual(client._clean_at_text("  @bot   hello  "), "hello")
            self.assertEqual(client._clean_at_text("@bot"), "")
            
            # Test _validate_message
            mock_msg = MagicMock()
            mock_msg.create_time = None
            mock_msg.message_id = "mid"
            mock_msg.message_type = "text"
            
            # Mock dependencies
            client._is_message_expired = MagicMock(return_value=False)
            client._is_duplicate_message = MagicMock(return_value=False)
            
            self.assertTrue(client._validate_message(mock_msg, "req_id"))
            
            # Test invalid type
            mock_msg.message_type = "audio"
            client._reply_message = MagicMock()
            self.assertFalse(client._validate_message(mock_msg, "req_id"))
            client._reply_message.assert_called()

if __name__ == '__main__':
    unittest.main()
