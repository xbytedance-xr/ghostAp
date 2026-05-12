import json
import threading
import time

from src.engine_base import ReviewPerspective
from src.spec_engine.adaptive_review import run_adaptive_role_review_pipeline
from src.spec_engine.review_aggregation import aggregate_role_outcomes
from src.spec_engine.review_artifacts import ReviewArtifacts
from src.spec_engine.review_roles import ReviewRoleSpec


def _role(role_id: str, *, depends_on: list[str] | None = None) -> ReviewRoleSpec:
    return ReviewRoleSpec(
        role_id=role_id,
        display_name=role_id.replace("_", " ").title(),
        category="test",
        mission=f"review as {role_id}",
        review_focus=["focus"],
        must_check=["check"],
        evidence_policy="blockers require evidence",
        depends_on=depends_on or [],
        base_perspective=ReviewPerspective.TESTER if role_id == "tester" else None,
    )


def _artifacts() -> ReviewArtifacts:
    return ReviewArtifacts(cycle_number=1, requirement="ship feature", cwd="/repo")


def _json(role_id: str, *, verdict: str = "PASS", suggestions: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "role_id": role_id,
            "verdict": verdict,
            "summary": f"{role_id} summary",
            "suggestions": suggestions or [],
        },
        ensure_ascii=False,
    )


def test_roles_in_same_dependency_batch_run_concurrently():
    roles = [_role("editor"), _role("fact_checker")]
    active = 0
    max_active = 0
    lock = threading.Lock()

    def factory(role):
        def runner(prompt, on_event, timeout):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            return _json(role.role_id)
        return runner

    result = run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        max_parallel=2,
        timeout=5,
    )

    assert max_active == 2
    assert result.all_passed
    assert [review.role_id for review in result.reviews] == ["editor", "fact_checker"]


def test_dependency_layers_run_after_prerequisites_finish():
    roles = [_role("fact_checker"), _role("conclusion_editor", depends_on=["fact_checker"])]
    order: list[str] = []

    def factory(role):
        def runner(prompt, on_event, timeout):
            order.append(f"start:{role.role_id}")
            time.sleep(0.01)
            order.append(f"done:{role.role_id}")
            return _json(role.role_id)
        return runner

    run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        max_parallel=2,
        timeout=5,
    )

    assert order == [
        "start:fact_checker",
        "done:fact_checker",
        "start:conclusion_editor",
        "done:conclusion_editor",
    ]


def test_evidence_less_blocker_is_downgraded_and_does_not_block_review_result():
    roles = [_role("fact_checker")]

    def factory(role):
        def runner(prompt, on_event, timeout):
            return _json(
                role.role_id,
                verdict="FAIL",
                suggestions=[
                    {
                        "severity": "blocker",
                        "confidence": "high",
                        "evidence": "",
                        "recommendation": "补充来源",
                    }
                ],
            )
        return runner

    result = run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        max_parallel=1,
        timeout=5,
    )

    assert result.all_passed
    assert result.reviews[0].passed is True
    assert "observation" in result.reviews[0].suggestions[0]


def test_aggregated_blocking_suggestion_keeps_role_name_and_maps_to_base_perspective():
    roles = [_role("tester")]

    def factory(role):
        def runner(prompt, on_event, timeout):
            return _json(
                role.role_id,
                verdict="FAIL",
                suggestions=[
                    {
                        "severity": "major",
                        "confidence": "high",
                        "evidence": "tests/test_feature.py lacks edge case",
                        "recommendation": "补充空输入边界测试",
                        "target": "tests/test_feature.py",
                    }
                ],
            )
        return runner

    result = run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        max_parallel=1,
        timeout=5,
    )

    assert result.all_passed is False
    assert result.reviews[0].perspective is ReviewPerspective.TESTER
    assert result.reviews[0].role_id == "tester"
    assert result.reviews[0].role_display_name == "Tester"
    assert result.reviews[0].suggestions == [
        "[Tester] 补充空输入边界测试 (evidence: tests/test_feature.py lacks edge case; target: tests/test_feature.py)"
    ]


def test_aggregator_deduplicates_same_recommendation_from_multiple_roles():
    roles = [_role("a"), _role("b")]

    def factory(role):
        def runner(prompt, on_event, timeout):
            return _json(
                role.role_id,
                verdict="FAIL",
                suggestions=[
                    {
                        "severity": "major",
                        "confidence": "high",
                        "evidence": f"{role.role_id} evidence",
                        "recommendation": "补充来源",
                    }
                ],
            )
        return runner

    result = run_adaptive_role_review_pipeline(
        _artifacts(),
        roles,
        prompt_runner_factory=factory,
        max_parallel=2,
        timeout=5,
    )
    aggregated = aggregate_role_outcomes(result.role_outcomes)

    assert len(aggregated.blocking_suggestions) == 1
    assert aggregated.blocking_suggestions[0].role_ids == ["a", "b"]
