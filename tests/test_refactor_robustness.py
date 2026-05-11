import json
import unittest
from unittest.mock import MagicMock, patch

from src.card.builders.system import SystemBuilder
from src.feishu.handlers.system import SystemHandler
from src.feishu.ws_client import FeishuWSClient
from src.feishu.slash_command_parser import SlashCommandParser


class TestRefactorRobustness(unittest.TestCase):
    def test_shell_output_truncation(self):
        """Test that long shell output is truncated in the card."""
        from src.sandbox.executor import ExecutionResult

        # Create a long output string (> SHELL_STDOUT_MAX=16000 chars)
        long_output = "a" * 20000
        result = ExecutionResult(return_code=0, stdout=long_output, stderr="", success=True)

        msg_type, content_json = SystemBuilder.build_shell_result_card("echo long", result)
        content = json.loads(content_json)

        # Find the code block with output
        found_output = False
        for element in content["body"]["elements"]:
            if element["tag"] == "markdown" and "```BASH" in element["content"]:
                text = element["content"]
                if "\u5df2\u622a\u65ad" in text:  # 已截断
                    found_output = True
                    # Check that it's actually shorter than the original
                    self.assertLess(len(text), 20000)
                    # Check for truncation marker
                    self.assertIn("已截断", text)

        self.assertTrue(found_output, "Did not find truncated output in card")

    def test_payload_truncation(self):
        """Test smart truncation of large payloads."""
        from src.feishu.renderers.base import BaseRenderer

        # Create a mock handler for BaseRenderer
        mock_handler = MagicMock()
        mock_handler.ctx = MagicMock()
        mock_handler.settings = MagicMock()
        mock_handler.settings.card.deep_compact_default = False

        renderer = BaseRenderer(mock_handler)

        # Case 1: Small payload, no truncation
        small_payload = json.dumps({"header": "test", "content": "short"})
        self.assertEqual(renderer._check_and_truncate_payload(small_payload, max_size=1000), small_payload)

        # Case 2: Smart truncation of long string (threshold bumped to 8000 chars)
        long_string = "a" * 20000
        large_payload = json.dumps({"header": "test", "elements": [{"tag": "div", "text": {"content": long_string}}]})

        truncated = renderer._check_and_truncate_payload(large_payload, max_size=12000)
        truncated_obj = json.loads(truncated)

        # Verify structure preserved
        self.assertIn("header", truncated_obj)
        self.assertIn("elements", truncated_obj)

        # Verify string truncated
        content = truncated_obj["elements"][0]["text"]["content"]
        self.assertLess(len(content), 20000)
        self.assertIn("…(已截断)", content)

        # Verify warning added
        self.assertTrue(any("内容过长" in str(e) for e in truncated_obj["elements"]))

        # Case 3: Deeply nested truncation
        nested_payload = json.dumps({"level1": {"level2": {"level3": {"content": long_string}}}})
        truncated = renderer._check_and_truncate_payload(nested_payload, max_size=12000)
        truncated_obj = json.loads(truncated)
        content = truncated_obj["level1"]["level2"]["level3"]["content"]
        self.assertIn("…(已截断)", content)

        # Case 4: Fallback for massive structure
        # Create a structure that is still too big even after string truncation (e.g. huge list)
        huge_list = [{"content": "a" * 10}] * 1000
        massive_payload = json.dumps({"elements": huge_list})

        # Set max_size small to force fallback
        fallback = renderer._check_and_truncate_payload(massive_payload, max_size=500)
        fallback_obj = json.loads(fallback)

        # Verify fallback card structure
        self.assertIn("header", fallback_obj)
        self.assertIn("卡片过大", fallback_obj["header"]["title"]["content"])

    def test_system_handler_dispatch(self):
        """Test that SystemHandler correctly dispatches commands using the new registry."""
        mock_ctx = MagicMock()
        handler = SystemHandler(mock_ctx)
    
        # Mock handlers in registry
        coco_mock = MagicMock()
        project_mock = MagicMock()
        diagnostics_mock = MagicMock()
        mock_ctx.handlers.get.side_effect = lambda k: {
            "coco": coco_mock,
            "project": project_mock,
            "diagnostics": diagnostics_mock,
        }.get(k)
    
        # Test exact match
        handler.handle_intercepted_command(
            "mid",
            "cid",
            "/coco_info",
            command_match=SlashCommandParser.parse("/coco_info"),
        )
        coco_mock.show_info.assert_called_with("mid", "cid", None)
    
        # Test prefix match
        handler.handle_intercepted_command(
            "mid",
            "cid",
            "/status detail",
            command_match=SlashCommandParser.parse("/status detail"),
        )
        diagnostics_mock.show_unified_status.assert_called_with("mid", "cid", "/status detail", None)
    
        # Test fallback
        with patch.object(handler, "show_full_help") as mock_help:
            handler.handle_intercepted_command(
                "mid",
                "cid",
                "/unknown_cmd",
                command_match=SlashCommandParser.parse("/unknown_cmd"),
            )
            mock_help.assert_called_with("mid", "cid", None)

    def test_ws_client_refactor_structure(self):
        """Test that the refactored WSClient methods exist and validate basic message."""
        # We can't easily instantiate WSClient fully without many mocks,
        # but we can test the specific logic by partial mocking or just checking attributes if we could.
        # Instead, let's use the ExitStack approach from before to instantiate it safely.
        from contextlib import ExitStack

        with ExitStack() as stack:
            stack.enter_context(patch("src.feishu.ws_client.get_settings"))
            stack.enter_context(patch("src.feishu.ws_client.ACPSessionManager"))
            stack.enter_context(patch("src.feishu.ws_client.IntentRecognizer"))
            stack.enter_context(patch("src.feishu.ws_client.TaskScheduler"))
            stack.enter_context(patch("src.feishu.ws_client.ProjectManager"))
            stack.enter_context(patch("src.feishu.ws_client.MessageProjectMapper"))
            stack.enter_context(patch("src.feishu.ws_client.MessageLinker"))
            stack.enter_context(patch("src.mode.ModeManager"))
            stack.enter_context(patch("src.feishu.ws_client.ProjectContextManager"))
            stack.enter_context(patch("src.feishu.ws_client.DeepEngineManager"))
            stack.enter_context(patch("src.feishu.ws_client.SpecEngineManager"))
            stack.enter_context(patch("src.feishu.ws_client.HandlerContext"))
            stack.enter_context(patch("src.feishu.ws_client.ActionDispatcher"))
            stack.enter_context(patch("src.feishu.ws_client.CocoModeHandler"))
            stack.enter_context(patch("src.feishu.ws_client.ClaudeModeHandler"))
            stack.enter_context(patch("src.feishu.ws_client.TTADKModeHandler"))
            stack.enter_context(patch("src.feishu.ws_client.DeepHandler"))
            stack.enter_context(patch("src.feishu.ws_client.SpecHandler"))
            stack.enter_context(patch("src.feishu.ws_client.ProjectHandler"))
            stack.enter_context(patch("src.feishu.ws_client.SystemHandler"))
            stack.enter_context(patch("src.feishu.ws_client.DiagnosticsHandler"))

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
            client._reply_text = MagicMock()
            self.assertFalse(client._validate_message(mock_msg, "req_id"))
            client._reply_text.assert_called()


if __name__ == "__main__":
    unittest.main()
