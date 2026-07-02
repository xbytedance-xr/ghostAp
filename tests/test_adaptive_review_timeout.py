"""Tests for per-role timeout multiplier, diff truncation, and timeout degradation.

AC-17: classify_timeout regression — verify unified timeout detection.
AC-18: Edge cases — asyncio.TimeoutError, __cause__ chain, OSError no longer misclassified.
AC-19: All review roles degrade gracefully on timeout.
"""

import asyncio
import json

import pytest

from src.engine_base import ReviewPerspective
from src.spec_engine.adaptive_review import (
    RoleReviewWorker,
    build_role_review_prompt,
    run_adaptive_role_review_pipeline,
)
from src.spec_engine.review_artifacts import ReviewArtifacts
from src.spec_engine.review_roles import ReviewRoleSpec


def _role(role_id: str, *, base_perspective=None) -> ReviewRoleSpec:
    return ReviewRoleSpec(
        role_id=role_id,
        display_name=role_id.replace("_", " ").title(),
        category="software",
        mission=f"review as {role_id}",
        review_focus=["focus"],
        must_check=["check"],
        evidence_policy="blockers require evidence",
        base_perspective=base_perspective,
    )


def _artifacts(*, diff_patch: str = "") -> ReviewArtifacts:
    return ReviewArtifacts(cycle_number=1, requirement="ship feature", cwd="/repo", diff_patch=diff_patch)


def _json_pass(role_id: str) -> str:
    return json.dumps({"role_id": role_id, "verdict": "PASS", "summary": "ok", "suggestions": []})


# --- Test 1: per-role timeout multiplier ---


def test_role_timeout_multiplier_applied():
    """Architect role should receive timeout = base * 1.5 = 360."""
    captured_timeouts: dict[str, float] = {}

    def factory(role):
        def runner(prompt, on_event, timeout):
            captured_timeouts[role.role_id] = timeout
            return _json_pass(role.role_id)
        return runner

    roles = [_role("architect", base_perspective=ReviewPerspective.ARCHITECT), _role("tester", base_perspective=ReviewPerspective.TESTER)]
    run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        timeout=240.0,
        role_timeout_multipliers={"architect": 1.5},
    )

    assert captured_timeouts["architect"] == 360.0, f"Expected 360.0, got {captured_timeouts['architect']}"
    assert captured_timeouts["tester"] == 240.0, f"Expected 240.0, got {captured_timeouts['tester']}"


def test_role_timeout_multiplier_default_is_1():
    """Roles without multiplier config should get base timeout unchanged."""
    captured_timeouts: dict[str, float] = {}

    def factory(role):
        def runner(prompt, on_event, timeout):
            captured_timeouts[role.role_id] = timeout
            return _json_pass(role.role_id)
        return runner

    roles = [_role("designer")]
    run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        timeout=200.0,
        role_timeout_multipliers={"architect": 2.0},
    )

    assert captured_timeouts["designer"] == 200.0


# --- Test 2: large diff truncation ---


def test_large_diff_truncated():
    """Diff > 20000 chars should be truncated with head + tail."""
    large_diff = "x" * 50_000
    artifacts = _artifacts(diff_patch=large_diff)
    role = _role("architect")
    prompt = build_role_review_prompt(role, artifacts)

    # head 8000 + tail 8000 + truncation marker + other prompt parts
    # The diff portion should be ≈ 16000 + marker, total prompt well under 50000
    assert len(prompt) < 25_000, f"Prompt too large: {len(prompt)}"
    assert "...[truncated 34000 chars]..." in prompt


def test_small_diff_not_truncated():
    """Diff ≤ 20000 chars should not be truncated."""
    small_diff = "y" * 15_000
    artifacts = _artifacts(diff_patch=small_diff)
    role = _role("tester")
    prompt = build_role_review_prompt(role, artifacts)

    assert "truncated" not in prompt
    assert "y" * 15_000 in prompt


# --- Test 3: timeout degradation ---


