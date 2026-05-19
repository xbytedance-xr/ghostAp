"""Adaptive role review pipeline for Spec Engine."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from src.engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from src.spec_engine.review_aggregation import (
    AggregatedReview,
    RoleReviewOutcome,
    RoleSuggestion,
    aggregate_role_outcomes,
)
from src.spec_engine.review_artifacts import ReviewArtifacts
from src.spec_engine.review_roles import ReviewRoleSpec, batch_roles_by_dependencies
from src.utils.errors import get_error_detail

logger = logging.getLogger(__name__)

PromptRunner = Callable[[str, Callable, float], str]
PromptRunnerFactory = Callable[[ReviewRoleSpec], PromptRunner]


@dataclass
class AdaptiveReviewResult(ReviewResult):
    role_outcomes: list[RoleReviewOutcome] = field(default_factory=list)
    aggregated: AggregatedReview | None = None
    role_plan_hash: str = ""
    blocking_suggestion_hash: str = ""
    blocking_review_passed: bool = False


def build_role_review_prompt(role: ReviewRoleSpec, artifacts: ReviewArtifacts) -> str:
    files = "\n".join(f"- {path}" for path in (artifacts.touched_files or [])[:50])
    diff = artifacts.diff_patch or ""
    if len(diff) > 40_000:
        diff = diff[:40_000] + "\n...[truncated]"
    return f"""你是 {role.display_name}。

## 任务目标
{artifacts.requirement}

## 角色任务
{role.mission}

## 关注点
{chr(10).join(f"- {x}" for x in role.review_focus)}

## 必查项
{chr(10).join(f"- {x}" for x in role.must_check)}

## 证据规则
{role.evidence_policy}

## 涉及文件
{files}

## Diff
```diff
{diff}
```

