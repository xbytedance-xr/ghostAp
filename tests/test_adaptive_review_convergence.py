from __future__ import annotations

from src.engine_base import ReviewResult
from src.spec_engine.convergence import update_review_pass_streak
from src.spec_engine.models import SpecProject


def _review(*, all_passed: bool = True, role_hash: str = "roles-v1", blocking_hash: str = "") -> ReviewResult:
    result = ReviewResult(iteration=1)
    result.role_plan_hash = role_hash
    result.blocking_suggestion_hash = blocking_hash
    result.blocking_review_passed = all_passed and not blocking_hash
    return result


def test_two_consecutive_passes_required_before_completion():
    project = SpecProject.create(name="demo", root_path="/tmp/demo")

    assert update_review_pass_streak(project, _review(), all_satisfied=True, review_passed=True, required=2) is False
    assert project.review_pass_streak == 1

    assert update_review_pass_streak(project, _review(), all_satisfied=True, review_passed=True, required=2) is True
    assert project.review_pass_streak == 2


def test_role_plan_change_resets_pass_streak():
    project = SpecProject.create(name="demo", root_path="/tmp/demo")
    assert update_review_pass_streak(project, _review(role_hash="roles-v1"), all_satisfied=True, review_passed=True, required=2) is False

    assert update_review_pass_streak(project, _review(role_hash="roles-v2"), all_satisfied=True, review_passed=True, required=2) is False

    assert project.review_pass_streak == 1
    assert project.last_review_role_plan_hash == "roles-v2"


def test_blocking_suggestion_resets_pass_streak():
    project = SpecProject.create(name="demo", root_path="/tmp/demo")
    project.review_pass_streak = 1
    project.last_review_role_plan_hash = "roles-v1"

    assert (
        update_review_pass_streak(
            project,
            _review(all_passed=False, blocking_hash="needs-fix"),
            all_satisfied=True,
            review_passed=False,
            required=2,
        )
        is False
    )

    assert project.review_pass_streak == 0
    assert project.last_review_blocking_suggestion_hash == "needs-fix"


def test_project_persists_review_convergence_state():
    project = SpecProject.create(name="demo", root_path="/tmp/demo")
    project.review_pass_streak = 2
    project.last_review_role_plan_hash = "roles-v1"
    project.last_review_blocking_suggestion_hash = ""

    restored = SpecProject.from_dict(project.to_dict())

    assert restored.review_pass_streak == 2
    assert restored.last_review_role_plan_hash == "roles-v1"
    assert restored.last_review_blocking_suggestion_hash == ""
