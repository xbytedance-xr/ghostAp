"""Aggregate adaptive role review outputs into repair guidance."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class RoleSuggestion:
    severity: str
    confidence: str
    evidence: str
    recommendation: str
    target: str = ""
    blocking: bool = False

    def normalized_key(self) -> str:
        return " ".join((self.recommendation or "").strip().lower().split())

    def format_for_repair(self, role_name: str) -> str:
        parts = [f"[{role_name}] {self.recommendation}"]
        evidence_bits = []
        if self.evidence:
            evidence_bits.append(f"evidence: {self.evidence}")
        if self.target:
            evidence_bits.append(f"target: {self.target}")
        if evidence_bits:
            parts.append(f"({'; '.join(evidence_bits)})")
        return " ".join(parts)


@dataclass
class RoleReviewOutcome:
    role_id: str
    role_display_name: str
    role_category: str
    passed: bool
    summary: str = ""
    suggestions: list[RoleSuggestion] = field(default_factory=list)
    raw_preview: str = ""
    error: str = ""
    blocking: bool = True
    skipped: bool = False
    base_perspective_value: str = ""
    # Completion gate fields (only populated for completion_control role)
    goal_verdict: str = ""
    goal_confidence: str = ""
    goal_evidence: str = ""


@dataclass
class AggregatedSuggestion:
    suggestion_id: str
    severity: str
    confidence: str
    role_ids: list[str]
    evidence: list[str]
    recommendation: str
    target: str = ""
    blocking: bool = False

    def to_repair_text(self, role_names: dict[str, str]) -> str:
        names = ", ".join(role_names.get(role_id, role_id) for role_id in self.role_ids)
        parts = [f"[{names}] {self.recommendation}"]
        evidence_bits = []
        if self.evidence:
            evidence_bits.append(f"evidence: {' | '.join(self.evidence)}")
        if self.target:
            evidence_bits.append(f"target: {self.target}")
        if evidence_bits:
            parts.append(f"({'; '.join(evidence_bits)})")
        return " ".join(parts)


@dataclass
class AggregatedReview:
    blocking_suggestions: list[AggregatedSuggestion]
    observations: list[AggregatedSuggestion]
    role_names: dict[str, str]

    def blocking_hash(self) -> str:
        if not self.blocking_suggestions:
            return ""
        payload = [
            {
                "recommendation": item.recommendation,
                "roles": item.role_ids,
                "target": item.target,
                "severity": item.severity,
            }
            for item in self.blocking_suggestions
        ]
        raw = repr(payload).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]


def _severity_rank(value: str) -> int:
    return {"blocker": 3, "major": 2, "minor": 1, "observation": 0}.get(value, 0)


def aggregate_role_outcomes(outcomes: list[RoleReviewOutcome]) -> AggregatedReview:
    """Deduplicate and split blocking suggestions from observations."""

    role_names = {outcome.role_id: outcome.role_display_name for outcome in outcomes}
    by_key: dict[str, AggregatedSuggestion] = {}
    observations: dict[str, AggregatedSuggestion] = {}

    for outcome in outcomes:
        for suggestion in outcome.suggestions:
            key = suggestion.normalized_key()
            if not key:
                continue
            target = by_key if suggestion.blocking else observations
            existing = target.get(key)
            if existing is None:
                target[key] = AggregatedSuggestion(
                    suggestion_id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                    severity=suggestion.severity,
                    confidence=suggestion.confidence,
                    role_ids=[outcome.role_id],
                    evidence=[suggestion.evidence] if suggestion.evidence else [],
                    recommendation=suggestion.recommendation,
                    target=suggestion.target,
                    blocking=suggestion.blocking,
                )
                continue
            if outcome.role_id not in existing.role_ids:
                existing.role_ids.append(outcome.role_id)
            if suggestion.evidence and suggestion.evidence not in existing.evidence:
                existing.evidence.append(suggestion.evidence)
            if _severity_rank(suggestion.severity) > _severity_rank(existing.severity):
                existing.severity = suggestion.severity
            if not existing.target and suggestion.target:
                existing.target = suggestion.target

    return AggregatedReview(
        blocking_suggestions=list(by_key.values()),
        observations=list(observations.values()),
        role_names=role_names,
    )
