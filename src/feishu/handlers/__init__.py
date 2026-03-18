"""Feishu message handlers — extracted from the monolithic FeishuWSClient."""

from .base import BaseHandler
from .deep import DeepHandler
from .diagnostics import DiagnosticsHandler
from .loop import LoopHandler
from .programming import ClaudeModeHandler, CocoModeHandler, ProgrammingModeHandler, TTADKModeHandler
from .project import ProjectHandler
from .spec import SpecHandler
from .system import SystemHandler

__all__ = [
    "BaseHandler",
    "ProgrammingModeHandler",
    "CocoModeHandler",
    "ClaudeModeHandler",
    "TTADKModeHandler",
    "DeepHandler",
    "LoopHandler",
    "SpecHandler",
    "ProjectHandler",
    "SystemHandler",
    "DiagnosticsHandler",
]