请只输出 JSON:
{{
  "role_id": "{role.role_id}",
  "verdict": "PASS|FAIL",
  "summary": "short summary",
  "suggestions": [
    {{
      "severity": "blocker|major|minor|observation",
      "confidence": "high|medium|low",
      "evidence": "artifact evidence",
      "recommendation": "specific change",
      "target": "optional file or section"
    }}
  ]
}}
"""


def _extract_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def parse_role_review_output(role: ReviewRoleSpec, raw: str) -> RoleReviewOutcome:
    """Parse model output for a role, applying evidence downgrade rules."""

    try:
        data = _extract_json_object(raw)
    except Exception as exc:
        return RoleReviewOutcome(
            role_id=role.role_id,
            role_display_name=role.display_name,
            role_category=role.category,
            passed=True,
            summary="parse failure (infra, non-blocking)",
            suggestions=[
                RoleSuggestion(
                    severity="observation",
                    confidence="low",
                    evidence="role output was not valid JSON",
                    recommendation=f"{role.display_name} 审查输出无法解析，请重跑该角色审查",
                    blocking=False,
                )
            ],
            raw_preview=(raw or "")[:500],
            error=get_error_detail(exc),
            blocking=False,
            base_perspective_value=role.base_perspective.value if role.base_perspective else "",
        )

    suggestions: list[RoleSuggestion] = []
    for item in data.get("suggestions", []) or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "observation").strip().lower()
        confidence = str(item.get("confidence") or "medium").strip().lower()
        evidence = str(item.get("evidence") or "").strip()
        recommendation = str(item.get("recommendation") or "").strip()
        target = str(item.get("target") or "").strip()
        if not recommendation:
            continue
        blocking = role.blocking and severity in {"blocker", "major"} and confidence == "high"
        if blocking and not evidence:
            severity = "observation"
            blocking = False
            recommendation = f"{recommendation} [downgraded: missing evidence -> observation]"
        suggestions.append(
            RoleSuggestion(
                severity=severity,
                confidence=confidence,
                evidence=evidence,
                recommendation=recommendation,
                target=target,
                blocking=blocking,
            )
        )

    has_blocking = any(s.blocking for s in suggestions)
    verdict = str(data.get("verdict") or "").strip().upper()
    passed = verdict == "PASS" or not has_blocking
    return RoleReviewOutcome(
        role_id=role.role_id,
        role_display_name=role.display_name,
        role_category=role.category,
        passed=passed,
        summary=str(data.get("summary") or ""),
        suggestions=suggestions,
        raw_preview=(raw or "")[:500],
        blocking=role.blocking,
        base_perspective_value=role.base_perspective.value if role.base_perspective else "",
    )


class RoleReviewWorker:
    """Runs one review role using a caller-provided prompt runner."""

    def __init__(self, role: ReviewRoleSpec, *, timeout: float):
        self.role = role
        self.timeout = float(timeout)
        self._buf: list[str] = []

    def _on_event(self, event) -> None:
        text = getattr(event, "text", None)
        if text:
            self._buf.append(str(text))

    def run(self, artifacts: ReviewArtifacts, prompt_runner: PromptRunner) -> RoleReviewOutcome:
        prompt = build_role_review_prompt(self.role, artifacts)
        raw = ""
        t0 = time.monotonic()
        try:
            raw = prompt_runner(prompt, self._on_event, self.timeout) or ""
            if not raw:
                raw = "".join(self._buf)
            return parse_role_review_output(self.role, raw)
        except Exception as exc:
            err = get_error_detail(exc)
            logger.warning("[RoleReviewWorker:%s] failed: %s", self.role.role_id, err)
            return RoleReviewOutcome(
                role_id=self.role.role_id,
                role_display_name=self.role.display_name,
                role_category=self.role.category,
                passed=True,
                summary="审查异常 (infra, non-blocking)",
                suggestions=[
                    RoleSuggestion(
                        severity="observation",
                        confidence="low",
                        evidence=f"role failed after {int((time.monotonic() - t0) * 1000)}ms",
                        recommendation=f"{self.role.display_name} 审查异常：{err}",
                        blocking=False,
                    )
                ],
                error=err,
                blocking=False,
                base_perspective_value=self.role.base_perspective.value if self.role.base_perspective else "",
            )


def _fallback_perspective(role: ReviewRoleSpec) -> ReviewPerspective:
    if role.base_perspective:
        return role.base_perspective
    category = role.category.lower()
    if category in {"security", "api", "performance", "software"}:
        return ReviewPerspective.ARCHITECT
    if category in {"writing", "research", "domain"}:
        return ReviewPerspective.PRODUCT
    if category in {"design", "ux"}:
        return ReviewPerspective.DESIGNER
    return ReviewPerspective.TESTER


def _outcomes_to_review_result(outcomes: list[RoleReviewOutcome], iteration: int) -> AdaptiveReviewResult:
    aggregated = aggregate_role_outcomes(outcomes)
    {outcome.role_id: outcome for outcome in outcomes}
    reviews: list[PerspectiveReview] = []
    blocking_by_role: dict[str, list[str]] = {}
    observation_by_role: dict[str, list[str]] = {}

    for suggestion in aggregated.blocking_suggestions:
        text = suggestion.to_repair_text(aggregated.role_names)
        for role_id in suggestion.role_ids:
            blocking_by_role.setdefault(role_id, []).append(text)
    for suggestion in aggregated.observations:
        text = f"observation: {suggestion.to_repair_text(aggregated.role_names)}"
        for role_id in suggestion.role_ids:
            observation_by_role.setdefault(role_id, []).append(text)

    for outcome in outcomes:
        role = ReviewRoleSpec(
            role_id=outcome.role_id,
            display_name=outcome.role_display_name,
            category=outcome.role_category,
            mission="",
            review_focus=[],
            must_check=[],
            evidence_policy="",
            blocking=outcome.blocking,
            base_perspective=ReviewPerspective(outcome.base_perspective_value) if outcome.base_perspective_value else None,
        )
        suggestions = blocking_by_role.get(outcome.role_id) or observation_by_role.get(outcome.role_id, [])
        reviews.append(
            PerspectiveReview(
                perspective=_fallback_perspective(role),
                passed=not bool(blocking_by_role.get(outcome.role_id)),
                suggestions=suggestions,
                summary=outcome.summary,
                role_id=outcome.role_id,
                role_display_name=outcome.role_display_name,
                role_category=outcome.role_category,
                blocking=outcome.blocking,
            )
        )

    return AdaptiveReviewResult(
        reviews=reviews,
        iteration=iteration,
        role_outcomes=outcomes,
        aggregated=aggregated,
        blocking_suggestion_hash=aggregated.blocking_hash(),
        blocking_review_passed=all(pr.passed for pr in reviews),
    )


def _run_batch(
    artifacts: ReviewArtifacts,
    roles: list[ReviewRoleSpec],
    *,
    prompt_runner_factory: PromptRunnerFactory,
    max_parallel: int,
    timeout: float,
) -> list[RoleReviewOutcome]:
    outcomes: list[RoleReviewOutcome] = []
    max_parallel = max(1, int(max_parallel or 1))
    for i in range(0, len(roles), max_parallel):
        wave = roles[i:i + max_parallel]
        with ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="role-review-") as pool:
            futures = {
                pool.submit(RoleReviewWorker(role, timeout=timeout).run, artifacts, prompt_runner_factory(role)): role
                for role in wave
            }
            for future in as_completed(futures):
                outcomes.append(future.result())
    order = {role.role_id: idx for idx, role in enumerate(roles)}
    outcomes.sort(key=lambda outcome: order.get(outcome.role_id, 999))
    return outcomes


def run_adaptive_role_review_pipeline(
    artifacts: ReviewArtifacts,
    roles: list[ReviewRoleSpec],
    *,
    prompt_runner_factory: PromptRunnerFactory,
    max_parallel: int = 3,
    timeout: float = 240.0,
    iteration: int | None = None,
) -> AdaptiveReviewResult:
    """Run adaptive roles with dependency batching and parallel workers."""

    outcomes: list[RoleReviewOutcome] = []
    for batch in batch_roles_by_dependencies(roles):
        outcomes.extend(
            _run_batch(
                artifacts,
                batch,
                prompt_runner_factory=prompt_runner_factory,
                max_parallel=max_parallel,
                timeout=timeout,
            )
        )
    order = {role.role_id: idx for idx, role in enumerate(roles)}
    outcomes.sort(key=lambda outcome: order.get(outcome.role_id, 999))
    return _outcomes_to_review_result(outcomes, iteration or artifacts.cycle_number)
