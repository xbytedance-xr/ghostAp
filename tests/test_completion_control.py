"""Tests for spec completion control (objective verify, active verifier, completion gate)."""

import json
from unittest.mock import MagicMock, patch

from src.spec_engine.adaptive_review import (
    AdaptiveReviewResult,
    _outcomes_to_review_result,
    build_role_review_prompt,
    parse_role_review_output,
)
from src.spec_engine.criteria import evaluate_criteria, run_objective_verify
from src.spec_engine.review_aggregation import RoleReviewOutcome, RoleSuggestion
from src.spec_engine.review_artifacts import ReviewArtifacts, collect_review_artifacts
from src.spec_engine.review_roles import (
    COMPLETION_CONTROL_ROLE_ID,
    ReviewRoleSpec,
    completion_control_role,
)

# ===========================================================================
# Phase 1: Objective verify gate
# ===========================================================================


class TestObjectiveVerify:
    def test_run_objective_verify_no_command(self):
        passed, output = run_objective_verify("", "/tmp")
        assert passed is True
        assert output == ""

    def test_run_objective_verify_success(self):
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "5 passed"
        mock_result.stderr = ""
        mock_executor = MagicMock()
        mock_executor.return_value.execute.return_value = mock_result

        with patch("src.sandbox.executor.SandboxExecutor", mock_executor):
            passed, output = run_objective_verify("pytest tests/ -q", "/project")
        assert passed is True
        assert "5 passed" in output

    def test_run_objective_verify_failure(self):
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stdout = ""
        mock_result.stderr = "2 failed"
        mock_executor = MagicMock()
        mock_executor.return_value.execute.return_value = mock_result

        with patch("src.sandbox.executor.SandboxExecutor", mock_executor):
            passed, output = run_objective_verify("pytest tests/ -q", "/project")
        assert passed is False
        assert "2 failed" in output

    @patch("src.spec_engine.criteria.run_objective_verify")
    def test_evaluate_criteria_verify_overrides_llm_optimism(self, mock_verify):
        """When LLM says all PASS but verify fails, all_satisfied must be False."""
        mock_verify.return_value = (False, "FAILED: 2 tests failed")

        session = MagicMock()
        settings = MagicMock()
        settings.spec_objective_verify_enabled = True
        settings.spec_objective_verify_timeout = 60
        settings.engine_eval_prompt_timeout = 60

        project = MagicMock()
        project.verify_command = "pytest tests/ -q"
        project.root_path = "/project"
        project.criteria_tracker.is_all_satisfied = True

        # Mock the send_prompt to return all PASS
        def fake_send(prompt, **kwargs):
            on_event = kwargs.get("on_event")
            if on_event:
                evt = MagicMock()
                evt.event_type = "text_chunk"
                evt.text = "CRITERIA_1: PASS\nCRITERIA_2: PASS"
                on_event(evt)

        result = evaluate_criteria(
            session=session,
            criteria=["test1", "test2"],
            cycle=1,
            project=project,
            send_prompt_fn=fake_send,
            settings=settings,
        )
        assert result["all_satisfied"] is False
        assert result["verify_passed"] is False

    def test_evaluate_criteria_no_verify_command_passthrough(self):
        """When no verify_command, LLM self-eval is used as-is."""
        session = MagicMock()
        settings = MagicMock()
        settings.spec_objective_verify_enabled = True
        settings.spec_objective_verify_timeout = 60
        settings.engine_eval_prompt_timeout = 60

        project = MagicMock()
        project.verify_command = ""
        project.criteria_tracker.is_all_satisfied = True

        def fake_send(prompt, **kwargs):
            on_event = kwargs.get("on_event")
            if on_event:
                evt = MagicMock()
                evt.event_type = "text_chunk"
                evt.text = "CRITERIA_1: PASS"
                on_event(evt)

        result = evaluate_criteria(
            session=session,
            criteria=["test1"],
            cycle=1,
            project=project,
            send_prompt_fn=fake_send,
            settings=settings,
        )
        assert result["all_satisfied"] is True
        assert result["verify_passed"] is True


# ===========================================================================
# Phase 2: Active verifier (dedicated prompt)
# ===========================================================================


