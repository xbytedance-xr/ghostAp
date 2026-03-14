import unittest
from unittest.mock import MagicMock, patch
from src.feishu.renderers.base import BaseRenderer
from src.feishu.renderers.loop_renderer import LoopRenderer
from src.feishu.renderers.spec_renderer import SpecRenderer
from src.feishu.handlers.base import BaseHandler
from src.feishu.handlers.loop import LoopHandler
from src.feishu.handlers.spec import SpecHandler

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
        # Collapse logic: > 3 items, expand=False -> Hide completed
        content = "- ✅ Item 1\n- ✅ Item 2\n- ✅ Item 3\n- ⬜️ Item 4"
        result = self.base_renderer._render_collapsible_section(
            content, total_items=4, expanded=False, completed_count=3
        )
        
        self.assertIn("✅ 已通过 3 项", result)
        self.assertNotIn("- ✅ Item 1", result)
        self.assertIn("- ⬜️ Item 4", result)
        
        # Expand logic
        result = self.base_renderer._render_collapsible_section(
            content, total_items=4, expanded=True, completed_count=3
        )
        self.assertEqual(result, content)

    def test_base_render_collapsible_section_text(self):
        """Test BaseRenderer._render_collapsible_section with long text (Spec mode)"""
        # Create a long text > 10 lines
        lines = [f"Line {i}" for i in range(15)]
        content = "\n".join(lines)
        
        # Should truncate if not expanded
        result = self.base_renderer._render_collapsible_section(
            content, total_items=15, expanded=False
        )
        self.assertIn("📄 内容较长 (共 15 行)", result)
        self.assertIn("Line 0", result)
        self.assertIn("Line 4", result)
        self.assertNotIn("Line 14", result)
        
        # Should show all if expanded
        result = self.base_renderer._render_collapsible_section(
            content, total_items=15, expanded=True
        )
        self.assertEqual(result, content)

    def test_loop_renderer_inheritance(self):
        """Verify LoopRenderer inherits and uses base methods"""
        mock_loop_handler = MagicMock(spec=LoopHandler)
        mock_loop_handler.ctx = MagicMock()
        mock_loop_handler.settings = MagicMock()
        renderer = LoopRenderer(mock_loop_handler)
        
        self.assertTrue(hasattr(renderer, '_generate_progress_bar'))
        self.assertTrue(hasattr(renderer, '_render_collapsible_section'))
        
        # Verify it works
        self.assertEqual(renderer._generate_progress_bar(1, 2), "✅⬜️")

    def test_spec_renderer_inheritance(self):
        """Verify SpecRenderer inherits and uses base methods"""
        mock_spec_handler = MagicMock(spec=SpecHandler)
        mock_spec_handler.ctx = MagicMock()
        mock_spec_handler.settings = MagicMock()
        renderer = SpecRenderer(mock_spec_handler)
        
        self.assertTrue(hasattr(renderer, '_generate_progress_bar'))
        self.assertTrue(hasattr(renderer, '_render_collapsible_section'))
        
        # Verify default state includes expand_ac
        state = renderer.get_default_ui_state()
        self.assertIn("expand_ac", state)
        self.assertFalse(state["expand_ac"])

if __name__ == "__main__":
    unittest.main()
