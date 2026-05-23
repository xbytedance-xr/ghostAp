"""Tests for 2-button vertical layout on narrow screens.

Verifies that build_responsive_layout with force_vertical=True
renders buttons in a vertical stack instead of horizontal grid.

This ensures buttons don't get truncated on 320px narrow screens.

Implementation notes:
- Schema 2.0 uses column_set for both horizontal and vertical layouts
- Horizontal: 1 column_set with N columns (buttons side-by-side)
- Vertical: N column_set elements with 1 column each (buttons stacked)
"""

from src.card.shared import build_responsive_layout


def _make_button(text: str, action: str = "test_action") -> dict:
    """Create a test button dict."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "default",
        "behaviors": [{"type": "callback", "value": {"action": action}}],
    }


class TestForceVerticalLayout:
    """Test suite for force_vertical parameter."""

    def test_force_vertical_2_buttons_uses_vertical(self):
        """With force_vertical=True, 2 buttons use vertical stack (2 column_set elements)."""
        buttons = [_make_button("Button 1"), _make_button("Button 2")]

        # Without force_vertical, 2 buttons use horizontal (1 column_set)
        horizontal = build_responsive_layout(buttons)
        # Horizontal: single column_set with 2 columns
        assert len(horizontal) == 1
        assert horizontal[0]["tag"] == "column_set"
        assert len(horizontal[0]["columns"]) == 2

        # With force_vertical=True, uses vertical stack (2 column_set elements)
        vertical = build_responsive_layout(buttons, force_vertical=True)
        # Vertical: 2 separate column_set elements, each with 1 button
        assert len(vertical) == 2
        assert vertical[0]["tag"] == "column_set"
        assert len(vertical[0]["columns"]) == 1

    def test_force_vertical_takes_precedence(self):
        """force_vertical=True takes precedence over layout='desktop'."""
        buttons = [_make_button("A"), _make_button("B")]

        # Even with layout="desktop", force_vertical wins
        result = build_responsive_layout(buttons, layout="desktop", force_vertical=True)
        # Should be vertical (2 column_set elements)
        assert len(result) == 2

    def test_force_vertical_single_button(self):
        """force_vertical works with single button."""
        buttons = [_make_button("Single")]

        result = build_responsive_layout(buttons, force_vertical=True)
        # Single button: 1 column_set
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"

    def test_force_vertical_many_buttons(self):
        """force_vertical works with 3+ buttons (N column_set elements)."""
        buttons = [_make_button(f"B{i}") for i in range(5)]

        result = build_responsive_layout(buttons, force_vertical=True)
        # Vertical: 5 separate column_set elements
        assert len(result) == 5
        assert all(r["tag"] == "column_set" for r in result)

    def test_default_2_buttons_horizontal(self):
        """By default (force_vertical=False), 2 buttons use horizontal layout."""
        buttons = [_make_button("A"), _make_button("B")]

        result = build_responsive_layout(buttons)
        # Default: 2 buttons in 1 column_set (horizontal)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert len(result[0]["columns"]) == 2

    def test_force_vertical_empty_buttons(self):
        """force_vertical with empty buttons returns empty list."""
        result = build_responsive_layout([], force_vertical=True)
        assert result == []

    def test_force_vertical_vs_mobile_force_vertical(self):
        """force_vertical affects 2 buttons; mobile_force_vertical only affects >2."""
        buttons = [_make_button("A"), _make_button("B")]

        # mobile_force_vertical only affects >2 buttons
        result_mobile = build_responsive_layout(buttons, mobile_force_vertical=True)
        # 2 buttons still horizontal with mobile_force_vertical
        assert len(result_mobile) == 1
        assert len(result_mobile[0]["columns"]) == 2

        # force_vertical affects any button count
        result_force = build_responsive_layout(buttons, force_vertical=True)
        # 2 buttons become vertical with force_vertical
        assert len(result_force) == 2


class TestVerticalVsHorizontalStructure:
    """Verify the structural difference between vertical and horizontal layouts."""

    def test_horizontal_layout_structure(self):
        """Horizontal layout: single column_set with N columns."""
        buttons = [_make_button("A"), _make_button("B")]
        result = build_responsive_layout(buttons)

        # Horizontal: 1 column_set with 2 columns
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert len(result[0]["columns"]) == 2

    def test_vertical_layout_structure(self):
        """Vertical layout: N column_set elements with 1 column each."""
        buttons = [_make_button("A"), _make_button("B")]
        result = build_responsive_layout(buttons, force_vertical=True)

        # Vertical: 2 column_set elements, each with 1 column
        assert len(result) == 2
        assert result[0]["tag"] == "column_set"
        assert result[1]["tag"] == "column_set"
        assert len(result[0]["columns"]) == 1
        assert len(result[1]["columns"]) == 1
