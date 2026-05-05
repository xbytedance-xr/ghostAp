"""Backward-compatible re-export layer. Import from specific modules instead.

- themes.py: ProjectTheme, THEMES, DARK_THEME_NAMES, ENGINE_STYLES, PANEL_STYLES, get_theme, get_available_themes
- ui_text.py: UI_TEXT
- thresholds.py: THRESHOLDS, TRUNCATION_LIMITS
- buttons_config.py: BUTTON_CONFIG
- terminal.py: TERMINAL_MARKERS, FOOTER_STATUS, STATUS_DISPLAY_MAP
"""
from .themes import *  # noqa: F401, F403
from .themes import get_available_themes, get_theme  # noqa: F401
from .thresholds import *  # noqa: F401
from .buttons_config import *  # noqa: F401
from .terminal import *  # noqa: F401

# PEP 562: lazy deprecation warning for UI_TEXT access via this module
_UI_TEXT = None


def __getattr__(name: str):
    if name == "UI_TEXT":
        import warnings
        warnings.warn(
            "Importing UI_TEXT from src.card.styles is deprecated, "
            "use 'from src.card.ui_text import UI_TEXT' directly. "
            "This shim will be removed after 2026-06-01 (removal: 2026-06-01).",
            DeprecationWarning,
            stacklevel=2,
        )
        from .ui_text import UI_TEXT
        return UI_TEXT
    raise AttributeError(f"module 'src.card.styles' has no attribute {name!r}")
