"""Tests to verify styles.py split maintains backward compatibility."""
import pytest


class TestThemes:
    def test_import_from_new_module(self):
        from src.card.themes import ProjectTheme, THEMES, DARK_THEME_NAMES, ENGINE_STYLES, PANEL_STYLES, get_theme
        assert len(THEMES) >= 18
        assert isinstance(DARK_THEME_NAMES, (set, frozenset))
        assert "loop" in ENGINE_STYLES or "default" in ENGINE_STYLES
        theme = get_theme("blue")
        assert isinstance(theme, ProjectTheme)

    def test_backward_compat_themes(self):
        from src.card.styles import ProjectTheme, THEMES, DARK_THEME_NAMES, ENGINE_STYLES, PANEL_STYLES
        assert len(THEMES) >= 18

    def test_get_available_themes(self):
        from src.card.themes import get_available_themes, DARK_THEME_NAMES
        light_themes = get_available_themes(include_dark=False)
        all_themes = get_available_themes(include_dark=True)
        assert len(all_themes) >= 18
        assert len(light_themes) < len(all_themes)
        for dark_name in DARK_THEME_NAMES:
            assert dark_name not in light_themes

    def test_get_available_themes_backward_compat(self):
        from src.card.styles import get_available_themes
        themes = get_available_themes(include_dark=False)
        assert len(themes) >= 10


class TestUIText:
    def test_import_from_new_module(self):
        from src.card.ui_text import UI_TEXT
        assert len(UI_TEXT) >= 300  # at least 300 entries

    def test_backward_compat(self):
        from src.card.styles import UI_TEXT
        assert len(UI_TEXT) >= 300

    def test_contains_spec_entries(self):
        from src.card.ui_text import UI_TEXT
        # SPEC_UI_TEXT should be merged
        # Check for some known spec-related keys
        assert any("spec" in k.lower() or "review" in k.lower() for k in UI_TEXT)

    def test_contains_lock_entries(self):
        from src.card.ui_text import UI_TEXT
        # LOCK_UI_TEXT should be merged
        assert any("lock" in k.lower() for k in UI_TEXT)


class TestThresholds:
    def test_import_from_new_module(self):
        from src.card.thresholds import THRESHOLDS, TRUNCATION_LIMITS
        assert isinstance(THRESHOLDS, dict)
        assert isinstance(TRUNCATION_LIMITS, dict)
        assert len(THRESHOLDS) >= 10

    def test_backward_compat(self):
        from src.card.styles import THRESHOLDS, TRUNCATION_LIMITS
        assert isinstance(THRESHOLDS, dict)


class TestButtonsConfig:
    def test_import_from_new_module(self):
        from src.card.buttons_config import BUTTON_CONFIG
        assert isinstance(BUTTON_CONFIG, dict)
        assert len(BUTTON_CONFIG) >= 10

    def test_backward_compat(self):
        from src.card.styles import BUTTON_CONFIG
        assert isinstance(BUTTON_CONFIG, dict)


class TestTerminal:
    def test_import_from_new_module(self):
        from src.card.terminal import TERMINAL_MARKERS, FOOTER_STATUS
        assert isinstance(TERMINAL_MARKERS, dict)
        assert isinstance(FOOTER_STATUS, dict)
        assert "completed" in TERMINAL_MARKERS or "success" in TERMINAL_MARKERS

    def test_backward_compat(self):
        from src.card.styles import TERMINAL_MARKERS, FOOTER_STATUS
        assert isinstance(TERMINAL_MARKERS, dict)

    def test_status_display_map(self):
        from src.card.terminal import STATUS_DISPLAY_MAP
        assert isinstance(STATUS_DISPLAY_MAP, dict)

    def test_status_display_map_backward_compat(self):
        from src.card.styles import STATUS_DISPLAY_MAP
        assert isinstance(STATUS_DISPLAY_MAP, dict)
