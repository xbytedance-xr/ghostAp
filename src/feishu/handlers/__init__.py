"""Feishu message handlers — extracted from the monolithic FeishuWSClient."""

from .base import BaseHandler
from .deep import DeepHandler
from .diagnostics import DiagnosticsHandler
from .programming import (
    AidenModeHandler,
    ClaudeModeHandler,
    CocoModeHandler,
    CodexModeHandler,
    GeminiModeHandler,
    ProgrammingModeHandler,
    TTADKModeHandler,
    TraexModeHandler,
    Tui2acpModeHandler,
)
from .project import ProjectHandler
from .slock import SlockHandler
from .spec import SpecHandler
from .system import SystemHandler

__all__ = [
    "BaseHandler",
    "ProgrammingModeHandler",
    "CocoModeHandler",
    "ClaudeModeHandler",
    "AidenModeHandler",
    "CodexModeHandler",
    "GeminiModeHandler",
    "TraexModeHandler",
    "TTADKModeHandler",
    "Tui2acpModeHandler",
    "DeepHandler",
    "SpecHandler",
    "SlockHandler",
    "ProjectHandler",
    "SystemHandler",
    "DiagnosticsHandler",
]
