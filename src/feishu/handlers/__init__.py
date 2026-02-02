"""Feishu message handlers — extracted from the monolithic FeishuWSClient."""

from .base import BaseHandler
from .programming import ProgrammingModeHandler, CocoModeHandler, ClaudeModeHandler
from .deep import DeepHandler
from .project import ProjectHandler
from .system import SystemHandler
from .diagnostics import DiagnosticsHandler

__all__ = [
    "BaseHandler",
    "ProgrammingModeHandler",
    "CocoModeHandler",
    "ClaudeModeHandler",
    "DeepHandler",
    "ProjectHandler",
    "SystemHandler",
    "DiagnosticsHandler",
]
