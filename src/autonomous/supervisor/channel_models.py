"""Dependency-light shared contracts for parent-owned employee Channels."""

from __future__ import annotations

from enum import Enum


class ChannelProcessState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    CRASHED = "crashed"


__all__ = ["ChannelProcessState"]
