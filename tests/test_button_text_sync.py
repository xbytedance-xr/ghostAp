"""Test that button text in buttons_config.py stays in sync with UI_TEXT.

CI blocker: if this test fails, the merge is blocked until text is reconciled.
"""

from src.card.buttons_config import BUTTON_CONFIG
from src.card.ui_text import UI_TEXT

# Mapping from BUTTON_CONFIG keys to their corresponding UI_TEXT keys.
# Only keys that exist in BOTH sources need to be in sync.
_SYNC_MAP: dict[str, str] = {
    "mode_full": "card_btn_mode_full",
    "mode_compact": "card_btn_mode_compact",
}


def test_button_text_sync_mapped_keys():
    """Ensure explicitly mapped button texts match between BUTTON_CONFIG and UI_TEXT."""
    mismatches: list[str] = []
    for config_key, ui_key in _SYNC_MAP.items():
        config_text = BUTTON_CONFIG[config_key]["text"]
        ui_text = UI_TEXT[ui_key]
        if config_text != ui_text:
            mismatches.append(
                f"  {config_key!r}: BUTTON_CONFIG={config_text!r} vs UI_TEXT[{ui_key!r}]={ui_text!r}"
            )
    assert not mismatches, "Button text drift detected:\n" + "\n".join(mismatches)


def test_button_config_keys_exist():
    """Ensure all BUTTON_CONFIG keys referenced in _SYNC_MAP actually exist."""
    for config_key in _SYNC_MAP:
        assert config_key in BUTTON_CONFIG, f"Missing key in BUTTON_CONFIG: {config_key!r}"


def test_ui_text_keys_exist():
    """Ensure all UI_TEXT keys referenced in _SYNC_MAP actually exist."""
    for ui_key in _SYNC_MAP.values():
        assert ui_key in UI_TEXT, f"Missing key in UI_TEXT: {ui_key!r}"