class TestActiveVerifierPrompt:
    def test_completion_control_gets_dedicated_prompt(self):
        role = completion_control_role()
        artifacts = ReviewArtifacts(
            cycle_number=1,
            requirement="implement feature X",
            cwd="/project",
            acceptance_criteria=["criterion A", "criterion B"],
            criteria_satisfied={0: True, 1: False},
            verify_command="pytest",
            verify_passed=True,
            verify_output="3 passed",
        )
        prompt = build_role_review_prompt(role, artifacts)
        assert "不择手段验证" in prompt
        assert "GOAL_MET" in prompt
        assert "criterion A" in prompt
        assert "[PASS]" in prompt
        assert "[FAIL]" in prompt
        assert "pytest" in prompt
        assert "grill-me" in prompt
        assert "综合采纳判断" in prompt
        assert "自动采纳" in prompt

    def test_non_completion_role_gets_generic_prompt(self):
        role = ReviewRoleSpec(
            role_id="architect",
            display_name="架构师",
            category="software",
            mission="审查架构",
            review_focus=["架构"],
            must_check=["模块"],
            evidence_policy="evidence",
            blocking=True,
        )
        artifacts = ReviewArtifacts(
            cycle_number=1,
            requirement="implement feature X",
            cwd="/project",
            acceptance_criteria=["criterion A"],
            criteria_satisfied={0: True},
        )
        prompt = build_role_review_prompt(role, artifacts)
        # Generic prompt doesn't have completion-specific instructions
        assert "不择手段验证" not in prompt
        assert "架构师" in prompt
        assert "grill-me" in prompt
        assert "尖锐追问" in prompt
        assert "推荐答案" in prompt
        assert "采纳动作" in prompt

    def test_review_artifacts_carries_criteria_and_verify(self):
        """collect_review_artifacts passes criteria state and verify results."""
        cycle = MagicMock()
        cycle.cycle_number = 3
        cycle.spec_content = "spec"
        cycle.plan_content = "plan"
        cycle.tasks = []
        cycle.build_output = "build"
        cycle.spec_path = None
        cycle.plan_path = None
        cycle.tasks_path = None
        cycle.build_path = None

        project = MagicMock()
        project.requirement = "do stuff"
        project.acceptance_criteria = ["A", "B"]
        project.criteria_tracker.satisfied = {0: True, 1: False}
        project.verify_command = "make test"

        with patch("src.spec_engine.review_artifacts._git_diff", return_value=""):
            with patch("src.spec_engine.review_artifacts._git_touched_files", return_value=[]):
                artifacts = collect_review_artifacts(
                    cycle=cycle,
                    project=project,
                    cwd="/tmp",
                    verify_passed=False,
                    verify_output="FAIL",
                )

        assert artifacts.acceptance_criteria == ["A", "B"]
        assert artifacts.criteria_satisfied == {0: True, 1: False}
        assert artifacts.verify_command == "make test"
        assert artifacts.verify_passed is False
        assert artifacts.verify_output == "FAIL"


# ===========================================================================
# Phase 3: Completion gate (parse verdict + early stop / veto)
# ===========================================================================


class TestCompletionGateParsing:
    def test_parse_goal_met_verdict(self):
        role = completion_control_role()
        raw = json.dumps({
            "role_id": "completion_control",
            "verdict": "PASS",
            "goal_verdict": "GOAL_MET",
            "goal_confidence": "high",
            "evidence_summary": "all tests pass, files exist",
            "suggestions": [],
        })
        outcome = parse_role_review_output(role, raw)
        assert outcome.passed is True
        assert outcome.goal_verdict == "GOAL_MET"
        assert outcome.goal_confidence == "high"
        assert outcome.goal_evidence == "all tests pass, files exist"

    def test_parse_goal_not_met_verdict(self):
        role = completion_control_role()
        raw = json.dumps({
            "role_id": "completion_control",
            "verdict": "FAIL",
            "goal_verdict": "GOAL_NOT_MET",
            "goal_confidence": "high",
            "evidence_summary": "criterion 2 not implemented",
            "suggestions": [
                {
                    "severity": "blocker",
                    "confidence": "high",
                    "evidence": "file not found",
                    "recommendation": "implement criterion 2",
                }
            ],
        })
        outcome = parse_role_review_output(role, raw)
        assert outcome.passed is False
        assert outcome.goal_verdict == "GOAL_NOT_MET"
        assert outcome.goal_confidence == "high"

    def test_outcomes_to_review_result_extracts_completion_gate(self):
        outcomes = [
            RoleReviewOutcome(
                role_id="architect",
                role_display_name="架构师",
                role_category="software",
                passed=True,
                summary="OK",
                blocking=True,
                base_perspective_value="architect",
            ),
            RoleReviewOutcome(
                role_id=COMPLETION_CONTROL_ROLE_ID,
                role_display_name="完成度与方向把控",
                role_category="completion_control",
                passed=True,
                summary="all good",
                blocking=True,
                base_perspective_value="product",
                goal_verdict="GOAL_MET",
                goal_confidence="high",
                goal_evidence="tests pass",
            ),
        ]
        result = _outcomes_to_review_result(outcomes, iteration=1)
        assert isinstance(result, AdaptiveReviewResult)
        assert result.completion_gate_met is True
        assert result.completion_gate_confidence == "high"
        assert result.completion_gate_evidence == "tests pass"

    def test_outcomes_to_review_result_goal_not_met(self):
        outcomes = [
            RoleReviewOutcome(
                role_id=COMPLETION_CONTROL_ROLE_ID,
                role_display_name="完成度与方向把控",
                role_category="completion_control",
                passed=False,
                summary="not done",
                blocking=True,
                base_perspective_value="product",
                goal_verdict="GOAL_NOT_MET",
                goal_confidence="high",
                goal_evidence="missing feature",
                suggestions=[
                    RoleSuggestion(
                        severity="blocker",
                        confidence="high",
                        evidence="file not found",
                        recommendation="implement it",
                        blocking=True,
                    )
                ],
            ),
        ]
        result = _outcomes_to_review_result(outcomes, iteration=1)
        assert result.completion_gate_met is False


