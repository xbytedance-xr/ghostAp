"""Structural tests for src.card.delivery.types — MutationOutcome dataclass invariants."""

import dataclasses

import pytest

from src.card.delivery.types import MutationOutcome


class TestMutationOutcomeStructure:
    """Verify MutationOutcome frozen dataclass behavior and field constraints."""

    def test_is_frozen_dataclass(self):
        outcome = MutationOutcome(kind="applied")
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.kind = "rejected"  # type: ignore[misc]

    def test_kind_applied(self):
        o = MutationOutcome(kind="applied", message="ok")
        assert o.kind == "applied"
        assert o.message == "ok"

    def test_kind_reconcile(self):
        o = MutationOutcome(kind="reconcile", message="retry")
        assert o.kind == "reconcile"

    def test_kind_skipped(self):
        o = MutationOutcome(kind="skipped")
        assert o.kind == "skipped"

    def test_kind_rejected(self):
        o = MutationOutcome(kind="rejected", message="capacity exhausted")
        assert o.kind == "rejected"

    def test_default_message_is_empty(self):
        o = MutationOutcome(kind="applied")
        assert o.message == ""

    def test_all_four_kinds_are_valid(self):
        """Exhaustiveness: all four kinds should be constructible without error."""
        kinds = ("applied", "reconcile", "skipped", "rejected")
        for kind in kinds:
            o = MutationOutcome(kind=kind)
            assert o.kind == kind
