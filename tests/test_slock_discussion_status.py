"""Tests for DiscussionStatus.MAX_ROUNDS_REACHED — AC-R03.

Verifies:
- MAX_ROUNDS_REACHED exists in DiscussionStatus enum
- Serialization/deserialization round-trip is stable
- Distinct from TIMEOUT and CONVERGED
"""

from __future__ import annotations

from src.slock_engine.models import DiscussionStatus


class TestMaxRoundsReachedStatus:
    """AC-R03: MAX_ROUNDS_REACHED 与 TIMEOUT 区分。"""

    def test_max_rounds_reached_exists(self):
        """Enum member MAX_ROUNDS_REACHED is defined."""
        assert hasattr(DiscussionStatus, "MAX_ROUNDS_REACHED")
        assert DiscussionStatus.MAX_ROUNDS_REACHED.value == "max_rounds_reached"

    def test_distinct_from_timeout(self):
        """MAX_ROUNDS_REACHED is not the same as TIMEOUT."""
        assert DiscussionStatus.MAX_ROUNDS_REACHED != DiscussionStatus.TIMEOUT

    def test_distinct_from_converged(self):
        """MAX_ROUNDS_REACHED is not the same as CONVERGED."""
        assert DiscussionStatus.MAX_ROUNDS_REACHED != DiscussionStatus.CONVERGED

    def test_serialization_roundtrip(self):
        """Serialize to string and back."""
        status = DiscussionStatus.MAX_ROUNDS_REACHED
        serialized = status.value
        assert serialized == "max_rounds_reached"
        deserialized = DiscussionStatus(serialized)
        assert deserialized == DiscussionStatus.MAX_ROUNDS_REACHED

    def test_all_terminal_statuses_distinct(self):
        """All terminal statuses have unique values."""
        terminal = [
            DiscussionStatus.CONVERGED,
            DiscussionStatus.TIMEOUT,
            DiscussionStatus.MAX_ROUNDS_REACHED,
            DiscussionStatus.MANUALLY_STOPPED,
        ]
        values = [s.value for s in terminal]
        assert len(values) == len(set(values)), "Terminal status values must be unique"
