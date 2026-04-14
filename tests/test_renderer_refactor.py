import unittest
from unittest.mock import MagicMock

from src.feishu.handlers.base import BaseHandler
from src.feishu.handlers.loop import LoopHandler
from src.feishu.handlers.spec import SpecHandler
from src.feishu.renderers.base import BaseRenderer
from src.feishu.renderers.loop_renderer import LoopRenderer
from src.feishu.renderers.spec_renderer import SpecRenderer


class TestRendererRefactor(unittest.TestCase):
    def setUp(self):
        # Create a mock handler to initialize BaseRenderer
        self.mock_handler = MagicMock(spec=BaseHandler)
        self.mock_handler.ctx = MagicMock()
        self.mock_handler.settings = MagicMock()
        self.mock_handler.settings.card_deep_compact_default = False

        self.base_renderer = BaseRenderer(self.mock_handler)

    def test_base_generate_progress_bar(self):
        """Test BaseRenderer._generate_progress_bar"""
        # Same tests as before, but on base class
        self.assertEqual(self.base_renderer._generate_progress_bar(0, 0), "")
        self.assertEqual(self.base_renderer._generate_progress_bar(3, 5), "✅✅✅⬜️⬜️")

        bar = self.base_renderer._generate_progress_bar(15, 20)
        expected = "✅" * 7 + "⬜️" * 3 + " (15/20)"
        self.assertEqual(bar, expected)

    def test_base_render_collapsible_section_ac(self):
        """Test BaseRenderer._render_collapsible_section with AC list"""
        # Collapse logic: > COLLAPSE_ITEM_THRESHOLD=8 items, expand=False -> Hide completed
        completed_lines = [f"- \u2705 Item {i}" for i in range(1, 9)]
        incomplete_line = "- \u2b1c\ufe0f Item 9"
        content = "\n".join(completed_lines + [incomplete_line])
        result = self.base_renderer._render_collapsible_section(
            content, total_items=9, expanded=False, completed_count=8
        )

        self.assertIn("\u2705 \u5df2\u901a\u8fc7 8 \u9879", result)  # ✅ 已通过 8 项
        self.assertNotIn("- \u2705 Item 1", result)
        self.assertIn("- \u2b1c\ufe0f Item 9", result)

        # Expand logic
        result = self.base_renderer._render_collapsible_section(
            content, total_items=9, expanded=True, completed_count=8
        )
        self.assertEqual(result, content)

    def test_base_render_collapsible_section_text(self):
        """Test BaseRenderer._render_collapsible_section with long text (Spec mode)"""
        # Create a long text > COLLAPSE_LINE_THRESHOLD=30 lines
        lines = [f"Line {i}" for i in range(40)]
        content = "\n".join(lines)

        # Should truncate if not expanded (shows first COLLAPSE_DISPLAY_LINES=15 lines)
        result = self.base_renderer._render_collapsible_section(content, total_items=40, expanded=False)
        self.assertIn("\u5185\u5bb9\u8f83\u957f (共 40 行)", result)  # 内容较长 (共 40 行)
        self.assertIn("Line 0", result)
        self.assertIn("Line 14", result)
        self.assertNotIn("Line 39", result)

        # Should show all if expanded
        result = self.base_renderer._render_collapsible_section(content, total_items=40, expanded=True)
        self.assertEqual(result, content)

    def test_loop_renderer_inheritance(self):
        """Verify LoopRenderer inherits and uses base methods"""
        mock_loop_handler = MagicMock(spec=LoopHandler)
        mock_loop_handler.ctx = MagicMock()
        mock_loop_handler.settings = MagicMock()
        renderer = LoopRenderer(mock_loop_handler)

        self.assertTrue(hasattr(renderer, "_generate_progress_bar"))
        self.assertTrue(hasattr(renderer, "_render_collapsible_section"))

        # Verify it works
        self.assertEqual(renderer._generate_progress_bar(1, 2), "✅⬜️")

    def test_spec_renderer_inheritance(self):
        """Verify SpecRenderer inherits and uses base methods"""
        mock_spec_handler = MagicMock(spec=SpecHandler)
        mock_spec_handler.ctx = MagicMock()
        mock_spec_handler.settings = MagicMock()
        renderer = SpecRenderer(mock_spec_handler)

        self.assertTrue(hasattr(renderer, "_generate_progress_bar"))
        self.assertTrue(hasattr(renderer, "_render_collapsible_section"))

        # Verify default state includes expand_ac
        state = renderer.get_default_ui_state()
        self.assertIn("expand_ac", state)
        self.assertFalse(state["expand_ac"])


if __name__ == "__main__":
    unittest.main()
