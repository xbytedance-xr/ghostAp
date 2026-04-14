import unittest
from unittest.mock import MagicMock

from src.feishu.handlers.loop import LoopHandler
from src.feishu.renderers.loop_renderer import LoopRenderer


class TestLoopUIOptimization(unittest.TestCase):
    def setUp(self):
        self.mock_handler = MagicMock(spec=LoopHandler)
        self.mock_handler.ctx = MagicMock()  # Mock ctx explicitly
        self.mock_handler.settings = MagicMock()
        self.mock_handler.settings.card_deep_compact_default = False
        self.renderer = LoopRenderer(self.mock_handler)

    def test_generate_progress_bar(self):
        """Test the logic for generating emoji progress bars"""
        # Case 1: Empty
        self.assertEqual(self.renderer._generate_progress_bar(0, 0), "")

        # Case 2: Small total (<= 10)
        # 3/5 => ✅✅✅⬜️⬜️
        bar = self.renderer._generate_progress_bar(3, 5)
        self.assertEqual(bar, "✅✅✅⬜️⬜️")

        # Case 3: Large total (> 10)
        # 15/20 => ratio 0.75 => 7.5 => 7 filled, 3 empty => ✅✅✅✅✅✅✅⬜️⬜️⬜️ (15/20)
        # MAX_BAR_LEN = 10
        # filled = int(15/20 * 10) = int(7.5) = 7
        bar = self.renderer._generate_progress_bar(15, 20)
        expected = "✅" * 7 + "⬜️" * 3 + " (15/20)"
        self.assertEqual(bar, expected)

    def test_render_ac_section_collapse(self):
        """Test AC section folding logic"""
        # Mock inputs
        # _render_collapsible_section signature: (content, total_items, expanded, completed_count=0)

        # Scenario 1: Few items (<= COLLAPSE_ITEM_THRESHOLD=8), no collapse
        criteria_section = "- ✅ AC1\n- ✅ AC2\n- ⬜️ AC3"
        result = self.renderer._render_collapsible_section(criteria_section, 3, False, completed_count=2)
        self.assertEqual(result, criteria_section)

        # Scenario 2: Many items (> COLLAPSE_ITEM_THRESHOLD=8), collapse enabled (expand_ac=False)
        # Should hide ✅ items
        completed_lines = [f"- ✅ AC{i}" for i in range(1, 9)]
        incomplete_line = "- ⬜️ AC9"
        criteria_section = "\n".join(completed_lines + [incomplete_line])
        result = self.renderer._render_collapsible_section(criteria_section, 9, False, completed_count=8)

        # Expect summary + incomplete items
        self.assertIn("✅ 已通过 8 项", result)
        self.assertIn("⬜️ AC9", result)
        self.assertNotIn("- ✅ AC1", result)

        # Scenario 3: Many items, expanded (expand_ac=True)
        # Should show all
        result = self.renderer._render_collapsible_section(criteria_section, 9, True, completed_count=8)
        self.assertEqual(result, criteria_section)

    def test_handler_toggle_ac(self):
        """Test handler state toggle method"""
        # Create real handler with mocked dependencies to test state update
        mock_ctx = MagicMock()
        handler = LoopHandler(mock_ctx)

        # Manually initialize the renderer since LoopHandler.__init__ does it
        # But here we want to inspect its state easily
        # LoopHandler.__init__ calls super.__init__ then self.renderer = LoopRenderer(self)
        # We can just use the handler's renderer

        # Inject mock method to avoid real rendering calls
        handler.renderer.render_current_view = MagicMock()

        # Initial state setup manually in the renderer's internal state dict
        # The renderer stores state in self.ui_states (inherited from BaseRenderer usually or custom)
        loop_project_id = "test_root"
        handler.renderer.update_ui_state(loop_project_id, expand_ac=False)

        # Call toggle
        handler.toggle_loop_ac("msg_id", "chat_id", None, loop_project_id, expand_ac=True)

        # Verify state updated
        state = handler.renderer.get_ui_state(loop_project_id)
        self.assertTrue(state["expand_ac"])
        # Verify render called
        handler.renderer.render_current_view.assert_called_with("msg_id", "chat_id", None, origin_message_id="msg_id")


if __name__ == "__main__":
    unittest.main()