def test_blocking_role_timeout_stays_blocking():
    """Blocking role timeout should remain blocking with timeout_blocking prefix."""
    role = _role("architect", base_perspective=ReviewPerspective.ARCHITECT)
    role.blocking = True

    def runner(prompt, on_event, timeout):
        raise TimeoutError("ACP prompt 执行超时 (240.0s)")

    worker = RoleReviewWorker(role, timeout=240.0)
    outcome = worker.run(_artifacts(), runner)

    assert outcome.passed is False
    assert outcome.blocking is True
    assert outcome.suggestions[0].blocking is True
    assert "timeout_blocking" in outcome.error
    assert "阻断" in outcome.summary
    assert "timeout_degraded" not in outcome.error


def test_non_blocking_role_timeout_degrades():
    """Non-blocking role timeout should degrade to non-blocking with timeout_degraded prefix."""
    role = _role("doc_writer", base_perspective=ReviewPerspective.PRODUCT)
    role.blocking = False

    def runner(prompt, on_event, timeout):
        raise TimeoutError("ACP prompt 执行超时 (240.0s)")

    worker = RoleReviewWorker(role, timeout=240.0)
    outcome = worker.run(_artifacts(), runner)

    assert outcome.passed is False
    assert outcome.blocking is False
    assert outcome.suggestions[0].blocking is False
    assert "timeout_degraded" in outcome.error
    assert "已降级" in outcome.summary


def test_non_timeout_error_still_blocking():
    """Non-timeout errors should remain blocking."""
    role = _role("architect", base_perspective=ReviewPerspective.ARCHITECT)

    def runner(prompt, on_event, timeout):
        raise RuntimeError("connection refused")

    worker = RoleReviewWorker(role, timeout=240.0)
    outcome = worker.run(_artifacts(), runner)

    assert outcome.passed is False
    assert outcome.blocking is True
    assert outcome.suggestions[0].blocking is True
    assert "timeout_degraded" not in outcome.error


# --- AC-17: classify_timeout regression (unified SSOT) ---


class TestAC17ClassifyTimeoutRegression:
    """AC-17: Verify classify_timeout is used instead of hand-rolled isinstance checks."""

    def test_plain_timeout_error_blocking_role(self):
        """Blocking role + plain TimeoutError → blocking=True, timeout_blocking prefix."""
        role = _role("tester", base_perspective=ReviewPerspective.TESTER)
        role.blocking = True

        def runner(prompt, on_event, timeout):
            raise TimeoutError("operation timed out")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is True
        assert "timeout_blocking" in outcome.error
        assert "阻断" in outcome.summary

    def test_plain_timeout_error_non_blocking_role(self):
        """Non-blocking role + plain TimeoutError → blocking=False, timeout_degraded prefix."""
        role = _role("tester", base_perspective=ReviewPerspective.TESTER)
        role.blocking = False

        def runner(prompt, on_event, timeout):
            raise TimeoutError("operation timed out")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is False
        assert "timeout_degraded" in outcome.error
        assert "已降级" in outcome.summary

    def test_asyncio_timeout_error_blocking(self):
        """Blocking role + asyncio.TimeoutError → blocking=True, timeout_blocking."""
        role = _role("architect", base_perspective=ReviewPerspective.ARCHITECT)
        role.blocking = True

        def runner(prompt, on_event, timeout):
            raise asyncio.TimeoutError()

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is True
        assert "timeout_blocking" in outcome.error

    def test_asyncio_timeout_error_non_blocking(self):
        """Non-blocking role + asyncio.TimeoutError → blocking=False, timeout_degraded."""
        role = _role("architect", base_perspective=ReviewPerspective.ARCHITECT)
        role.blocking = False

        def runner(prompt, on_event, timeout):
            raise asyncio.TimeoutError()

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is False
        assert "timeout_degraded" in outcome.error

    def test_wrapped_timeout_in_cause_chain_blocking(self):
        """Blocking role + RuntimeError wrapping TimeoutError → blocking=True."""
        role = _role("designer", base_perspective=ReviewPerspective.DESIGNER)
        role.blocking = True

        def runner(prompt, on_event, timeout):
            try:
                raise TimeoutError("inner timeout")
            except TimeoutError:
                raise RuntimeError("wrapper") from TimeoutError("inner timeout")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is True
        assert "timeout_blocking" in outcome.error

    def test_oserror_no_longer_misclassified(self):
        """Pure OSError (no timeout in chain) → is_timeout=False, blocking=True."""
        role = _role("architect", base_perspective=ReviewPerspective.ARCHITECT)

        def runner(prompt, on_event, timeout):
            raise OSError("Connection refused")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is True
        assert "timeout_degraded" not in outcome.error

    def test_oserror_with_chinese_timeout_no_longer_matches(self):
        """OSError with '超时' in message → is_timeout=False (no string matching)."""
        role = _role("tester", base_perspective=ReviewPerspective.TESTER)

        def runner(prompt, on_event, timeout):
            raise OSError("网络超时")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        # After switching to classify_timeout, OSError without a TimeoutError
        # in its __cause__/__context__ chain is NOT classified as timeout.
        assert outcome.blocking is True
        assert "timeout_degraded" not in outcome.error

    def test_runtime_error_still_blocking(self):
        """RuntimeError without timeout in chain → blocking=True."""
        role = _role("perf_reviewer", base_perspective=ReviewPerspective.PRODUCT)

        def runner(prompt, on_event, timeout):
            raise RuntimeError("unexpected failure")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.blocking is True
        assert "timeout_degraded" not in outcome.error


