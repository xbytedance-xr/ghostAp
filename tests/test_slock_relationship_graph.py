"""Tests for relationship_graph.py — inter-agent collaboration memory."""
import json
import os
import tempfile
import pytest
from src.slock_engine.relationship_graph import RelationshipGraph, RelationshipEdge


@pytest.fixture
def graph(tmp_path):
    """Create a graph with temp storage."""
    path = str(tmp_path / "relationships.json")
    return RelationshipGraph(path)


class TestRelationshipEdge:
    def test_to_dict_roundtrip(self):
        """Serialization roundtrip preserves data."""
        edge = RelationshipEdge(
            agent_a="a1", agent_b="b1",
            interaction_count=5, avg_quality=75.0,
            trust_level=0.7, preferred_mode="casual",
            last_interaction_ts=1000.0,
        )
        restored = RelationshipEdge.from_dict(edge.to_dict())
        assert restored.agent_a == "a1"
        assert restored.interaction_count == 5
        assert restored.trust_level == 0.7


class TestRelationshipGraph:
    def test_record_and_trust_growth(self, graph):
        """Positive interactions grow trust."""
        for _ in range(5):
            graph.record_interaction("a", "b", quality=80.0)
        assert graph.get_trust("a", "b") > 0.5

    def test_trust_decay_on_low_quality(self, graph):
        """Low quality interactions decrease trust."""
        # Start with some positive interactions
        for _ in range(3):
            graph.record_interaction("a", "b", quality=80.0)
        initial_trust = graph.get_trust("a", "b")
        # Bad interaction
        graph.record_interaction("a", "b", quality=30.0)
        assert graph.get_trust("a", "b") < initial_trust

    def test_preferred_mode_evolution(self, graph):
        """Mode evolves from formal to brief with high trust."""
        assert graph.get_preferred_mode("a", "b") == "formal"
        # Build trust
        for _ in range(10):
            graph.record_interaction("a", "b", quality=90.0)
        mode = graph.get_preferred_mode("a", "b")
        assert mode in ("casual", "brief")

    def test_symmetric_lookup(self, graph):
        """Trust is symmetric regardless of query order."""
        graph.record_interaction("a", "b", quality=80.0)
        assert graph.get_trust("a", "b") == graph.get_trust("b", "a")

    def test_unknown_pair_defaults(self, graph):
        """Unknown pairs return default values."""
        assert graph.get_trust("x", "y") == 0.5
        assert graph.get_preferred_mode("x", "y") == "formal"
        assert graph.get_interaction_context("x", "y") == ""

    def test_interaction_context_after_multiple(self, graph):
        """Context hint appears after 2+ interactions."""
        graph.record_interaction("a", "b", quality=80.0)
        assert graph.get_interaction_context("a", "b") == ""  # Only 1 interaction
        graph.record_interaction("a", "b", quality=80.0)
        ctx = graph.get_interaction_context("a", "b")
        assert "协作过 2 次" in ctx
        assert "信任度" in ctx

    def test_rank_partners(self, graph):
        """Partners ranked by trust descending."""
        graph.record_interaction("a", "b", quality=90.0)
        graph.record_interaction("a", "b", quality=90.0)
        graph.record_interaction("a", "c", quality=30.0)
        ranked = graph.rank_partners("a", ["b", "c", "d"])
        assert ranked[0] == "b"  # Highest trust

    def test_self_interaction_ignored(self, graph):
        """Self-interaction is silently ignored."""
        graph.record_interaction("a", "a", quality=100.0)
        assert graph.get_trust("a", "a") == 0.5  # Default, not recorded

    def test_persistence_roundtrip(self, tmp_path):
        """Data survives reload from disk."""
        path = str(tmp_path / "rel.json")
        g1 = RelationshipGraph(path)
        g1.record_interaction("a", "b", quality=80.0)
        g1.record_interaction("a", "b", quality=80.0)

        # Reload from same path
        g2 = RelationshipGraph(path)
        assert g2.get_trust("a", "b") > 0.5
        assert g2.get_interaction_context("a", "b") != ""
