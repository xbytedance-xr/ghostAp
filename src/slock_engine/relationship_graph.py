"""Relationship Graph — tracks inter-agent collaboration history.

Maintains per-pair records of interaction quality, trust level,
and communication preferences. Fed into partner selection and
prompt context injection.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RelationshipEdge:
    agent_a: str  # Lexicographically smaller ID
    agent_b: str  # Lexicographically larger ID
    interaction_count: int = 0
    avg_quality: float = 50.0  # 0-100 exponential moving average
    trust_level: float = 0.5  # 0.0-1.0
    preferred_mode: str = "formal"  # formal | brief | casual
    last_interaction_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "agent_a": self.agent_a,
            "agent_b": self.agent_b,
            "interaction_count": self.interaction_count,
            "avg_quality": round(self.avg_quality, 1),
            "trust_level": round(self.trust_level, 3),
            "preferred_mode": self.preferred_mode,
            "last_interaction_ts": self.last_interaction_ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RelationshipEdge":
        return cls(
            agent_a=data.get("agent_a", ""),
            agent_b=data.get("agent_b", ""),
            interaction_count=data.get("interaction_count", 0),
            avg_quality=data.get("avg_quality", 50.0),
            trust_level=data.get("trust_level", 0.5),
            preferred_mode=data.get("preferred_mode", "formal"),
            last_interaction_ts=data.get("last_interaction_ts", 0.0),
        )


class RelationshipGraph:
    """Per-channel relationship graph with JSON persistence."""

    def __init__(self, storage_path: str) -> None:
        self._path = storage_path
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._edges: dict[tuple[str, str], RelationshipEdge] = {}
        self._dirty = False
        self._load()

    def _edge_key(self, a: str, b: str) -> tuple[str, str]:
        return (min(a, b), max(a, b))

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("edges", []):
                edge = RelationshipEdge.from_dict(entry)
                key = (edge.agent_a, edge.agent_b)
                self._edges[key] = edge
        except Exception as e:
            logger.warning("Failed to load relationship graph: %s", repr(e))

    def _persist(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {"edges": [e.to_dict() for e in self._edges.values()]}
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as e:
            logger.warning("Failed to persist relationship graph: %s", repr(e))

    def record_interaction(
        self, agent_a: str, agent_b: str, *, quality: float, context: str = ""
    ) -> None:
        """Record a collaboration event between two agents."""
        if agent_a == agent_b:
            return
        with self._lock:
            key = self._edge_key(agent_a, agent_b)
            edge = self._edges.get(key)
            if edge is None:
                edge = RelationshipEdge(agent_a=key[0], agent_b=key[1])
                self._edges[key] = edge

            edge.interaction_count += 1
            # Exponential moving average
            edge.avg_quality = edge.avg_quality * 0.8 + quality * 0.2
            # Trust grows slowly with positive interactions
            trust_delta = 0.05 if quality >= 70 else -0.02 if quality < 40 else 0.0
            edge.trust_level = max(0.0, min(1.0, edge.trust_level + trust_delta))
            # Update preferred mode based on trust
            if edge.trust_level >= 0.7 and edge.interaction_count >= 3:
                edge.preferred_mode = "brief"
            elif edge.trust_level >= 0.5:
                edge.preferred_mode = "casual"
            edge.last_interaction_ts = time.time()
            self._dirty = True
            self._persist()

    def get_trust(self, agent_a: str, agent_b: str) -> float:
        """Get trust level between two agents. Returns 0.5 for unknown pairs."""
        with self._lock:
            edge = self._edges.get(self._edge_key(agent_a, agent_b))
            return edge.trust_level if edge else 0.5

    def get_preferred_mode(self, agent_a: str, agent_b: str) -> str:
        """Get preferred communication mode. Returns 'formal' for unknown pairs."""
        with self._lock:
            edge = self._edges.get(self._edge_key(agent_a, agent_b))
            return edge.preferred_mode if edge else "formal"

    def get_interaction_context(self, agent_a: str, agent_b: str) -> str:
        """Return a prompt-injectable hint about the working relationship."""
        with self._lock:
            edge = self._edges.get(self._edge_key(agent_a, agent_b))
        if not edge or edge.interaction_count < 2:
            return ""
        trust_label = "高" if edge.trust_level >= 0.7 else "中" if edge.trust_level >= 0.4 else "低"
        return (
            f"(你与此队友协作过 {edge.interaction_count} 次，"
            f"信任度: {trust_label}，偏好模式: {edge.preferred_mode})"
        )

    def rank_partners(self, agent_id: str, candidates: list[str]) -> list[str]:
        """Sort candidates by trust score descending."""
        with self._lock:
            scored = []
            for c in candidates:
                edge = self._edges.get(self._edge_key(agent_id, c))
                trust = edge.trust_level if edge else 0.5
                recency = edge.last_interaction_ts if edge else 0.0
                scored.append((c, trust, recency))
        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return [s[0] for s in scored]
