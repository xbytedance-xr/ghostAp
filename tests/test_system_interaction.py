import json
import unittest
from unittest.mock import MagicMock

from src.feishu.handlers.system import SystemHandler


class TestSystemInteraction(unittest.TestCase):
    def setUp(self):
        self.mock_ctx = MagicMock()
        self.mock_ctx.settings.app_id = "test_app"
        self.mock_ctx.settings.app_secret = "test_secret"

        # Mock mode manager
        self.mock_ctx.mode_manager.get_mode.return_value = "smart"
        self.mock_ctx.mode_manager.is_coco_mode.return_value = False

        # Mock project manager
        self.mock_ctx.project_manager.get_active_project.return_value = None

        # Mock working dir
        self.mock_ctx.working_dirs = {}

        self.handler = SystemHandler(self.mock_ctx)

        # Mock BaseHandler methods
        self.handler.reply_card = MagicMock()
        self.handler.reply_text = MagicMock()
        self.handler.update_card = MagicMock()
        self.handler.get_working_dir = MagicMock(return_value="/tmp")

    def test_show_full_help(self):
        """测试 /help 命令生成并发送交互式卡片"""
        # Execute
        self.handler.show_full_help("msg_1", "chat_1")

        # Verify reply_card called (production uses self.reply_card)
        self.handler.reply_card.assert_called_once()
        args = self.handler.reply_card.call_args
        msg_id = args[0][0]
        content = args[0][1]

        self.assertEqual(msg_id, "msg_1")

        # Verify content structure
        card = json.loads(content)
        self.assertIn("header", card)
        self.assertEqual(card["header"]["title"]["content"], "📖 GhostAP 使用帮助")

        # Verify category buttons exist
        elements = card["body"]["elements"]
        has_buttons = any(e.get("tag") == "column_set" for e in elements)
        self.assertTrue(has_buttons)

    def test_handle_menu_command(self):
        """测试 /menu 命令生成并发送菜单卡片"""
        # Execute
        self.handler.handle_menu_command("msg_2", "chat_1")

        # Verify reply_card called
        self.handler.reply_card.assert_called_once()
        args = self.handler.reply_card.call_args
        content = args[0][1]

        # Verify content structure
        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "📱 快捷菜单")

        # Verify buttons - search all elements and all column sets
        elements = card["body"]["elements"]
        found_actions = []
        for elem in elements:
            if elem.get("tag") == "column_set":
                for col in elem.get("columns", []):
                    for sub_elem in col.get("elements", []):
                        if sub_elem.get("tag") == "button":
                            val = sub_elem.get("value", {})
                            found_actions.append(val.get("action"))
            elif elem.get("tag") == "action":  # Also check action sets if used
                for action in elem.get("actions", []):
                    if action.get("tag") == "button":
                        val = action.get("value", {})
                        found_actions.append(val.get("action"))

        self.assertIn("new_project_prompt", found_actions)
        self.assertIn("switch_project", found_actions)

    def test_handle_help_category_patch(self):
        """测试帮助分类切换使用 Patch 更新卡片"""
        # Mock update_card to succeed
        self.handler.update_card.return_value = True

        # Execute
        self.handler.handle_help_category("msg_3", "chat_1", "deep", origin_message_id="origin_msg_id")

        # Verify update_card used
        self.handler.update_card.assert_called_once()
        self.handler.reply_card.assert_not_called()

        # Verify content contains Deep specific help
        args = self.handler.update_card.call_args
        content = args[0][1]
        self.assertIn("Deep Engine", content)

    def test_handle_help_category_reply_fallback(self):
        """测试 Patch 失败时回退到 Reply"""
        # Mock update_card to fail
        self.handler.update_card.return_value = False

        # Execute
        self.handler.handle_help_category("msg_4", "chat_1", "project", origin_message_id="origin_msg_id")

        # Verify update_card called but failed
        self.handler.update_card.assert_called_once()

        # Verify reply_card called as fallback
        self.handler.reply_card.assert_called_once()

        # Verify content contains Project specific help
        args = self.handler.reply_card.call_args
        content = args[0][1]
        self.assertIn("项目管理", content)

    def test_help_more_category_lists_full_spec_commands(self):
        self.handler.update_card.return_value = True

        self.handler.handle_help_category("msg_5", "chat_1", "more", origin_message_id="origin_msg_id")

        args = self.handler.update_card.call_args
        content = args[0][1]
        self.assertIn("/spec_pause", content)
        self.assertIn("/spec_resume", content)
        self.assertIn("/spec_metrics", content)
        self.assertIn("/spec_save", content)
        self.assertIn("/spec_export", content)
        self.assertIn("/spec_recover", content)


if __name__ == "__main__":
    unittest.main()