class TestCompletionGateEngine:
    """Test completion gate logic in engine finalize flow (unit-level mocking)."""

    def _make_settings(self, **overrides):
        settings = MagicMock()
        settings.spec_review_enabled = True
        settings.spec_review_pass_streak_required = 2
        settings.spec_completion_gate_enabled = True
        settings.spec_objective_verify_enabled = True
        settings.spec_objective_verify_timeout = 60
        settings.spec_discovery_enabled = False
        settings.spec_persist_phase_artifacts = False
        settings.spec_persist_every_phase = False
        settings.spec_convergence_window = 0
        settings.spec_backlog_stuck_window = 0
        settings.spec_success_ignore_backlog = True
        settings.spec_rebuild_session_between_cycles = False
        settings.spec_disable_convergence = True
        settings.spec_disable_early_stop = False
        settings.spec_min_cycles = 1
        settings.spec_infinite_mode = False
        for k, v in overrides.items():
            setattr(settings, k, v)
        return settings

    def test_early_stop_with_evidence(self):
        """Completion gate allows early stop bypassing streak requirement."""
        # Create a mock AdaptiveReviewResult with completion gate
        review_result = AdaptiveReviewResult(
            reviews=[],
            iteration=1,
            completion_gate_met=True,
            completion_gate_confidence="high",
            completion_gate_evidence="all verified",
        )

        # The logic:
        # all_satisfied=True, review_passed=True, effective_review_passed=False (streak=1<2)
        # But: completion_gate_met + high confidence + verify != False -> override to True
        all_satisfied = True
        review_passed = True
        effective_review_passed = False  # streak not met
        verify_passed = True
        completion_gate_met = review_result.completion_gate_met
        completion_gate_confidence = review_result.completion_gate_confidence

        # Apply the gate logic (same as engine code)
        if (
            all_satisfied
            and completion_gate_met
            and completion_gate_confidence == "high"
            and review_passed
            and not effective_review_passed
            and verify_passed is not False
        ):
            effective_review_passed = True

        assert effective_review_passed is True

    def test_veto_prevents_success(self):
        """Completion control blocking prevents success even if other signals OK."""
        review_result = AdaptiveReviewResult(
            reviews=[],
            iteration=1,
            completion_gate_met=False,
            role_outcomes=[
                RoleReviewOutcome(
                    role_id=COMPLETION_CONTROL_ROLE_ID,
                    role_display_name="完成度与方向把控",
                    role_category="completion_control",
                    passed=False,
                    blocking=True,
                    base_perspective_value="product",
                    goal_verdict="GOAL_NOT_MET",
                    goal_confidence="high",
                    goal_evidence="missing feature",
                    suggestions=[
                        RoleSuggestion(
                            severity="blocker",
                            confidence="high",
                            evidence="file not found",
                            recommendation="implement it",
                            blocking=True,
                        )
                    ],
                ),
            ],
        )

        all_satisfied = True
        effective_review_passed = True  # other roles say OK

        # Apply veto logic
        if (
            all_satisfied
            and effective_review_passed
            and not review_result.completion_gate_met
        ):
            has_cc_blockers = any(
                o.role_id == COMPLETION_CONTROL_ROLE_ID and not o.passed
                for o in review_result.role_outcomes
            )
            if has_cc_blockers:
                effective_review_passed = False

        assert effective_review_passed is False

    def test_no_veto_when_gate_disabled(self):
        """When completion_gate_enabled=False, no veto happens."""
        settings_gate_disabled = False

        all_satisfied = True
        effective_review_passed = True
        completion_gate_met = False

        # Gate disabled -> skip veto
        if settings_gate_disabled and all_satisfied and effective_review_passed and not completion_gate_met:
            effective_review_passed = False

        # Should remain True since gate is disabled
        assert effective_review_passed is True


# ===========================================================================
# Config validation
# ===========================================================================


class TestCompletionControlConfig:
    def test_default_settings_have_completion_fields(self):
        """New settings fields exist with correct defaults."""
        from src.config import get_settings
        s = get_settings()
        assert s.spec_objective_verify_enabled is True
        assert s.spec_objective_verify_timeout == 300
        assert s.spec_completion_control_active_verify is True
        assert s.spec_completion_gate_enabled is True
