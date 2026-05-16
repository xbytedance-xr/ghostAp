"""Test that BUTTON_CONFIG text values stay in sync with UI_TEXT."""

from src.card.buttons_config import BUTTON_CONFIG
from src.card.ui_text import UI_TEXT

# Mapping: BUTTON_CONFIG key → UI_TEXT key (for buttons that have a corresponding UI_TEXT entry)
_BUTTON_TO_UI_TEXT_KEY = {
    "mode_full": "card_btn_mode_full",
    "mode_compact": "card_btn_mode_compact",
    "stop_danger": "card_btn_force_stop",
}


class TestButtonTextConsistency:
    """Ensure BUTTON_CONFIG text values match UI_TEXT counterparts."""

    def test_mapped_buttons_use_ui_text_values(self):
        """Buttons with known UI_TEXT keys must reference the same value."""
        for btn_key, ui_key in _BUTTON_TO_UI_TEXT_KEY.items():
            assert btn_key in BUTTON_CONFIG, f"BUTTON_CONFIG missing key: {btn_key}"
            assert ui_key in UI_TEXT, f"UI_TEXT missing key: {ui_key}"
            assert BUTTON_CONFIG[btn_key]["text"] == UI_TEXT[ui_key], (
                f"BUTTON_CONFIG['{btn_key}']['text'] = {BUTTON_CONFIG[btn_key]['text']!r} "
                f"!= UI_TEXT['{ui_key}'] = {UI_TEXT[ui_key]!r}"
            )

    def test_button_config_has_required_keys(self):
        """Each BUTTON_CONFIG entry must have 'text' and 'type' fields."""
        for key, config in BUTTON_CONFIG.items():
            assert "text" in config, f"BUTTON_CONFIG['{key}'] missing 'text'"
            assert "type" in config, f"BUTTON_CONFIG['{key}'] missing 'type'"
            assert isinstance(config["text"], str), f"BUTTON_CONFIG['{key}']['text'] must be str"
