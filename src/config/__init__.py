"""Configuration package — split from monolithic config.py for maintainability.

All public names are re-exported here so existing ``from src.config import …``
statements continue to work without modification.
"""

from .card import CardSessionConfig
from .errors import ConfigurationError
from .settings import Settings
from .singleton import (
    _post_validate_warnings,
    _reset_settings_for_testing,
    get_settings,
    set_settings,
)
from .spec import SpecReviewConfig

__all__ = [
    "CardSessionConfig",
    "ConfigurationError",
    "Settings",
    "SpecReviewConfig",
    "get_settings",
    "set_settings",
    "_post_validate_warnings",
    "_reset_settings_for_testing",
]
