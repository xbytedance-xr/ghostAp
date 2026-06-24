"""Tests for src/workflow_engine/tool_registry.py — dynamic tool discovery."""

import unittest
from unittest.mock import patch

from src.workflow_engine.tool_registry import (
    _FALLBACK_DESCRIPTIONS,
    _discover_acp_tools,
    get_available_tools,
    invalidate_cache,
)


class TestGetAvailableTools(unittest.TestCase):
    """Test get_available_tools returns a valid tool dict."""

    def setUp(self):
        invalidate_cache()

    def tearDown(self):
        invalidate_cache()

    def test_returns_dict(self):
        tools = get_available_tools()
        self.assertIsInstance(tools, dict)

    def test_contains_coco(self):
        tools = get_available_tools()
        self.assertIn("coco", tools)

    def test_all_values_are_strings(self):
        tools = get_available_tools()
        for name, desc in tools.items():
            self.assertIsInstance(name, str)
            self.assertIsInstance(desc, str)
            self.assertTrue(len(desc) > 0, f"{name} has empty description")

    def test_fallback_on_import_error(self):
        """When ACP imports fail, returns fallback descriptions."""
        with patch(
            "src.workflow_engine.tool_registry._discover_acp_tools",
            side_effect=Exception("no acp"),
        ):
            invalidate_cache()
            tools = get_available_tools(force_refresh=True)
            # Should still have all fallback tools
            for name in _FALLBACK_DESCRIPTIONS:
                self.assertIn(name, tools)

    def test_cache_returns_copy(self):
        """Returned dict is a copy — mutations don't affect cache."""
        tools1 = get_available_tools()
        tools1["MUTATED"] = "should not persist"
        tools2 = get_available_tools()
        self.assertNotIn("MUTATED", tools2)

    def test_force_refresh(self):
        """force_refresh=True re-discovers even with warm cache."""
        call_count = [0]

        def counting_discover(**_kwargs):
            call_count[0] += 1
            return dict(_FALLBACK_DESCRIPTIONS)

        with patch(
            "src.workflow_engine.tool_registry._discover_tools",
            side_effect=counting_discover,
        ):
            invalidate_cache()
            get_available_tools(force_refresh=True)
            get_available_tools(force_refresh=True)
            self.assertEqual(call_count[0], 2)

    def test_require_available_does_not_add_fallback_tools(self):
        """Selectable UI mode must not show tools that were only fallback names."""
        with patch(
            "src.workflow_engine.tool_registry._discover_acp_tools",
            return_value={},
        ):
            invalidate_cache()
            tools = get_available_tools(force_refresh=True, require_available=True)
            self.assertEqual(tools, {})

    def test_discover_acp_tools_filters_unavailable_providers(self):
        """require_available=True keeps only providers passing runtime probes."""
        with patch(
            "src.acp.providers.get_providers",
            return_value={"coco": object(), "codex": object()},
        ), patch(
            "src.acp.providers.tool_registry.get_availability",
            side_effect=lambda name, **_: name == "coco",
        ), patch(
            "src.utils.text.get_acp_result_header_text",
            return_value={"tool_desc_coco": "Coco OK", "tool_desc_codex": "Codex NO"},
        ):
            tools = _discover_acp_tools(require_available=True)

        self.assertEqual(tools, {"coco": "Coco OK"})


class TestInvalidateCache(unittest.TestCase):
    """Test cache invalidation."""

    def test_invalidate_forces_rediscovery(self):
        # Warm cache
        get_available_tools()
        invalidate_cache()

        call_count = [0]

        def counting_discover(**_kwargs):
            call_count[0] += 1
            return dict(_FALLBACK_DESCRIPTIONS)

        with patch(
            "src.workflow_engine.tool_registry._discover_tools",
            side_effect=counting_discover,
        ):
            get_available_tools()
            self.assertEqual(call_count[0], 1)


if __name__ == "__main__":
    unittest.main()