# --- AC-19: Timeout semantics differ by role blocking status ---


_ALL_REVIEW_ROLES = [
    ("architect", ReviewPerspective.ARCHITECT),
    ("tester", ReviewPerspective.TESTER),
    ("designer", ReviewPerspective.DESIGNER),
    ("perf_reviewer", ReviewPerspective.PRODUCT),
]


class TestAC19TimeoutSemanticsByRoleType:
    """AC-19: Blocking roles fail-closed on timeout; non-blocking roles degrade.

    - Blocking role + timeout → blocking=True, error=timeout_blocking:..., summary含"阻断"
    - Non-blocking role + timeout → blocking=False, error=timeout_degraded:..., summary含"已降级"
    """

    @pytest.mark.parametrize("role_id,perspective", _ALL_REVIEW_ROLES)
    def test_blocking_role_timeout_stays_blocking(self, role_id, perspective):
        """Blocking role: TimeoutError → blocking=True, timeout_blocking prefix."""
        role = _role(role_id, base_perspective=perspective)
        role.blocking = True

        def runner(prompt, on_event, timeout):
            raise TimeoutError(f"ACP prompt 执行超时 ({timeout}s)")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.passed is False
        assert outcome.blocking is True, f"{role_id} (blocking) should stay blocking on timeout"
        assert "阻断" in outcome.summary
        assert "timeout_blocking" in outcome.error
        assert outcome.suggestions[0].blocking is True
        assert outcome.suggestions[0].severity == "major"

    @pytest.mark.parametrize("role_id,perspective", _ALL_REVIEW_ROLES)
    def test_non_blocking_role_timeout_degrades(self, role_id, perspective):
        """Non-blocking role: TimeoutError → blocking=False, timeout_degraded prefix."""
        role = _role(role_id, base_perspective=perspective)
        role.blocking = False

        def runner(prompt, on_event, timeout):
            raise TimeoutError(f"ACP prompt 执行超时 ({timeout}s)")

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert outcome.passed is False
        assert outcome.blocking is False, f"{role_id} (non-blocking) should degrade on timeout"
        assert outcome.summary == "审查超时（已降级）"
        assert "timeout_degraded" in outcome.error
        assert outcome.suggestions[0].blocking is False

    @pytest.mark.parametrize("role_id,perspective", _ALL_REVIEW_ROLES)
    def test_blocking_role_timeout_no_json_parse_error(self, role_id, perspective):
        """Blocking role timeout should produce clean outcome, not a JSON parse failure."""
        role = _role(role_id, base_perspective=perspective)
        role.blocking = True

        def runner(prompt, on_event, timeout):
            raise asyncio.TimeoutError()

        worker = RoleReviewWorker(role, timeout=240.0)
        outcome = worker.run(_artifacts(), runner)

        assert "无法解析" not in (outcome.summary or "")
        assert outcome.error is not None
        assert "timeout_blocking" in outcome.error
        assert len(outcome.suggestions) == 1
        assert outcome.suggestions[0].severity == "major"
