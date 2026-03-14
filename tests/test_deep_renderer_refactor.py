import unittest
from unittest.mock import MagicMock
from src.feishu.renderers.deep_renderer import DeepRenderer
from src.feishu.handlers.deep import DeepHandler

class TestDeepRendererRefactor(unittest.TestCase):
    def setUp(self):
        self.mock_handler = MagicMock(spec=DeepHandler)
        self.mock_handler.ctx = MagicMock()
        self.mock_handler.settings = MagicMock()
        self.mock_handler.settings.card_deep_compact_default = False
        self.renderer = DeepRenderer(self.mock_handler)
        
    def test_inheritance(self):
        """Verify DeepRenderer inherits from BaseRenderer"""
        self.assertTrue(hasattr(self.renderer, '_generate_progress_bar'))
        self.assertTrue(hasattr(self.renderer, '_render_collapsible_section'))
        
    def test_ui_state_defaults(self):
        """Verify DeepRenderer specific UI defaults"""
        state = self.renderer.get_default_ui_state()
        self.assertIn("expand_ac", state)
        self.assertFalse(state["expand_ac"])
        
    def test_text_collapsing(self):
        """Verify text collapsing works for Markdown content (typical for Deep mode)"""
        # Long text with multiple paragraphs/lines
        long_content = "\n".join([f"Step {i}: Thinking process..." for i in range(20)])
        
        # Should be collapsed by default
        collapsed = self.renderer._render_collapsible_section(
            long_content, 
            total_items=20, 
            expanded=False
        )
        self.assertIn("📄 内容较长", collapsed)
        self.assertIn("Step 0", collapsed)
        self.assertNotIn("Step 19", collapsed)
        
        # Should be full when expanded
        expanded_content = self.renderer._render_collapsible_section(
            long_content, 
            total_items=20, 
            expanded=True
        )
        self.assertEqual(expanded_content, long_content)

if __name__ == "__main__":
    unittest.main()
