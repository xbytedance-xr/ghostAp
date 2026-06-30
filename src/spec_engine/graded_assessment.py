"""Graded Completion Assessment model for Spec Engine.

Replaces the binary PASS/FAIL per-criterion approach with a multi-dimensional
graded evaluation. Designed to be resistant to LLM optimism and sensitive to
regressions.

Evaluation Dimensions:
  1. Functional Completeness - Does the code do what was asked?
  2. Implementation Quality  - Code structure, patterns, error handling
  3. Verification Confidence - Test coverage, verify_command results
  4. Goal Alignment          - Drift detection from original requirement
  5. Integration Health      - No regressions, builds clean

Per-Criterion Grading (5 levels with asymmetric scoring):
  NOT_STARTED(0.0) / PARTIAL(0.4) / IMPLEMENTED(0.7) / TESTED(0.85) / VERIFIED(1.0)

Key design decisions:
  - "IMPLEMENTED" alone caps at 0.7; you MUST have test/verification evidence to
    exceed that. This penalizes unverified LLM claims.
  - Regressions (grade going down) trigger a 1.5x penalty multiplier on the
    dimension score to make them highly visible.
  - Confidence decays exponentially when criteria are not re-verified across cycles.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CompletionGrade(Enum):
    """5-level grading scale for a single criterion."""

    NOT_STARTED = "not_started"
    PARTIAL = "partial"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    VERIFIED = "verified"


class Dimension(Enum):
    """Evaluation dimensions."""

    FUNCTIONAL_COMPLETENESS = "functional_completeness"
    IMPLEMENTATION_QUALITY = "implementation_quality"
    VERIFICATION_CONFIDENCE = "verification_confidence"
    GOAL_ALIGNMENT = "goal_alignment"
    INTEGRATION_HEALTH = "integration_health"


# ---------------------------------------------------------------------------
# Grade numeric mapping
# ---------------------------------------------------------------------------

GRADE_SCORES: dict[CompletionGrade, float] = {
    CompletionGrade.NOT_STARTED: 0.0,
    CompletionGrade.PARTIAL: 0.4,
    CompletionGrade.IMPLEMENTED: 0.7,
    CompletionGrade.TESTED: 0.85,
    CompletionGrade.VERIFIED: 1.0,
}

# Ordered for regression detection (index = severity level)
GRADE_ORDER: list[CompletionGrade] = [
    CompletionGrade.NOT_STARTED,
    CompletionGrade.PARTIAL,
    CompletionGrade.IMPLEMENTED,
    CompletionGrade.TESTED,
    CompletionGrade.VERIFIED,
]


# ---------------------------------------------------------------------------
# Configuration (tunable weights)
# ---------------------------------------------------------------------------


@dataclass
class AssessmentWeights:
    """Tunable weight configuration for the graded assessment.

    Dimension weights must sum to 1.0. The defaults encode the product
    priority: functional correctness matters most, followed by verification
    confidence (to penalize unverified claims), then quality/alignment/health.
    """

    # --- Dimension weights (MUST sum to 1.0) ---
    dimension_weights: dict[Dimension, float] = field(default_factory=lambda: {
        Dimension.FUNCTIONAL_COMPLETENESS: 0.35,
        Dimension.IMPLEMENTATION_QUALITY: 0.15,
        Dimension.VERIFICATION_CONFIDENCE: 0.25,
        Dimension.GOAL_ALIGNMENT: 0.15,
        Dimension.INTEGRATION_HEALTH: 0.10,
    })

    # --- Confidence decay ---
    # Half-life in cycles: after this many cycles without re-verification,
    # confidence drops to 50% of its value.
    decay_half_life_cycles: int = 3

    # Minimum confidence floor (never decays below this)
    decay_floor: float = 0.3

    # --- Regression penalty ---
    # Multiplier applied to the contribution of a regressed criterion.
    # > 1.0 means it REDUCES the dimension score below what the raw grade
    # alone would give.
    regression_penalty_multiplier: float = 1.5

    # --- Cold start ---
    # On first cycle, verification_confidence dimension is evaluated leniently:
    # IMPLEMENTED counts as 0.75 instead of 0.7 (small boost to avoid
    # penalizing the first pass before tests exist).
    cold_start_impl_boost: float = 0.05

    # Number of cycles considered "cold start" (lenient scoring)
    cold_start_cycles: int = 1

    def validate(self) -> None:
        total = sum(self.dimension_weights.values())
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"Dimension weights must sum to 1.0, got {total:.4f}"
            )


# ---------------------------------------------------------------------------
# Per-criterion grading record
# ---------------------------------------------------------------------------


@dataclass
class CriterionAssessment:
    """Graded assessment for a single acceptance criterion."""

    criterion_id: int
    criterion_text: str

    # Per-dimension grades
    grades: dict[Dimension, CompletionGrade] = field(default_factory=lambda: {
        d: CompletionGrade.NOT_STARTED for d in Dimension
    })

    # Cycle number when each dimension was last evaluated
    last_evaluated_at: dict[Dimension, int] = field(default_factory=dict)

    # Historical grade for regression detection (previous cycle's grades)
    previous_grades: dict[Dimension, CompletionGrade] = field(default_factory=dict)

    # Evidence strings for auditability
    evidence: dict[Dimension, str] = field(default_factory=dict)

    # Timestamp of last update
    updated_at: float = field(default_factory=time.time)

    def update_grade(
        self,
        dimension: Dimension,
        grade: CompletionGrade,
        cycle_number: int,
        evidence_text: str = "",
    ) -> None:
        """Update grade for a dimension, preserving history for regression detection."""
        old_grade = self.grades.get(dimension, CompletionGrade.NOT_STARTED)
        self.previous_grades[dimension] = old_grade
        self.grades[dimension] = grade
        self.last_evaluated_at[dimension] = cycle_number
        if evidence_text:
            self.evidence[dimension] = evidence_text
        self.updated_at = time.time()

    def has_regressed(self, dimension: Dimension) -> bool:
        """Check if the grade for a dimension has gone DOWN since last evaluation."""
        prev = self.previous_grades.get(dimension)
        if prev is None:
            return False
        curr = self.grades.get(dimension, CompletionGrade.NOT_STARTED)
        return GRADE_ORDER.index(curr) < GRADE_ORDER.index(prev)

    def effective_score(
        self,
        dimension: Dimension,
        current_cycle: int,
        weights: AssessmentWeights,
    ) -> float:
        """Compute effective score for one dimension of this criterion.

        Accounts for:
        - Base grade score
        - Confidence decay if not recently verified
        - Regression penalty
        - Cold start boost
        """
        grade = self.grades.get(dimension, CompletionGrade.NOT_STARTED)
        base_score = GRADE_SCORES[grade]

        # Cold start boost for verification_confidence
        if (
            dimension == Dimension.VERIFICATION_CONFIDENCE
            and current_cycle <= weights.cold_start_cycles
            and grade == CompletionGrade.IMPLEMENTED
        ):
            base_score += weights.cold_start_impl_boost

        # Confidence decay
        last_eval = self.last_evaluated_at.get(dimension, 0)
        cycles_since_eval = max(0, current_cycle - last_eval)
        decay_factor = self._compute_decay(
            cycles_since_eval, weights.decay_half_life_cycles, weights.decay_floor
        )
        decayed_score = base_score * decay_factor

        # Regression penalty
        if self.has_regressed(dimension):
            # Penalty: reduce contribution by the multiplier
            # E.g., if score=0.4 and penalty=1.5, effective = 0.4 / 1.5 = 0.267
            decayed_score = decayed_score / weights.regression_penalty_multiplier

        return min(1.0, max(0.0, decayed_score))

    @staticmethod
    def _compute_decay(cycles_elapsed: int, half_life: int, floor: float) -> float:
        """Exponential decay with floor.

        decay(n) = max(floor, 2^(-n/half_life))

        After half_life cycles -> 0.5
        After 2*half_life cycles -> 0.25 (but clamped to floor)
        """
        if cycles_elapsed <= 0:
            return 1.0
        raw_decay = math.pow(2.0, -cycles_elapsed / half_life)
        return max(floor, raw_decay)

    def to_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "criterion_text": self.criterion_text,
            "grades": {d.value: g.value for d, g in self.grades.items()},
            "last_evaluated_at": {d.value: c for d, c in self.last_evaluated_at.items()},
            "previous_grades": {d.value: g.value for d, g in self.previous_grades.items()},
            "evidence": {d.value: e for d, e in self.evidence.items()},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CriterionAssessment":
        grades = {
            Dimension(k): CompletionGrade(v)
            for k, v in data.get("grades", {}).items()
        }
        last_eval = {
            Dimension(k): int(v)
            for k, v in data.get("last_evaluated_at", {}).items()
        }
        prev_grades = {
            Dimension(k): CompletionGrade(v)
            for k, v in data.get("previous_grades", {}).items()
        }
        evidence = {
            Dimension(k): str(v)
            for k, v in data.get("evidence", {}).items()
        }
        return cls(
            criterion_id=int(data.get("criterion_id", 0)),
            criterion_text=str(data.get("criterion_text", "")),
            grades=grades,
            last_evaluated_at=last_eval,
            previous_grades=prev_grades,
            evidence=evidence,
            updated_at=float(data.get("updated_at", 0.0)),
        )


# ---------------------------------------------------------------------------
# Composite assessment result
# ---------------------------------------------------------------------------


@dataclass
class GradedAssessmentResult:
    """Composite result from evaluating all criteria across all dimensions."""

    # Per-criterion assessments
    assessments: list[CriterionAssessment] = field(default_factory=list)

    # Composite scores (computed)
    composite_score: float = 0.0
    dimension_scores: dict[Dimension, float] = field(default_factory=dict)

    # Metadata
    cycle_number: int = 0
    regression_count: int = 0
    unverified_count: int = 0  # criteria with no TESTED or VERIFIED grade

    # Termination signal
    termination_eligible: bool = False
    termination_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "composite_score": self.composite_score,
            "dimension_scores": {d.value: s for d, s in self.dimension_scores.items()},
            "cycle_number": self.cycle_number,
            "regression_count": self.regression_count,
            "unverified_count": self.unverified_count,
            "termination_eligible": self.termination_eligible,
            "termination_reason": self.termination_reason,
            "assessments": [a.to_dict() for a in self.assessments],
        }


# ---------------------------------------------------------------------------
# Aggregation Engine
# ---------------------------------------------------------------------------


class GradedAssessmentEngine:
    """Computes composite graded assessment from per-criterion evaluations.

    Aggregation Algorithm (pseudocode):
    ─────────────────────────────────────
    1. For each criterion C_i:
       a. For each dimension D_j:
          - Compute effective_score(D_j, current_cycle, weights)
            = grade_score * decay_factor / regression_penalty_if_applicable
       b. Compute criterion_score(C_i) = weighted_sum over D_j of effective_score

    2. Aggregate across criteria:
       - raw_composite = mean(criterion_scores)
       - Apply verification penalty:
         If unverified_ratio > 0.5: penalty = 0.9
         (caps composite below 0.9 when most criteria lack test evidence)
       - composite = raw_composite * verification_cap

    3. Termination check:
       - composite >= 0.92 AND regression_count == 0 AND unverified_count == 0
         -> termination_eligible
    ─────────────────────────────────────
    """

    def __init__(self, weights: Optional[AssessmentWeights] = None):
        self.weights = weights or AssessmentWeights()
        self.weights.validate()

    def compute(
        self,
        assessments: list[CriterionAssessment],
        current_cycle: int,
    ) -> GradedAssessmentResult:
        """Compute composite graded assessment."""
        if not assessments:
            return GradedAssessmentResult(cycle_number=current_cycle)

        criterion_scores: list[float] = []
        dimension_totals: dict[Dimension, list[float]] = {d: [] for d in Dimension}
        regression_count = 0
        unverified_count = 0

        for assessment in assessments:
            # Check if criterion has any verification evidence
            verification_grade = assessment.grades.get(
                Dimension.VERIFICATION_CONFIDENCE, CompletionGrade.NOT_STARTED
            )
            if GRADE_ORDER.index(verification_grade) < GRADE_ORDER.index(CompletionGrade.TESTED):
                unverified_count += 1

            # Compute per-dimension effective scores
            weighted_sum = 0.0
            for dim in Dimension:
                eff_score = assessment.effective_score(dim, current_cycle, self.weights)
                dim_weight = self.weights.dimension_weights[dim]
                weighted_sum += eff_score * dim_weight
                dimension_totals[dim].append(eff_score)

                # Count regressions
                if assessment.has_regressed(dim):
                    regression_count += 1

            criterion_scores.append(weighted_sum)

        # Raw composite: mean of weighted criterion scores
        raw_composite = sum(criterion_scores) / len(criterion_scores)

        # Verification penalty cap: if >50% criteria are unverified,
        # cap the composite to prevent LLM optimism from inflating scores
        unverified_ratio = unverified_count / len(assessments)
        if unverified_ratio > 0.5:
            verification_cap = 0.7 + (1.0 - unverified_ratio) * 0.6
            # At 100% unverified: cap = 0.7
            # At  50% unverified: cap = 1.0 (no penalty)
        else:
            verification_cap = 1.0

        composite = min(1.0, raw_composite * verification_cap)

        # Dimension-level averages for reporting
        dimension_scores = {
            dim: (sum(scores) / len(scores) if scores else 0.0)
            for dim, scores in dimension_totals.items()
        }

        # Termination eligibility
        termination_eligible = (
            composite >= 0.92
            and regression_count == 0
            and unverified_count == 0
        )
        termination_reason = ""
        if termination_eligible:
            termination_reason = "All criteria verified with high composite score"
        elif composite >= 0.92 and unverified_count > 0:
            termination_reason = (
                f"High composite but {unverified_count} criteria lack verification"
            )

        return GradedAssessmentResult(
            assessments=assessments,
            composite_score=composite,
            dimension_scores=dimension_scores,
            cycle_number=current_cycle,
            regression_count=regression_count,
            unverified_count=unverified_count,
            termination_eligible=termination_eligible,
            termination_reason=termination_reason,
        )

    def compute_goal_attainment(
        self,
        assessments: list[CriterionAssessment],
        current_cycle: int,
    ) -> float:
        """Drop-in replacement for the legacy goal_attainment float.

        Returns a value in [0.0, 1.0] compatible with existing reporting.
        """
        result = self.compute(assessments, current_cycle)
        return result.composite_score


# ---------------------------------------------------------------------------
# Bridge: Convert legacy binary tracker to graded assessments
# ---------------------------------------------------------------------------


def from_legacy_tracker(
    criteria: list[str],
    satisfied: dict[int, bool],
    current_cycle: int,
) -> list[CriterionAssessment]:
    """Convert a binary CriteriaTracker state into graded assessments.

    Used during cold start / migration from the old system:
    - PASS  -> IMPLEMENTED grade across functional + goal_alignment
    - FAIL  -> NOT_STARTED
    - No verification_confidence or quality data (set to NOT_STARTED)

    This intentionally under-scores legacy data to incentivize re-evaluation.
    """
    assessments = []
    for idx, text in enumerate(criteria):
        is_satisfied = satisfied.get(idx, False)
        assessment = CriterionAssessment(
            criterion_id=idx,
            criterion_text=text,
        )
        if is_satisfied:
            base_grade = CompletionGrade.IMPLEMENTED
            assessment.grades[Dimension.FUNCTIONAL_COMPLETENESS] = base_grade
            assessment.grades[Dimension.GOAL_ALIGNMENT] = base_grade
            assessment.grades[Dimension.IMPLEMENTATION_QUALITY] = CompletionGrade.PARTIAL
            assessment.grades[Dimension.VERIFICATION_CONFIDENCE] = CompletionGrade.NOT_STARTED
            assessment.grades[Dimension.INTEGRATION_HEALTH] = CompletionGrade.PARTIAL
        else:
            for dim in Dimension:
                assessment.grades[dim] = CompletionGrade.NOT_STARTED

        # Mark evaluation cycle
        for dim in Dimension:
            assessment.last_evaluated_at[dim] = current_cycle

        assessments.append(assessment)
    return assessments
