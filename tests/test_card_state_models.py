"""Stable defaults and immutability contracts for card state models."""

import dataclasses
from dataclasses import replace

import pytest

from src.card.state.models import CardMetadata, CardState
from src.card.state.runtime_stats import RuntimeStats


def test_card_state_defaults() -> None:
    state = CardState()

    assert state.blocks == ()
    assert state.terminal == "running"
    assert state.version == 0


def test_card_state_changes_use_replace() -> None:
    state = CardState()

    with pytest.raises(dataclasses.FrozenInstanceError):
        state.version = 99  # type: ignore[misc]

    updated = replace(state, version=1)
    assert (state.version, updated.version) == (0, 1)


def test_card_metadata_user_visible_defaults() -> None:
    metadata = CardMetadata()

    assert metadata.mode_name == "Coco"
    assert metadata.mode_emoji == "🤖"
    assert metadata.engine_type is None


def test_runtime_stats_defaults() -> None:
    stats = RuntimeStats()

    assert stats.elapsed_seconds == 0.0
    assert stats.deep_phase is None
    assert stats.spec_cycle is None
    assert stats.spec_perspective is None
    assert stats.worktree_subagent is None


def test_runtime_stats_is_immutable() -> None:
    stats = RuntimeStats()

    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.elapsed_seconds = 999.0  # type: ignore[misc]
