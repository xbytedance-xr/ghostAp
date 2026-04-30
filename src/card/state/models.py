"""Immutable card state dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


TerminalStatus = Literal[
    "running", "completed", "failed", "cancelled", "paused", "awaiting_approval"
]

BlockStatus = Literal["active", "completed", "failed"]


@dataclass(frozen=True)
class CardMetadata:
    """Project/tool/model metadata for the card."""
    project_name: str | None = None
    mode_name: str = "Coco"
    mode_emoji: str = "🤖"
    tool_name: str | None = None
    model_name: str | None = None
    engine_type: str | None = None  # "deep" / "loop" / "spec" / None


@dataclass(frozen=True)
class HeaderState:
    """Card header state."""
    title: str = ""
    subtitle: str | None = None
    template: str = "blue"


@dataclass(frozen=True)
class FooterState:
    """Card footer state."""
    status: Literal["thinking", "tool_running", "waiting_approval", "idle"] | None = None
    status_text: str | None = None
    progress: str | None = None


@dataclass(frozen=True)
class ButtonSpec:
    """Button specification."""
    text: str = ""
    action_id: str = ""
    type: Literal["primary", "default", "danger"] = "default"
    confirm: str | None = None


@dataclass(frozen=True)
class ContentBlock:
    """Indivisible content atom in the card."""
    kind: Literal["text", "tool_call", "reasoning", "plan"] = "text"
    block_id: str = ""
    content: str = ""
    element_id: str | None = None
    status: BlockStatus = "active"
    # tool_call specific
    tool_name: str | None = None
    tool_summary: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    # reasoning specific
    char_count: int = 0


@dataclass(frozen=True)
class CardState:
    """Top-level immutable card state produced by reducer."""
    blocks: tuple[ContentBlock, ...] = ()
    terminal: TerminalStatus = "running"
    header: HeaderState = field(default_factory=HeaderState)
    footer: FooterState = field(default_factory=FooterState)
    buttons: tuple[ButtonSpec, ...] = ()
    metadata: CardMetadata = field(default_factory=CardMetadata)
    version: int = 0
