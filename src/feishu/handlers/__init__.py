"""Feishu message handlers — extracted from the monolithic FeishuWSClient."""

from .base import BaseHandler
from .programming import ProgrammingModeHandler, CocoModeHandler, ClaudeModeHandler, TTADKModeHandler
from .deep import DeepHandler
from .loop import LoopHandler
from .spec import SpecHandler
from .project import ProjectHandler
from .system import SystemHandler
from .diagnostics import DiagnosticsHandler

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
