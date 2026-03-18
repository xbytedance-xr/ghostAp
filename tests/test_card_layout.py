from unittest.mock import MagicMock, patch

import pytest

from src.card.shared import build_responsive_layout


class TestCardLayout:
    @pytest.fixture
    def mock_settings(self):
        with patch("src.card.shared.get_settings") as mock:
            settings = MagicMock()
            # Defaults
            settings.card_button_layout = "responsive"
            settings.card_button_size = "medium"
            settings.card_mobile_force_vertical = True
            settings.card_mobile_layout_mode = "vertical"
            mock.return_value = settings
            yield settings

    @pytest.mark.parametrize(
        "layout_mode, button_count, expected_tag, expected_mode",
        [
            ("desktop", 2, "column_set", "stretch"),  # Desktop uses grid (Action row emulated)
            ("mobile", 2, "column_set", "none"),  # Mobile force vertical
            ("flow", 2, "column_set", "flow"),  # Flow layout
        ],
    )
    def test_explicit_layout_modes(self, mock_settings, layout_mode, button_count, expected_tag, expected_mode):
        """Test explicit layout modes defined in card_button_layout."""
        mock_settings.card_button_layout = layout_mode
        buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": f"Btn {i}"}} for i in range(button_count)]

        result = build_responsive_layout(buttons)

        assert len(result) > 0
        first_row = result[0]
        assert first_row.get("tag") == expected_tag
        assert first_row.get("flex_mode") == expected_mode

    def test_responsive_layout_few_buttons(self, mock_settings):
        """Responsive layout with <= 2 buttons should use grid (row action style)."""
        mock_settings.card_button_layout = "responsive"
        buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": f"Btn {i}"}} for i in range(2)]

        result = build_responsive_layout(buttons)

        # Grid layout (2 columns)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert result[0]["flex_mode"] == "stretch"
        assert len(result[0]["columns"]) == 2

    @pytest.mark.parametrize(
        "mobile_mode, expected_flex_mode",
        [
            ("vertical", "none"),
            ("flow", "flow"),
        ],
    )
    def test_mobile_force_vertical_strategy(self, mock_settings, mobile_mode, expected_flex_mode):
        """Test mobile optimization strategy when buttons > 2."""
        mock_settings.card_button_layout = "responsive"
        mock_settings.card_mobile_force_vertical = True
        mock_settings.card_mobile_layout_mode = mobile_mode

        # 3 buttons to trigger mobile optimization
        buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": f"Btn {i}"}} for i in range(3)]

        result = build_responsive_layout(buttons)

        if mobile_mode == "vertical":
            # Vertical stack: multiple column_sets or one with flex_mode=none?
            # _build_button_vertical returns multiple rows (one per button)
            assert len(result) == 3
            for row in result:
                assert row["tag"] == "column_set"
                assert row["flex_mode"] == "none"
        else:
            # Flow: single column_set with flow mode
            assert len(result) == 1
            assert result[0]["tag"] == "column_set"
            assert result[0]["flex_mode"] == "flow"

    def test_mobile_force_vertical_disabled(self, mock_settings):
        """Test when mobile optimization is disabled."""
        mock_settings.card_button_layout = "responsive"
        mock_settings.card_mobile_force_vertical = False

        # 3 buttons, should fall back to grid layout (2 columns)
        buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": f"Btn {i}"}} for i in range(3)]

        result = build_responsive_layout(buttons)

        # 3 buttons in 2-column grid -> 2 rows
        assert len(result) == 2
        assert result[0]["flex_mode"] == "stretch"  # Grid row 1
        assert result[1]["flex_mode"] == "stretch"  # Grid row 2
