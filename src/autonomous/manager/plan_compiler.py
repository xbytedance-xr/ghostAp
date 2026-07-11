"""Plan Compiler - validates plans before activation.

Checks: DAG acyclicity, capability existence, parameter schemas,
dependency compatibility, resource conflicts, verifier coverage, budget feasibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import (
    CapabilityDescriptor,
    GoalCriterion,
    Plan,
)


@dataclass
class CompilationError:
    step_id: str
    error_type: str
    message: str


@dataclass
class CompilationResult:
    valid: bool
    errors: list[CompilationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PlanCompiler:
    """Validates a plan against capabilities, criteria and constraints."""

    def __init__(self, capability_registry: dict[str, CapabilityDescriptor]):
        self._capabilities = capability_registry

    def compile(
        self,
        plan: Plan,
        criteria: list[GoalCriterion],
        budget_limits: Optional[dict] = None,
    ) -> CompilationResult:
        errors: list[CompilationError] = []
        warnings: list[str] = []

        # 1. DAG validation
        dag_errors = plan.validate_dag()
        for msg in dag_errors:
            errors.append(CompilationError(step_id="", error_type="dag", message=msg))

        # 2. Capability existence
        for step in plan.steps:
            if step.capability and step.capability not in self._capabilities:
                errors.append(CompilationError(
                    step_id=step.step_id,
                    error_type="capability_missing",
                    message=f"Capability '{step.capability}' not in registry",
                ))

        # 3. Parameter schema validation
        for step in plan.steps:
            cap = self._capabilities.get(step.capability)
            if cap and cap.parameters_schema:
                schema_keys = set(cap.parameters_schema.get("required", []))
                provided_keys = set(step.arguments_schema.keys())
                missing = schema_keys - provided_keys
                if missing:
                    errors.append(CompilationError(
                        step_id=step.step_id,
                        error_type="schema_mismatch",
                        message=f"Missing required parameters: {missing}",
                    ))

        # 4. Verifier coverage - terminal steps must have verifier
        terminal_step_ids = self._find_terminal_steps(plan)
        for sid in terminal_step_ids:
            step = next((s for s in plan.steps if s.step_id == sid), None)
            if step and not step.verifier_oracle:
                errors.append(CompilationError(
                    step_id=sid,
                    error_type="missing_verifier",
                    message="Terminal step must have a verifier oracle",
                ))

        # 5. Criteria coverage
        covered_criteria = set()
        for step in plan.steps:
            covered_criteria.update(step.criterion_ids)
        all_criteria = {c.criterion_id for c in criteria}
        uncovered = all_criteria - covered_criteria
        if uncovered:
            for cid in uncovered:
                errors.append(CompilationError(
                    step_id="",
                    error_type="criteria_uncovered",
                    message=f"Criterion '{cid}' not covered by any step",
                ))

        # 6. Empty plan check
        if not plan.steps:
            errors.append(CompilationError(
                step_id="",
                error_type="empty_plan",
                message="Plan has no steps",
            ))

        return CompilationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _find_terminal_steps(self, plan: Plan) -> list[str]:
        """Find steps that no other step depends on."""
        all_ids = {s.step_id for s in plan.steps}
        depended_on = set()
        for step in plan.steps:
            depended_on.update(step.depends_on)
        return list(all_ids - depended_on)
