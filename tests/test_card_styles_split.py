"""Tests for split card style modules."""


class TestThemes:
    def test_import_from_new_module(self):
        from src.card.themes import DARK_THEME_NAMES, ENGINE_STYLES, THEMES, ProjectTheme, get_theme
        assert len(THEMES) >= 18
        assert isinstance(DARK_THEME_NAMES, (set, frozenset))
        assert "default" in ENGINE_STYLES
        theme = get_theme("blue")
        assert isinstance(theme, ProjectTheme)

    def test_get_available_themes(self):
        from src.card.themes import DARK_THEME_NAMES, get_available_themes
        light_themes = get_available_themes(include_dark=False)
        all_themes = get_available_themes(include_dark=True)
        assert len(all_themes) >= 18
        assert len(light_themes) < len(all_themes)
        for dark_name in DARK_THEME_NAMES:
            assert dark_name not in light_themes


class TestUIText:
    def test_import_from_new_module(self):
        from src.card.ui_text import UI_TEXT
        assert len(UI_TEXT) >= 300  # at least 300 entries

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


class TestButtonsConfig:
    def test_import_from_new_module(self):
        from src.card.buttons_config import BUTTON_CONFIG
        assert isinstance(BUTTON_CONFIG, dict)
        assert len(BUTTON_CONFIG) >= 10


class TestTerminal:
    def test_import_from_new_module(self):
        from src.card.terminal import FOOTER_STATUS, TERMINAL_MARKERS
        assert isinstance(TERMINAL_MARKERS, dict)
        assert isinstance(FOOTER_STATUS, dict)
        assert "completed" in TERMINAL_MARKERS or "success" in TERMINAL_MARKERS

    def test_status_display_map(self):
        from src.card.terminal import STATUS_DISPLAY_MAP
        assert isinstance(STATUS_DISPLAY_MAP, dict)
