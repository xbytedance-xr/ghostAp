"""ACP (Agent Client Protocol) integration layer for GhostAP.

Provides structured communication with AI agents (Coco/Claude) via JSON-RPC 2.0
over stdio, replacing the previous subprocess-based CLI interaction.
"""

from .models import (
    ACPEvent,
    ACPEventType,
    ACPSessionState,
    PlanEntryInfo,
    PlanInfo,
    PromptResult,
    ToolCallInfo,
)
from .client import GhostAPClient
from .session import ACPSession
from .sync_adapter import SyncACPSession, start_session_with_retry
from .renderer import ACPEventRenderer

__all__ = [
    "ACPEvent",
    "ACPEventType",
    "ACPSessionState",
    "PlanEntryInfo",
    "PlanInfo",
    "PromptResult",
    "ToolCallInfo",
    "GhostAPClient",
    "ACPSession",
    "SyncACPSession",
    "start_session_with_retry",
    "ACPEventRenderer",
]
