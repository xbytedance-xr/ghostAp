"""Tests for loop_engine — subprocess-driven LoopEngine + new modules."""

import pytest
from unittest.mock import patch, MagicMock

from src.loop_engine.engine import LoopEngine, LoopEngineManager, LoopEngineCallbacks
from src.loop_engine.models import (
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    IterationRecord,
    IterationStatus,
    LoopRole,
    CriteriaTracker,
    TerminationSignal,
    TerminationResult,
    IterationState,
    RoleSelection,
    LoopContextManager,
)
from src.loop_engine.roles import RoleRouter, ROLE_PROMPTS
from src.loop_engine.termination import TerminationChecker
from src.loop_engine.analyzer import RequirementAnalyzer
from src.loop_engine.reporter import LoopReporter


# ---------------------------------------------------------------------------
# Helper: make settings mock
# ---------------------------------------------------------------------------


def _mock_settings():
    s = MagicMock()
    s.loop_max_iterations = 15
    s.loop_convergence_window = 3
    s.loop_execution_timeout = 300
    s.loop_max_context_tokens = 8000
    s.loop_default_max_retries = 2
    return s


# ===========================================================================
# TestLoopEngine
# ===========================================================================


class TestLoopEngine:
    @patch("src.loop_engine.engine.get_settings")
    def _make_engine(self, mock_settings, **kwargs):
        mock_settings.return_value = _mock_settings()
        return LoopEngine(chat_id="c1", root_path="/tmp/test", **kwargs)

    def test_initial_state(self):
        engine = self._make_engine()
        assert engine.project is None
        assert not engine.is_running

    def test_stop(self):
        engine = self._make_engine()
        engine._is_running = True
        engine.stop()
        assert engine._should_stop

    def test_pause(self):
        engine = self._make_engine()
        engine._project = MagicMock()
        engine._is_running = True
        engine.pause()
        assert engine._should_stop

    def test_cleanup(self):
        engine = self._make_engine()
        engine._ai_session = MagicMock()
        engine._session_manager = MagicMock()
        engine._project = MagicMock()
        engine.cleanup()
        assert engine._ai_session is None
        assert engine._project is None
        assert not engine._is_running

    def test_inject_guidance_via_context_manager(self):
        engine = self._make_engine()
        engine.inject_guidance("focus on login")
        assert engine._context_manager.has_user_guidance()
        guidance = engine._context_manager.consume_user_guidance()
        assert guidance == "focus on login"

    def test_build_role_prompt_contains_role_and_criteria(self):
        engine = self._make_engine()
        req = LoopRequirement(
            goal="add login", acceptance_criteria=["email", "phone"], raw_text="test"
        )
        tracker = CriteriaTracker()
        tracker.init_criteria(["email", "phone"])
        tracker.update(0, True, 1)
        state = IterationState(
            iteration_number=2,
            requirement=req,
            criteria_tracker=tracker,
            recent_iterations=[],
            context_summary="",
        )
        selection = RoleSelection(
            role=LoopRole.DEVELOPER, reason="test", focus="email feature"
        )
        prompt = engine._build_role_prompt(state, selection)
        assert "add login" in prompt
        assert "开发者" in prompt
        assert "[x]" in prompt  # email is satisfied
        assert "[ ]" in prompt  # phone is not
        assert "/tmp/test" in prompt
        assert "DEEP_TASK_SUCCESS" in prompt

    def test_build_role_prompt_with_guidance(self):
        engine = self._make_engine()
        req = LoopRequirement(goal="test", acceptance_criteria=["c1"], raw_text="test")
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1"])
        state = IterationState(
            iteration_number=2,
            requirement=req,
            criteria_tracker=tracker,
            recent_iterations=[],
            context_summary="",
            user_guidance="prioritize X",
        )
        selection = RoleSelection(role=LoopRole.DEVELOPER, reason="test", focus="c1")
        prompt = engine._build_role_prompt(state, selection)
        assert "prioritize X" in prompt

    def test_build_role_prompt_with_context(self):
        engine = self._make_engine()
        req = LoopRequirement(goal="test", acceptance_criteria=["c1"], raw_text="test")
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1"])
        state = IterationState(
            iteration_number=3,
            requirement=req,
            criteria_tracker=tracker,
            recent_iterations=[],
            context_summary="## 迭代历史\n已完成架构设计",
        )
        selection = RoleSelection(role=LoopRole.TESTER, reason="test", focus="testing")
        prompt = engine._build_role_prompt(state, selection)
        assert "已完成的工作" in prompt
        assert "已完成架构设计" in prompt
        assert "测试者" in prompt

    def test_build_iteration_state(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        req = LoopRequirement(
            goal="test", acceptance_criteria=["c1", "c2"], raw_text="test"
        )
        engine._project.set_requirement(req)
        engine._project.iterations.append(
            IterationRecord(
                iteration=1, role=LoopRole.ARCHITECT, status=IterationStatus.SUCCESS
            )
        )
        state = engine._build_iteration_state(2, req)
        assert state.iteration_number == 2
        assert len(state.recent_iterations) == 1
        assert state.consecutive_failures == 0
        assert state.last_role == LoopRole.ARCHITECT

    def test_save_state_no_project(self):
        engine = self._make_engine()
        with pytest.raises(ValueError):
            engine.save_state()

    def test_resume_returns_project(self):
        engine = self._make_engine()
        engine._project = MagicMock()
        result = engine.resume()
        assert result is engine._project

    def test_has_core_modules(self):
        engine = self._make_engine()
        assert isinstance(engine._role_router, RoleRouter)
        assert isinstance(engine._termination_checker, TerminationChecker)
        assert isinstance(engine._context_manager, LoopContextManager)


# ===========================================================================
# TestLoopEngineManager
# ===========================================================================


class TestLoopEngineManager:
    @patch("src.loop_engine.engine.get_settings")
    def _make_manager(self, mock_settings):
        mock_settings.return_value = _mock_settings()
        return LoopEngineManager()

    def test_get_or_create(self):
        mgr = self._make_manager()
        engine = mgr.get_or_create("c1", "/tmp/test")
        assert engine is not None
        engine2 = mgr.get_or_create("c1", "/tmp/test")
        assert engine is engine2

    def test_get_missing(self):
        mgr = self._make_manager()
        assert mgr.get("c1", "/tmp") is None

    def test_get_active_engine(self):
        mgr = self._make_manager()
        engine = mgr.get_or_create("c1", "/tmp/test")
        assert mgr.get_active_engine("c1") is None
        engine._is_running = True
        assert mgr.get_active_engine("c1") is engine

    def test_get_active_engines(self):
        mgr = self._make_manager()
        e1 = mgr.get_or_create("c1", "/tmp/a")
        e2 = mgr.get_or_create("c1", "/tmp/b")
        assert len(mgr.get_active_engines("c1")) == 0
        e1._is_running = True
        e2._is_running = True
        assert len(mgr.get_active_engines("c1")) == 2

    def test_list_engines(self):
        mgr = self._make_manager()
        mgr.get_or_create("c1", "/tmp/a")
        mgr.get_or_create("c1", "/tmp/b")
        mgr.get_or_create("c2", "/tmp/c")
        assert len(mgr.list_engines("c1")) == 2
        assert len(mgr.list_engines()) == 3

    def test_engine_name_switch(self):
        mgr = self._make_manager()
        e1 = mgr.get_or_create("c1", "/tmp/test", engine_name="Coco")
        assert e1.engine_name == "Coco"
        e2 = mgr.get_or_create("c1", "/tmp/test", engine_name="Claude")
        assert e2.engine_name == "Claude"
        assert e1 is not e2

    def test_cleanup_all(self):
        mgr = self._make_manager()
        mgr.get_or_create("c1", "/tmp/test")
        mgr.cleanup_all()
        assert mgr.get("c1", "/tmp/test") is None


# ===========================================================================
# TestRoleRouter
# ===========================================================================


class TestRoleRouter:
    def _make_state(
        self,
        iteration=1,
        consecutive_failures=0,
        last_role=None,
        satisfied=None,
        criteria=None,
        recent=None,
    ):
        criteria = criteria or ["c1", "c2", "c3"]
        tracker = CriteriaTracker()
        tracker.init_criteria(criteria)
        if satisfied:
            for cid, val in satisfied.items():
                tracker.update(cid, val, 1)
        return IterationState(
            iteration_number=iteration,
            requirement=LoopRequirement(
                goal="test", acceptance_criteria=criteria, raw_text="test"
            ),
            criteria_tracker=tracker,
            recent_iterations=recent or [],
            context_summary="",
            consecutive_failures=consecutive_failures,
            last_role=last_role,
        )

    def test_first_iteration_architect(self):
        router = RoleRouter()
        state = self._make_state(iteration=1)
        result = router.select_role(state)
        assert result.role == LoopRole.ARCHITECT

    def test_consecutive_failures_debugger(self):
        router = RoleRouter()
        state = self._make_state(iteration=3, consecutive_failures=2)
        result = router.select_role(state)
        assert result.role == LoopRole.DEBUGGER

    def test_majority_satisfied_tester(self):
        router = RoleRouter()
        state = self._make_state(
            iteration=4,
            satisfied={0: True, 1: True},  # 2/3 = 66% > 60%
        )
        result = router.select_role(state)
        assert result.role == LoopRole.TESTER

    def test_has_tester_and_high_satisfaction_integrator(self):
        router = RoleRouter()
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.TESTER, status=IterationStatus.SUCCESS
            )
        ]
        state = self._make_state(
            iteration=5,
            satisfied={0: True, 1: True, 2: True},  # 100%
            recent=recent,
        )
        result = router.select_role(state)
        assert result.role == LoopRole.INTEGRATOR

    def test_iteration_3_no_review_reviewer(self):
        router = RoleRouter()
        state = self._make_state(iteration=3)
        result = router.select_role(state)
        assert result.role == LoopRole.REVIEWER

    def test_default_developer(self):
        router = RoleRouter()
        # iteration >=3 with existing reviewer → default developer
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.REVIEWER, status=IterationStatus.SUCCESS
            )
        ]
        state = self._make_state(iteration=4, recent=recent)
        result = router.select_role(state)
        assert result.role == LoopRole.DEVELOPER

    def test_get_role_prompt_all_roles(self):
        router = RoleRouter()
        for role in LoopRole:
            prompt = router.get_role_prompt(role)
            assert isinstance(prompt, str)
            assert len(prompt) > 0

    def test_role_prompts_complete(self):
        for role in LoopRole:
            assert role in ROLE_PROMPTS


# ===========================================================================
# TestTerminationChecker
# ===========================================================================


class TestTerminationChecker:
    def _make_project(self, iterations=None, criteria=None, satisfied=None):
        project = LoopProject.create(name="test", root_path="/tmp")
        criteria = criteria or ["c1", "c2"]
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=criteria,
                raw_text="test",
            )
        )
        if satisfied:
            for cid, val in satisfied.items():
                project.criteria_tracker.update(cid, val, 1)
        if iterations:
            project.iterations = iterations
        return project

    def test_user_stop(self):
        checker = TerminationChecker()
        project = self._make_project()
        result = checker.evaluate(project, should_stop=True)
        assert result.signal == TerminationSignal.USER_STOP

    def test_fatal_consecutive_failures(self):
        checker = TerminationChecker()
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.FAILED)
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.FATAL

    def test_max_iter(self):
        checker = TerminationChecker(max_iterations=3)
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.SUCCESS)
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.MAX_ITER

    def test_complete_all_satisfied(self):
        checker = TerminationChecker()
        project = self._make_project(satisfied={0: True, 1: True})
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.COMPLETE

    def test_continue_default(self):
        checker = TerminationChecker()
        project = self._make_project()
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.CONTINUE

    def test_convergence_same_role_no_progress(self):
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=i,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={},
            )
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.CONVERGED

    def test_convergence_similar_output_no_progress(self):
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=i,
                role=LoopRole(["developer", "reviewer", "tester"][i - 1]),
                status=IterationStatus.SUCCESS,
                output="x" * 100,  # same length
                criteria_progress={},
            )
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.CONVERGED

    def test_no_convergence_with_progress(self):
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=i,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={0: True},  # has progress
            )
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.CONTINUE

    def test_no_convergence_not_enough_iterations(self):
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=1, status=IterationStatus.SUCCESS, criteria_progress={}
            )
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.CONTINUE

    def test_priority_user_stop_over_fatal(self):
        """USER_STOP has higher priority than FATAL."""
        checker = TerminationChecker()
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.FAILED)
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project, should_stop=True)
        assert result.signal == TerminationSignal.USER_STOP


# ===========================================================================
# TestRequirementAnalyzer
# ===========================================================================


class TestRequirementAnalyzer:
    def test_fallback_parse_with_criteria(self):
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("实现登录\n- 支持邮箱\n- 支持手机号\n- 有错误提示")
        assert len(req.acceptance_criteria) == 3
        assert "支持邮箱" in req.acceptance_criteria

    def test_fallback_parse_checkbox(self):
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("功能\n[ ] 第一项\n[x] 第二项")
        assert len(req.acceptance_criteria) == 2

    def test_fallback_parse_no_criteria(self):
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("实现登录功能")
        assert len(req.acceptance_criteria) == 1
        assert "完成需求" in req.acceptance_criteria[0]

    def test_llm_analyze_success(self):
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = """```json
{
  "goal": "实现用户登录",
  "acceptance_criteria": ["邮箱注册", "手机号注册", "密码验证"],
  "constraints": ["使用现有技术栈"],
  "estimated_iterations": 5
}
```"""
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        req = analyzer.analyze("实现登录注册")
        assert req.goal == "实现用户登录"
        assert len(req.acceptance_criteria) == 3
        assert req.constraints == ["使用现有技术栈"]
        assert req.estimated_iterations == 5

    def test_llm_analyze_fallback_on_error(self):
        mock_session = MagicMock()
        mock_session.send_prompt.side_effect = Exception("LLM timeout")
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        req = analyzer.analyze("实现登录\n- 邮箱\n- 手机号")
        # Should fallback to text parsing
        assert len(req.acceptance_criteria) == 2

    def test_llm_analyze_fallback_on_bad_json(self):
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = "This is not JSON at all"
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        req = analyzer.analyze("实现登录\n- 邮箱")
        assert len(req.acceptance_criteria) == 1
        assert "邮箱" in req.acceptance_criteria[0]

    def test_extract_json_from_code_block(self):
        text = 'Some text\n```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```\nMore text'
        data = RequirementAnalyzer._extract_json(text)
        assert data["goal"] == "test"

    def test_extract_json_direct(self):
        text = '{"goal": "test", "acceptance_criteria": ["c1"]}'
        data = RequirementAnalyzer._extract_json(text)
        assert data["goal"] == "test"

    def test_llm_analyze_missing_fields_fallback(self):
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = (
            '```json\n{"goal": "", "acceptance_criteria": []}\n```'
        )
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        req = analyzer.analyze("需求\n- c1")
        # Empty goal/criteria from LLM → fallback
        assert len(req.acceptance_criteria) >= 1


# ===========================================================================
# TestLoopModels
# ===========================================================================


class TestLoopModels:
    def test_loop_project_create(self):
        project = LoopProject.create(name="test", root_path="/tmp/test")
        assert project.name == "test"
        assert project.status == LoopProjectStatus.IDLE
        assert project.current_iteration == 0

    def test_loop_project_lifecycle(self):
        project = LoopProject.create(name="test", root_path="/tmp/test")
        project.start()
        assert project.status == LoopProjectStatus.RUNNING
        project.pause()
        assert project.status == LoopProjectStatus.PAUSED
        project.resume()
        assert project.status == LoopProjectStatus.RUNNING
        project.complete()
        assert project.status == LoopProjectStatus.COMPLETED
        assert project.completed_at is not None

    def test_loop_project_abort(self):
        project = LoopProject.create(name="test", root_path="/tmp/test")
        project.abort("fatal error")
        assert project.status == LoopProjectStatus.ABORTED
        assert project.error == "fatal error"

    def test_loop_project_set_requirement(self):
        project = LoopProject.create(name="test", root_path="/tmp/test")
        req = LoopRequirement(
            goal="login",
            acceptance_criteria=["email", "phone"],
            raw_text="test",
        )
        project.set_requirement(req)
        assert project.total_criteria == 2
        assert not project.is_all_satisfied

    def test_loop_project_serialization(self):
        project = LoopProject.create(name="test", root_path="/tmp/test")
        req = LoopRequirement(
            goal="login",
            acceptance_criteria=["email"],
            raw_text="test",
        )
        project.set_requirement(req)
        data = project.to_dict()
        restored = LoopProject.from_dict(data)
        assert restored.name == "test"
        assert restored.total_criteria == 1

    def test_iteration_record_complete(self):
        record = IterationRecord(iteration=1)
        record.complete("output text", "summary text", {0: True})
        assert record.status == IterationStatus.SUCCESS
        assert record.output == "output text"
        assert record.completed_at is not None

    def test_iteration_record_fail(self):
        record = IterationRecord(iteration=1)
        record.fail("error msg", "partial output")
        assert record.status == IterationStatus.FAILED
        assert record.error == "error msg"

    def test_iteration_record_serialization(self):
        record = IterationRecord(iteration=1, role=LoopRole.DEVELOPER, focus="coding")
        data = record.to_dict()
        restored = IterationRecord.from_dict(data)
        assert restored.iteration == 1
        assert restored.role == LoopRole.DEVELOPER
        assert restored.focus == "coding"

    def test_criteria_tracker(self):
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1", "c2", "c3"])
        assert tracker.total_count == 3
        assert tracker.satisfied_count == 0

        tracker.update(0, True, 1)
        assert tracker.satisfied_count == 1
        assert tracker.satisfied_at_iteration[0] == 1

        tracker.batch_update({1: True, 2: False}, 2)
        assert tracker.satisfied_count == 2
        assert not tracker.is_all_satisfied

        tracker.update(2, True, 3)
        assert tracker.is_all_satisfied

    def test_criteria_tracker_serialization(self):
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1", "c2"])
        tracker.update(0, True, 1)
        data = tracker.to_dict()
        restored = CriteriaTracker.from_dict(data)
        assert restored.satisfied_count == 1
        assert restored.criteria == ["c1", "c2"]

    def test_criteria_tracker_invalid_id(self):
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1"])
        tracker.update(-1, True, 1)
        tracker.update(5, True, 1)
        assert tracker.satisfied_count == 0

    def test_loop_requirement_serialization(self):
        req = LoopRequirement(
            goal="test",
            acceptance_criteria=["c1", "c2"],
            constraints=["fast"],
            raw_text="raw",
        )
        data = req.to_dict()
        restored = LoopRequirement.from_dict(data)
        assert restored.goal == "test"
        assert len(restored.acceptance_criteria) == 2
        assert restored.constraints == ["fast"]

    def test_loop_role_properties(self):
        for role in LoopRole:
            assert role.display_name
            assert role.emoji

    def test_termination_signal_values(self):
        assert TerminationSignal.CONTINUE.value == "continue"
        assert TerminationSignal.COMPLETE.value == "complete"
        assert TerminationSignal.CONVERGED.value == "converged"
        assert TerminationSignal.MAX_ITER.value == "max_iter"
        assert TerminationSignal.FATAL.value == "fatal"
        assert TerminationSignal.USER_STOP.value == "user_stop"

    def test_loop_project_counters(self):
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations.append(
            IterationRecord(iteration=1, status=IterationStatus.SUCCESS)
        )
        project.iterations.append(
            IterationRecord(iteration=2, status=IterationStatus.FAILED)
        )
        project.iterations.append(
            IterationRecord(iteration=3, status=IterationStatus.FAILED)
        )
        assert project.success_count == 1
        assert project.failure_count == 2
        assert project.consecutive_failures == 2
        assert project.current_iteration == 3

    def test_loop_project_auto_name(self):
        project = LoopProject.create(root_path="/tmp/myproject")
        assert project.name == "myproject"


# ===========================================================================
# TestLoopContextManager
# ===========================================================================


class TestLoopContextManager:
    def test_record_and_count(self):
        ctx = LoopContextManager()
        assert ctx.iteration_count == 0
        ctx.record_iteration(IterationRecord(iteration=1, role=LoopRole.DEVELOPER))
        assert ctx.iteration_count == 1

    def test_user_guidance(self):
        ctx = LoopContextManager()
        assert not ctx.has_user_guidance()
        assert ctx.consume_user_guidance() is None
        ctx.inject_user_guidance("do X")
        assert ctx.has_user_guidance()
        result = ctx.consume_user_guidance()
        assert result == "do X"
        assert not ctx.has_user_guidance()

    def test_build_context_prompt_empty(self):
        ctx = LoopContextManager()
        assert ctx.build_context_prompt() == ""

    def test_build_context_prompt_with_iterations(self):
        ctx = LoopContextManager()
        record = IterationRecord(
            iteration=1,
            role=LoopRole.DEVELOPER,
            status=IterationStatus.SUCCESS,
            summary="did stuff",
            output="some output",
        )
        ctx.record_iteration(record)
        result = ctx.build_context_prompt()
        assert "迭代历史" in result

    def test_get_iteration_summaries(self):
        ctx = LoopContextManager()
        ctx.record_iteration(
            IterationRecord(iteration=1, role=LoopRole.DEVELOPER, summary="step1")
        )
        ctx.record_iteration(
            IterationRecord(iteration=2, role=LoopRole.TESTER, summary="")
        )
        summaries = ctx.get_iteration_summaries()
        assert summaries == ["step1"]


# ===========================================================================
# TestLoopReporter
# ===========================================================================


class TestLoopReporter:
    def _make_project(self) -> LoopProject:
        project = LoopProject.create(name="test_project", root_path="/tmp/test")
        project.set_requirement(
            LoopRequirement(
                goal="implement login",
                acceptance_criteria=["email login", "phone login"],
                raw_text="test",
            )
        )
        return project

    def test_format_analyzing_start(self):
        reporter = LoopReporter()
        result = reporter.format_analyzing_start("implement login")
        assert "Loop Agent" in result
        assert "implement login" in result

    def test_format_analyzing_done(self):
        reporter = LoopReporter()
        project = self._make_project()
        result = reporter.format_analyzing_done(project)
        assert "需求分析完成" in result
        assert "email login" in result

    def test_format_analyzing_done_no_requirement(self):
        reporter = LoopReporter()
        project = LoopProject.create(name="test", root_path="/tmp")
        result = reporter.format_analyzing_done(project)
        assert "失败" in result

    def test_format_iteration_start(self):
        reporter = LoopReporter()
        result = reporter.format_iteration_start(1, 5)
        assert "1/5" in result

    def test_format_iteration_done_success(self):
        reporter = LoopReporter()
        record = IterationRecord(
            iteration=1, focus="Implement email", status=IterationStatus.SUCCESS
        )
        result = reporter.format_iteration_done(1, record)
        assert "完成" in result
        assert "Implement email" in result

    def test_format_iteration_done_failed(self):
        reporter = LoopReporter()
        record = IterationRecord(
            iteration=1, status=IterationStatus.FAILED, error="timeout"
        )
        result = reporter.format_iteration_done(1, record)
        assert "失败" in result
        assert "timeout" in result

    def test_format_project_done_completed(self):
        reporter = LoopReporter()
        project = self._make_project()
        project.status = LoopProjectStatus.COMPLETED
        result = reporter.format_project_done(project)
        assert "完成" in result

    def test_format_project_done_aborted(self):
        reporter = LoopReporter()
        project = self._make_project()
        project.status = LoopProjectStatus.ABORTED
        project.error = "fatal error"
        result = reporter.format_project_done(project)
        assert "终止" in result

    def test_format_status(self):
        reporter = LoopReporter()
        project = self._make_project()
        project.status = LoopProjectStatus.RUNNING
        result = reporter.format_status(project)
        assert "执行中" in result

    def test_format_error(self):
        reporter = LoopReporter()
        result = reporter.format_error("something broke")
        assert "something broke" in result

    def test_progress_bar(self):
        reporter = LoopReporter()
        bar = reporter._make_progress_bar(3, 5)
        assert "60%" in bar

    def test_progress_bar_zero(self):
        reporter = LoopReporter()
        bar = reporter._make_progress_bar(0, 0)
        assert "0%" in bar

    def test_get_progress_info(self):
        reporter = LoopReporter()
        project = self._make_project()
        project.status = LoopProjectStatus.RUNNING
        info = reporter.get_progress_info(project)
        assert info["is_running"]
        assert info["total_criteria"] == 2

    def test_format_guidance_injected(self):
        reporter = LoopReporter()
        result = reporter.format_guidance_injected("focus on email")
        assert "focus on email" in result

    def test_format_criteria_update(self):
        reporter = LoopReporter()
        project = self._make_project()
        project.criteria_tracker.update(0, True, 1)
        result = reporter.format_criteria_update(project)
        assert "验收标准" in result
        assert "✅" in result

    def test_title_helpers(self):
        reporter = LoopReporter()
        assert "Loop Agent" in reporter.get_analyzing_start_title()
        assert "分析完成" in reporter.get_analyzing_done_title()
        assert "1/5" in reporter.get_iteration_start_title(1, 5)
        assert "完成" in reporter.get_iteration_done_title(True, 1)
        assert "失败" in reporter.get_iteration_done_title(False, 1)
        assert "错误" in reporter.get_error_title()
        assert "状态" in reporter.get_status_title()
        assert "标准" in reporter.get_criteria_update_title()
        assert "引导" in reporter.get_guidance_injected_title()

    def test_project_done_title_variants(self):
        reporter = LoopReporter()
        p1 = self._make_project()
        p1.status = LoopProjectStatus.COMPLETED
        assert "完成" in reporter.get_project_done_title(p1)

        p2 = self._make_project()
        p2.status = LoopProjectStatus.ABORTED
        assert "终止" in reporter.get_project_done_title(p2)

        p3 = self._make_project()
        p3.status = LoopProjectStatus.PAUSED
        assert "暂停" in reporter.get_project_done_title(p3)


# ===========================================================================
# Extended RoleRouter Tests — edge cases & boundary conditions
# ===========================================================================


class TestRoleRouterEdgeCases:
    """Edge cases and boundary conditions for RoleRouter."""

    def _make_state(
        self,
        iteration=1,
        consecutive_failures=0,
        last_role=None,
        satisfied=None,
        criteria=None,
        recent=None,
    ):
        criteria = criteria or ["c1", "c2", "c3"]
        tracker = CriteriaTracker()
        tracker.init_criteria(criteria)
        if satisfied:
            for cid, val in satisfied.items():
                tracker.update(cid, val, 1)
        return IterationState(
            iteration_number=iteration,
            requirement=LoopRequirement(
                goal="test", acceptance_criteria=criteria, raw_text="test"
            ),
            criteria_tracker=tracker,
            recent_iterations=recent or [],
            context_summary="",
            consecutive_failures=consecutive_failures,
            last_role=last_role,
        )

    def test_empty_criteria_tracker(self):
        """RoleRouter with no criteria should default to DEVELOPER after iteration 1."""
        router = RoleRouter()
        tracker = CriteriaTracker()  # No criteria initialized
        state = IterationState(
            iteration_number=2,
            requirement=LoopRequirement(
                goal="test", acceptance_criteria=[], raw_text="test"
            ),
            criteria_tracker=tracker,
            recent_iterations=[
                IterationRecord(
                    iteration=1, role=LoopRole.ARCHITECT, status=IterationStatus.SUCCESS
                )
            ],
            context_summary="",
        )
        result = router.select_role(state)
        # Empty criteria → satisfied_ratio = 0.0 → no TESTER
        # iteration 2 < 3 → no REVIEWER → default DEVELOPER
        assert result.role == LoopRole.DEVELOPER

    def test_exact_threshold_60_percent(self):
        """Exactly 60% satisfaction should trigger TESTER."""
        router = RoleRouter()
        # 3/5 = 60%
        state = self._make_state(
            iteration=4,
            criteria=["c1", "c2", "c3", "c4", "c5"],
            satisfied={0: True, 1: True, 2: True},
        )
        result = router.select_role(state)
        assert result.role == LoopRole.TESTER

    def test_below_60_percent_no_tester(self):
        """Below 60% satisfaction should not trigger TESTER at iteration 2."""
        router = RoleRouter()
        # 1/5 = 20% < 60%
        state = self._make_state(
            iteration=2,
            criteria=["c1", "c2", "c3", "c4", "c5"],
            satisfied={0: True},
        )
        result = router.select_role(state)
        assert result.role == LoopRole.DEVELOPER

    def test_exact_threshold_80_percent_with_tester(self):
        """Exactly 80% satisfaction with TESTER history triggers INTEGRATOR."""
        router = RoleRouter()
        # 4/5 = 80%
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.TESTER, status=IterationStatus.SUCCESS
            )
        ]
        state = self._make_state(
            iteration=5,
            criteria=["c1", "c2", "c3", "c4", "c5"],
            satisfied={0: True, 1: True, 2: True, 3: True},
            recent=recent,
        )
        result = router.select_role(state)
        assert result.role == LoopRole.INTEGRATOR

    def test_tester_with_failed_status_not_counted(self):
        """Failed TESTER iteration should not count as has_tester."""
        router = RoleRouter()
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.TESTER, status=IterationStatus.FAILED
            )
        ]
        state = self._make_state(
            iteration=4,
            satisfied={0: True, 1: True},  # 2/3 = 66% > 60%
            recent=recent,
        )
        result = router.select_role(state)
        # has_tester is False (only SUCCESS counts) → TESTER again
        assert result.role == LoopRole.TESTER

    def test_debugger_priority_over_all_others(self):
        """DEBUGGER has higher priority than TESTER/REVIEWER at iteration >= 3."""
        router = RoleRouter()
        state = self._make_state(
            iteration=5,
            consecutive_failures=3,
            satisfied={0: True, 1: True},  # 66% → would trigger TESTER
        )
        result = router.select_role(state)
        assert result.role == LoopRole.DEBUGGER

    def test_first_iteration_always_architect(self):
        """First iteration is ARCHITECT regardless of failure or satisfaction."""
        router = RoleRouter()
        state = self._make_state(
            iteration=1,
            consecutive_failures=5,
            satisfied={0: True, 1: True, 2: True},
        )
        result = router.select_role(state)
        assert result.role == LoopRole.ARCHITECT

    def test_single_criterion_all_satisfied(self):
        """Single criterion all satisfied at iteration 2."""
        router = RoleRouter()
        # 1/1 = 100% → TESTER (no tester history)
        state = self._make_state(
            iteration=2,
            criteria=["c1"],
            satisfied={0: True},
        )
        result = router.select_role(state)
        assert result.role == LoopRole.TESTER

    def test_already_has_integrator_falls_to_reviewer(self):
        """With TESTER+INTEGRATOR done, at iteration >= 3 → REVIEWER."""
        router = RoleRouter()
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.TESTER, status=IterationStatus.SUCCESS
            ),
            IterationRecord(
                iteration=3, role=LoopRole.INTEGRATOR, status=IterationStatus.SUCCESS
            ),
        ]
        state = self._make_state(
            iteration=4,
            satisfied={0: True, 1: True, 2: True},  # 100%
            recent=recent,
        )
        result = router.select_role(state)
        # has_tester=True, has_integrator=True → skip INTEGRATOR
        # iteration>=3 and no reviewer → REVIEWER
        assert result.role == LoopRole.REVIEWER

    def test_all_roles_covered_defaults_to_developer(self):
        """When all special roles have been used, default to DEVELOPER."""
        router = RoleRouter()
        recent = [
            IterationRecord(
                iteration=1, role=LoopRole.ARCHITECT, status=IterationStatus.SUCCESS
            ),
            IterationRecord(
                iteration=2, role=LoopRole.TESTER, status=IterationStatus.SUCCESS
            ),
            IterationRecord(
                iteration=3, role=LoopRole.REVIEWER, status=IterationStatus.SUCCESS
            ),
            IterationRecord(
                iteration=4, role=LoopRole.INTEGRATOR, status=IterationStatus.SUCCESS
            ),
        ]
        state = self._make_state(
            iteration=5,
            criteria=["c1", "c2", "c3", "c4", "c5"],
            satisfied={0: True, 1: True, 2: True, 3: True},  # 80%
            recent=recent,
        )
        result = router.select_role(state)
        # has_tester=True, has_integrator=True → skip
        # has_reviewer=True → skip
        assert result.role == LoopRole.DEVELOPER

    def test_developer_focus_is_unsatisfied_criterion(self):
        """DEVELOPER's focus should be the first unsatisfied criterion."""
        router = RoleRouter()
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.REVIEWER, status=IterationStatus.SUCCESS
            )
        ]
        state = self._make_state(
            iteration=4,
            criteria=["feature A", "feature B", "feature C"],
            satisfied={0: True},  # B and C unsatisfied
            recent=recent,
        )
        result = router.select_role(state)
        assert result.role == LoopRole.DEVELOPER
        assert result.focus == "feature B"

    def test_exactly_2_failures_triggers_debugger(self):
        """Exactly 2 consecutive failures (the threshold) triggers DEBUGGER."""
        router = RoleRouter()
        state = self._make_state(iteration=5, consecutive_failures=2)
        result = router.select_role(state)
        assert result.role == LoopRole.DEBUGGER

    def test_1_failure_no_debugger(self):
        """1 consecutive failure does not trigger DEBUGGER."""
        router = RoleRouter()
        recent = [
            IterationRecord(
                iteration=2, role=LoopRole.REVIEWER, status=IterationStatus.SUCCESS
            )
        ]
        state = self._make_state(iteration=4, consecutive_failures=1, recent=recent)
        result = router.select_role(state)
        assert result.role != LoopRole.DEBUGGER

    def test_role_selection_has_reason(self):
        """Every RoleSelection must have a non-empty reason."""
        router = RoleRouter()
        for iteration in [1, 2, 3, 5]:
            state = self._make_state(iteration=iteration)
            result = router.select_role(state)
            assert result.reason
            assert result.focus


# ===========================================================================
# Extended TerminationChecker Tests — edge cases & boundary conditions
# ===========================================================================


class TestTerminationCheckerEdgeCases:
    """Edge cases and boundary conditions for TerminationChecker."""

    def _make_project(self, iterations=None, criteria=None, satisfied=None):
        project = LoopProject.create(name="test", root_path="/tmp")
        criteria = criteria or ["c1", "c2"]
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=criteria,
                raw_text="test",
            )
        )
        if satisfied:
            for cid, val in satisfied.items():
                project.criteria_tracker.update(cid, val, 1)
        if iterations:
            project.iterations = iterations
        return project

    def test_empty_project(self):
        """Empty project with no iterations should CONTINUE."""
        checker = TerminationChecker()
        project = self._make_project()
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.CONTINUE

    def test_exactly_2_failures_not_fatal(self):
        """Exactly 2 consecutive failures is not FATAL (threshold is 3)."""
        checker = TerminationChecker()
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.FAILED)
            for i in range(1, 3)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal != TerminationSignal.FATAL

    def test_max_iter_boundary_at_exact(self):
        """current_iteration == max_iterations should trigger MAX_ITER."""
        checker = TerminationChecker(max_iterations=5)
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.SUCCESS)
            for i in range(1, 6)
        ]
        project = self._make_project(iterations=iters)
        assert project.current_iteration == 5
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.MAX_ITER

    def test_max_iter_below_boundary(self):
        """current_iteration < max_iterations should not trigger MAX_ITER."""
        checker = TerminationChecker(max_iterations=5)
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.SUCCESS)
            for i in range(1, 5)  # 4 iterations
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal != TerminationSignal.MAX_ITER

    def test_convergence_window_1(self):
        """Convergence window of 1 — single iteration with no progress."""
        checker = TerminationChecker(convergence_window=1)
        iters = [
            IterationRecord(
                iteration=1,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={},
            ),
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        # condition_a: True (no progress), condition_c: 1 role in 1 window → same role
        assert result.signal == TerminationSignal.CONVERGED

    def test_convergence_not_triggered_with_varied_output_lengths(self):
        """Different output lengths should not trigger convergence via condition_b."""
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=1,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={},
            ),
            IterationRecord(
                iteration=2,
                role=LoopRole.REVIEWER,
                status=IterationStatus.SUCCESS,
                output="y" * 200,  # 100% diff
                criteria_progress={},
            ),
            IterationRecord(
                iteration=3,
                role=LoopRole.TESTER,
                status=IterationStatus.SUCCESS,
                output="z" * 400,  # very different
                criteria_progress={},
            ),
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        # condition_b: False (varied lengths), condition_c: False (different roles)
        assert result.signal == TerminationSignal.CONTINUE

    def test_convergence_mixed_empty_output(self):
        """Some empty outputs don't trigger length-based convergence."""
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=1,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                output="",
                criteria_progress={},
            ),
            IterationRecord(
                iteration=2,
                role=LoopRole.REVIEWER,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={},
            ),
            IterationRecord(
                iteration=3,
                role=LoopRole.TESTER,
                status=IterationStatus.SUCCESS,
                output="",
                criteria_progress={},
            ),
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        # Only 1 non-empty output → can't compare, condition_b = False
        # 3 different roles → condition_c = False
        assert result.signal == TerminationSignal.CONTINUE

    def test_convergence_none_roles_not_matched(self):
        """Iterations with role=None should not satisfy same-role condition."""
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=i,
                role=None,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={},
            )
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        # role=None → roles list is empty → len(roles) != convergence_window → condition_c = False
        # condition_b may be True (same output length)
        # condition_a: True, condition_b: True → CONVERGED
        assert result.signal == TerminationSignal.CONVERGED

    def test_complete_takes_priority_over_convergence(self):
        """COMPLETE has higher priority than CONVERGED."""
        checker = TerminationChecker(convergence_window=3)
        iters = [
            IterationRecord(
                iteration=i,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                output="x" * 100,
                criteria_progress={},
            )
            for i in range(1, 4)
        ]
        project = self._make_project(
            iterations=iters,
            satisfied={0: True, 1: True},  # all satisfied
        )
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.COMPLETE

    def test_fatal_takes_priority_over_max_iter(self):
        """FATAL (3 consecutive failures) has higher priority than MAX_ITER."""
        checker = TerminationChecker(max_iterations=3)
        iters = [
            IterationRecord(iteration=i, status=IterationStatus.FAILED)
            for i in range(1, 4)
        ]
        project = self._make_project(iterations=iters)
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.FATAL

    def test_non_consecutive_failures_not_fatal(self):
        """Non-consecutive failures should not trigger FATAL."""
        checker = TerminationChecker()
        iters = [
            IterationRecord(iteration=1, status=IterationStatus.FAILED),
            IterationRecord(iteration=2, status=IterationStatus.SUCCESS),
            IterationRecord(iteration=3, status=IterationStatus.FAILED),
            IterationRecord(iteration=4, status=IterationStatus.FAILED),
        ]
        project = self._make_project(iterations=iters)
        assert project.consecutive_failures == 2
        result = checker.evaluate(project)
        assert result.signal != TerminationSignal.FATAL

    def test_single_criterion_complete(self):
        """Single criterion fully satisfied triggers COMPLETE."""
        checker = TerminationChecker()
        project = self._make_project(criteria=["c1"], satisfied={0: True})
        result = checker.evaluate(project)
        assert result.signal == TerminationSignal.COMPLETE

    def test_reason_is_non_empty(self):
        """Every TerminationResult should have a non-empty reason."""
        checker = TerminationChecker()
        project = self._make_project()
        for should_stop in [True, False]:
            result = checker.evaluate(project, should_stop=should_stop)
            assert result.reason


# ===========================================================================
# Extended RequirementAnalyzer Tests — edge cases & boundary conditions
# ===========================================================================


class TestRequirementAnalyzerEdgeCases:
    """Edge cases and boundary conditions for RequirementAnalyzer."""

    def test_empty_text_returns_default_criterion(self):
        """Empty text should produce a fallback criterion."""
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("")
        assert len(req.acceptance_criteria) == 1
        assert "完成需求" in req.acceptance_criteria[0]

    def test_whitespace_only_text(self):
        """Whitespace-only text should produce a fallback criterion."""
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("   \n\n   ")
        assert len(req.acceptance_criteria) >= 1

    def test_numbered_list_parsing(self):
        """Numbered list items (1., 2.) are not list markers for fallback."""
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("需求\n1. 第一项\n2. 第二项")
        # Numbered lists are NOT parsed by fallback (only - and * and [ ])
        assert len(req.acceptance_criteria) == 1
        assert "完成需求" in req.acceptance_criteria[0]

    def test_mixed_list_formats(self):
        """Mixed - and * list items should all be parsed."""
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("任务\n- 第一个\n* 第二个\n- 第三个")
        assert len(req.acceptance_criteria) == 3

    def test_asterisk_list_items(self):
        """Asterisk (*) list items should be parsed."""
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("功能\n* 支持登录\n* 支持注册")
        assert len(req.acceptance_criteria) == 2
        assert "支持登录" in req.acceptance_criteria

    def test_checkbox_mixed_states(self):
        """Both checked and unchecked checkboxes should be parsed."""
        analyzer = RequirementAnalyzer()
        req = analyzer.analyze("tasks\n[ ] not done\n[x] done\n[ ] another")
        assert len(req.acceptance_criteria) == 3

    def test_llm_returns_extra_fields(self):
        """LLM returning extra fields should be ignored gracefully."""
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = """```json
{
  "goal": "build auth",
  "acceptance_criteria": ["login works"],
  "constraints": [],
  "estimated_iterations": 3,
  "extra_field": "should be ignored"
}
```"""
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        req = analyzer.analyze("build auth")
        assert req.goal == "build auth"
        assert len(req.acceptance_criteria) == 1

    def test_llm_returns_no_constraints(self):
        """LLM output without constraints field should default to empty list."""
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = (
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```'
        )
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        req = analyzer.analyze("test")
        assert req.constraints == []
        assert req.estimated_iterations == 6  # default

    def test_analyze_with_override_session(self):
        """Passing session in analyze() overrides constructor session."""
        default_session = MagicMock()
        default_session.send_prompt.side_effect = Exception("should not be called")

        override_session = MagicMock()
        override_session.send_prompt.return_value = (
            '```json\n{"goal": "override", "acceptance_criteria": ["c1"]}\n```'
        )

        analyzer = RequirementAnalyzer(session=default_session, cwd="/tmp")
        req = analyzer.analyze("test", session=override_session)
        assert req.goal == "override"
        override_session.send_prompt.assert_called_once()

    def test_analyze_with_override_cwd(self):
        """Passing cwd in analyze() should use the override cwd."""
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = (
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```'
        )
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/default")
        analyzer.analyze("test", cwd="/override")
        call_args = mock_session.send_prompt.call_args
        assert "/override" in call_args.kwargs.get("cwd", "") or "/override" in str(
            call_args
        )

    def test_extract_json_with_extra_whitespace(self):
        """JSON extraction should handle extra whitespace in code block."""
        text = (
            '```json\n  \n  {"goal": "test", "acceptance_criteria": ["c1"]}  \n  \n```'
        )
        data = RequirementAnalyzer._extract_json(text)
        assert data["goal"] == "test"

    def test_extract_json_invalid_raises(self):
        """Invalid JSON should raise an exception."""
        with pytest.raises(Exception):
            RequirementAnalyzer._extract_json("not json at all {{}}")

    def test_no_session_uses_fallback(self):
        """Without a session, always uses fallback parsing."""
        analyzer = RequirementAnalyzer()  # no session
        req = analyzer.analyze("需求\n- 标准1\n- 标准2")
        assert len(req.acceptance_criteria) == 2

    def test_fallback_preserves_raw_text(self):
        """Fallback parsing should preserve raw_text."""
        analyzer = RequirementAnalyzer()
        text = "实现登录功能"
        req = analyzer.analyze(text)
        assert req.raw_text == text

    def test_llm_analyze_preserves_raw_text(self):
        """LLM parsing should preserve raw_text."""
        mock_session = MagicMock()
        mock_session.send_prompt.return_value = (
            '```json\n{"goal": "login", "acceptance_criteria": ["c1"]}\n```'
        )
        analyzer = RequirementAnalyzer(session=mock_session, cwd="/tmp")
        raw = "原始文本"
        req = analyzer.analyze(raw)
        assert req.raw_text == raw

    def test_long_text_no_criteria_truncation(self):
        """Very long text without list markers generates a truncated fallback criterion."""
        analyzer = RequirementAnalyzer()
        long_text = "A" * 500
        req = analyzer.analyze(long_text)
        assert len(req.acceptance_criteria) == 1
        assert len(req.acceptance_criteria[0]) <= 110  # "完成需求: " + 100 chars


# ===========================================================================
# Engine Integration Tests — full execute flow with mocked session
# ===========================================================================


class TestLoopEngineIntegration:
    """Integration tests for LoopEngine.execute() with mocked AI session."""

    @patch("src.loop_engine.engine.get_settings")
    def _make_engine(self, mock_settings, max_iterations=5):
        s = _mock_settings()
        s.loop_max_iterations = max_iterations
        mock_settings.return_value = s

        mock_session_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session_mgr.start_session.return_value = mock_session

        engine = LoopEngine(
            chat_id="c1",
            root_path="/tmp/test",
            session_manager=mock_session_mgr,
        )
        return engine, mock_session

    def test_execute_success_all_criteria_met(self):
        """Execute should complete when criteria are satisfied via evaluate."""
        engine, mock_session = self._make_engine(max_iterations=5)

        # send_prompt_streaming returns text
        mock_session.send_prompt_streaming.return_value = "All done"

        # send_prompt (criteria evaluation) returns PASS for all
        mock_session.send_prompt.side_effect = [
            # First call: RequirementAnalyzer._llm_analyze
            '```json\n{"goal": "login", "acceptance_criteria": ["email", "phone"]}\n```',
            # Iteration 1: criteria eval
            "CRITERIA_1: PASS\nCRITERIA_2: PASS",
        ]

        callbacks = LoopEngineCallbacks()
        called_events = {
            "analyzing_start": 0,
            "analyzing_done": 0,
            "iteration_start": 0,
            "iteration_done": 0,
            "criteria_update": 0,
            "project_done": 0,
        }

        callbacks.on_analyzing_start = lambda t: called_events.__setitem__(
            "analyzing_start", called_events["analyzing_start"] + 1
        )
        callbacks.on_analyzing_done = lambda p: called_events.__setitem__(
            "analyzing_done", called_events["analyzing_done"] + 1
        )
        callbacks.on_iteration_start = lambda c, m, r: called_events.__setitem__(
            "iteration_start", called_events["iteration_start"] + 1
        )
        callbacks.on_iteration_done = lambda i, r: called_events.__setitem__(
            "iteration_done", called_events["iteration_done"] + 1
        )
        callbacks.on_criteria_update = lambda p: called_events.__setitem__(
            "criteria_update", called_events["criteria_update"] + 1
        )
        callbacks.on_project_done = lambda p: called_events.__setitem__(
            "project_done", called_events["project_done"] + 1
        )

        project = engine.execute("实现登录\n- email\n- phone", callbacks)

        assert project.status == LoopProjectStatus.COMPLETED
        assert project.is_all_satisfied
        assert called_events["analyzing_start"] == 1
        assert called_events["analyzing_done"] == 1
        assert called_events["iteration_start"] >= 1
        assert called_events["iteration_done"] >= 1
        assert called_events["project_done"] == 1
        assert not engine.is_running

    def test_execute_max_iterations_abort(self):
        """Execute should abort when max_iterations is reached."""
        engine, mock_session = self._make_engine(max_iterations=2)

        mock_session.send_prompt_streaming.return_value = "working..."
        mock_session.send_prompt.side_effect = [
            # RequirementAnalyzer
            '```json\n{"goal": "test", "acceptance_criteria": ["c1", "c2"]}\n```',
            # Iteration 1 criteria eval
            "CRITERIA_1: FAIL\nCRITERIA_2: FAIL",
            # Iteration 2 criteria eval
            "CRITERIA_1: FAIL\nCRITERIA_2: FAIL",
        ]

        project = engine.execute("test\n- c1\n- c2")

        assert project.status == LoopProjectStatus.ABORTED
        assert project.current_iteration == 2
        assert not engine.is_running

    def test_execute_user_stop_during_iteration(self):
        """User stop should pause the project."""
        engine, mock_session = self._make_engine(max_iterations=10)

        call_count = [0]

        def streaming_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                engine._should_stop = True
            return "output"

        mock_session.send_prompt_streaming.side_effect = streaming_side_effect
        mock_session.send_prompt.side_effect = [
            # RequirementAnalyzer
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```',
            # Iteration 1 eval
            "CRITERIA_1: FAIL",
            # Iteration 2 eval
            "CRITERIA_1: FAIL",
        ]

        project = engine.execute("test")

        assert project.status == LoopProjectStatus.PAUSED
        assert not engine.is_running

    def test_execute_with_iteration_failure_recovery(self):
        """Engine should continue after a non-fatal iteration failure."""
        engine, mock_session = self._make_engine(max_iterations=5)

        call_count = [0]

        def streaming_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("timeout")
            return "output"

        mock_session.send_prompt_streaming.side_effect = streaming_side_effect
        mock_session.send_prompt.side_effect = [
            # RequirementAnalyzer
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```',
            # Iteration 1 eval (success)
            "CRITERIA_1: FAIL",
            # Iteration 2: streaming raises, no eval call
            # Iteration 3 eval
            "CRITERIA_1: PASS",
        ]

        project = engine.execute("test")

        assert project.status == LoopProjectStatus.COMPLETED
        assert project.failure_count == 1
        assert project.success_count >= 1

    def test_execute_fatal_consecutive_failures(self):
        """3 consecutive failures should trigger FATAL abort."""
        engine, mock_session = self._make_engine(max_iterations=10)

        mock_session.send_prompt_streaming.side_effect = Exception("always fail")
        mock_session.send_prompt.side_effect = [
            # RequirementAnalyzer fallback (LLM also fails)
            Exception("LLM down"),
        ]

        project = engine.execute("test\n- c1\n- c2")

        # After 3 consecutive failures → FATAL
        assert project.status == LoopProjectStatus.ABORTED
        assert project.failure_count == 3
        assert not engine.is_running

    def test_execute_llm_analyzer_fallback(self):
        """When LLM analyzer fails, fallback text parsing should work."""
        engine, mock_session = self._make_engine(max_iterations=2)

        mock_session.send_prompt_streaming.return_value = "done"
        # LLM returns non-JSON (fallback to text parse)
        mock_session.send_prompt.side_effect = [
            "not json",  # RequirementAnalyzer LLM fails → fallback
            "CRITERIA_1: PASS\nCRITERIA_2: PASS",  # Iteration 1 eval
        ]

        project = engine.execute("需求\n- 标准A\n- 标准B")

        assert project.requirement is not None
        assert len(project.requirement.acceptance_criteria) == 2
        assert project.status == LoopProjectStatus.COMPLETED

    def test_execute_criteria_evaluation_failure_graceful(self):
        """Criteria evaluation failure should not crash the engine."""
        engine, mock_session = self._make_engine(max_iterations=3)

        mock_session.send_prompt_streaming.return_value = "output"
        mock_session.send_prompt.side_effect = [
            # RequirementAnalyzer
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```',
            # Iteration 1 criteria eval fails
            Exception("eval timeout"),
            # Iteration 2 criteria eval succeeds
            "CRITERIA_1: PASS",
        ]

        project = engine.execute("test")

        # Should not crash, criteria not updated on iteration 1 but passes on iteration 2
        assert project.status == LoopProjectStatus.COMPLETED
        assert project.current_iteration == 2

    def test_execute_callbacks_receive_role_selection(self):
        """on_iteration_start callback should receive RoleSelection."""
        engine, mock_session = self._make_engine(max_iterations=2)

        mock_session.send_prompt_streaming.return_value = "done"
        mock_session.send_prompt.side_effect = [
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```',
            "CRITERIA_1: PASS",
        ]

        role_selections = []

        def on_start(current, max_iter, role_sel):
            role_selections.append(role_sel)

        callbacks = LoopEngineCallbacks(on_iteration_start=on_start)
        engine.execute("test", callbacks)

        assert len(role_selections) >= 1
        assert role_selections[0].role == LoopRole.ARCHITECT  # First iteration
        assert role_selections[0].reason

    def test_execute_role_transitions(self):
        """Verify role transitions across multiple iterations."""
        engine, mock_session = self._make_engine(max_iterations=5)

        mock_session.send_prompt_streaming.return_value = "output"
        mock_session.send_prompt.side_effect = [
            # RequirementAnalyzer
            '```json\n{"goal": "test", "acceptance_criteria": ["c1", "c2", "c3"]}\n```',
            # Iter 1 eval
            "CRITERIA_1: FAIL\nCRITERIA_2: FAIL\nCRITERIA_3: FAIL",
            # Iter 2 eval
            "CRITERIA_1: PASS\nCRITERIA_2: FAIL\nCRITERIA_3: FAIL",
            # Iter 3 eval
            "CRITERIA_1: PASS\nCRITERIA_2: PASS\nCRITERIA_3: FAIL",
            # Iter 4 eval
            "CRITERIA_1: PASS\nCRITERIA_2: PASS\nCRITERIA_3: PASS",
        ]

        roles_seen = []

        def on_start(c, m, role_sel):
            roles_seen.append(role_sel.role)

        callbacks = LoopEngineCallbacks(on_iteration_start=on_start)
        project = engine.execute("test", callbacks)

        # First iteration should be ARCHITECT
        assert roles_seen[0] == LoopRole.ARCHITECT
        assert project.status == LoopProjectStatus.COMPLETED
        assert len(roles_seen) >= 2

    def test_execute_exception_in_init_sets_aborted(self):
        """An exception during session init should set ABORTED."""
        engine, mock_session = self._make_engine()
        engine._session_manager.start_session.side_effect = Exception(
            "connection failed"
        )

        project = engine.execute("test")
        assert project.status == LoopProjectStatus.ABORTED

    def test_execute_on_error_callback(self):
        """on_error callback should be called on unexpected exception."""
        engine, mock_session = self._make_engine()
        engine._session_manager.start_session.side_effect = Exception("boom")

        errors = []
        callbacks = LoopEngineCallbacks(on_error=lambda e: errors.append(e))
        engine.execute("test", callbacks)

        assert len(errors) == 1
        assert "boom" in errors[0]

    def test_execute_context_manager_records_iterations(self):
        """Context manager should record all iterations."""
        engine, mock_session = self._make_engine(max_iterations=3)

        mock_session.send_prompt_streaming.return_value = "done"
        mock_session.send_prompt.side_effect = [
            '```json\n{"goal": "test", "acceptance_criteria": ["c1"]}\n```',
            "CRITERIA_1: FAIL",
            "CRITERIA_1: FAIL",
            "CRITERIA_1: PASS",
        ]

        engine.execute("test")

        assert engine._context_manager.iteration_count == 3

    def test_execute_criteria_writeback(self):
        """Criteria should be written back to CriteriaTracker after evaluation."""
        engine, mock_session = self._make_engine(max_iterations=3)

        mock_session.send_prompt_streaming.return_value = "done"
        mock_session.send_prompt.side_effect = [
            '```json\n{"goal": "test", "acceptance_criteria": ["login", "register"]}\n```',
            "CRITERIA_1: PASS\nCRITERIA_2: FAIL",
            "CRITERIA_1: PASS\nCRITERIA_2: PASS",
        ]

        project = engine.execute("test")

        assert project.criteria_tracker.satisfied[0] is True
        assert project.criteria_tracker.satisfied[1] is True
        assert project.criteria_tracker.satisfied_at_iteration[0] == 1
        assert project.criteria_tracker.satisfied_at_iteration[1] == 2


# ===========================================================================
# Extended Models Tests — boundary conditions
# ===========================================================================


class TestModelsEdgeCases:
    """Boundary condition tests for data models."""

    def test_criteria_tracker_batch_update_with_invalid_ids(self):
        """batch_update with out-of-range IDs should silently skip."""
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1", "c2"])
        tracker.batch_update({-1: True, 5: True, 0: True}, 1)
        assert tracker.satisfied_count == 1
        assert tracker.satisfied[0] is True

    def test_criteria_tracker_no_downgrade(self):
        """Once a criterion is satisfied, it cannot be unsatisfied."""
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1"])
        tracker.update(0, True, 1)
        assert tracker.satisfied[0] is True
        tracker.update(0, False, 2)  # Try to downgrade
        assert tracker.satisfied[0] is True

    def test_criteria_tracker_empty_criteria(self):
        """Empty criteria list should have 0 total and not be all_satisfied."""
        tracker = CriteriaTracker()
        tracker.init_criteria([])
        assert tracker.total_count == 0
        assert tracker.satisfied_count == 0
        assert not tracker.is_all_satisfied  # 0 total → False

    def test_criteria_tracker_unsatisfied_criteria_names(self):
        """unsatisfied_criteria should return the correct criterion names."""
        tracker = CriteriaTracker()
        tracker.init_criteria(["login", "register", "reset password"])
        tracker.update(0, True, 1)
        unsatisfied = tracker.unsatisfied_criteria
        assert "login" not in unsatisfied
        assert "register" in unsatisfied
        assert "reset password" in unsatisfied

    def test_criteria_tracker_satisfied_criteria_names(self):
        """satisfied_criteria should return the correct criterion names."""
        tracker = CriteriaTracker()
        tracker.init_criteria(["login", "register"])
        tracker.update(0, True, 1)
        satisfied = tracker.satisfied_criteria
        assert satisfied == ["login"]

    def test_loop_project_duration_not_started(self):
        """duration() should be None if project hasn't started."""
        project = LoopProject.create(name="test", root_path="/tmp")
        assert project.duration() is None

    def test_loop_project_duration_running(self):
        """duration() while running should return time since start."""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.start()
        import time as _time

        _time.sleep(0.01)
        d = project.duration()
        assert d is not None
        assert d > 0

    def test_loop_project_serialization_full_roundtrip(self):
        """Full project serialization with iterations and criteria."""
        project = LoopProject.create(name="test", root_path="/tmp/test")
        req = LoopRequirement(
            goal="login",
            acceptance_criteria=["email", "phone"],
            constraints=["fast"],
            raw_text="test",
            estimated_iterations=4,
        )
        project.set_requirement(req)
        project.start()

        record = IterationRecord(iteration=1, role=LoopRole.DEVELOPER, focus="email")
        record.complete("output", "summary", {0: True})
        project.iterations.append(record)
        project.criteria_tracker.update(0, True, 1)

        data = project.to_dict()
        restored = LoopProject.from_dict(data)

        assert restored.name == "test"
        assert restored.requirement.goal == "login"
        assert restored.requirement.constraints == ["fast"]
        assert len(restored.iterations) == 1
        assert restored.iterations[0].role == LoopRole.DEVELOPER
        assert restored.criteria_tracker.satisfied_count == 1
        assert restored.status == LoopProjectStatus.RUNNING

    def test_iteration_record_from_dict_with_legacy_iteration_id(self):
        """from_dict should support legacy 'iteration_id' key."""
        data = {"iteration_id": 5, "status": "success"}
        record = IterationRecord.from_dict(data)
        assert record.iteration == 5

    def test_loop_project_from_dict_with_no_requirement(self):
        """from_dict should handle missing requirement."""
        data = {
            "project_id": "test",
            "name": "test",
            "root_path": "/tmp",
        }
        project = LoopProject.from_dict(data)
        assert project.requirement is None

    def test_loop_project_consecutive_failures_all_success(self):
        """consecutive_failures should be 0 when all iterations succeed."""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations = [
            IterationRecord(iteration=i, status=IterationStatus.SUCCESS)
            for i in range(1, 4)
        ]
        assert project.consecutive_failures == 0

    def test_loop_project_last_role_empty(self):
        """last_role should be None with no iterations."""
        project = LoopProject.create(name="test", root_path="/tmp")
        assert project.last_role is None

    def test_iteration_state_defaults(self):
        """IterationState default values should be correct."""
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1"])
        state = IterationState(
            iteration_number=1,
            requirement=LoopRequirement(
                goal="test", acceptance_criteria=["c1"], raw_text="test"
            ),
            criteria_tracker=tracker,
            recent_iterations=[],
            context_summary="",
        )
        assert state.user_guidance is None
        assert state.consecutive_failures == 0
        assert state.last_role is None


# ===========================================================================
# Extended LoopContextManager Tests — three-tier compression
# ===========================================================================


class TestLoopContextManagerEdgeCases:
    """Edge cases for LoopContextManager three-tier compression."""

    def test_multiple_guidances_concatenated(self):
        """Multiple guidances should be joined with newlines."""
        ctx = LoopContextManager()
        ctx.inject_user_guidance("first")
        ctx.inject_user_guidance("second")
        result = ctx.consume_user_guidance()
        assert "first" in result
        assert "second" in result
        assert "\n" in result

    def test_three_tier_compression_remote(self):
        """Remote (old) iterations should get 1-line summary."""
        ctx = LoopContextManager()
        for i in range(1, 8):
            record = IterationRecord(
                iteration=i,
                role=LoopRole.DEVELOPER,
                status=IterationStatus.SUCCESS,
                summary=f"step{i}",
                output=f"output{i}" * 100,
            )
            ctx.record_iteration(record)

        prompt = ctx.build_context_prompt(recent_full=1, recent_brief=3)
        # Should contain section header
        assert "迭代历史" in prompt
        # The latest (7th) should have full output (```...```)
        assert "```" in prompt

    def test_three_tier_full_only(self):
        """With only 1 iteration, it should be in 'full' tier."""
        ctx = LoopContextManager()
        record = IterationRecord(
            iteration=1,
            role=LoopRole.ARCHITECT,
            status=IterationStatus.SUCCESS,
            summary="designed",
            output="architecture output",
        )
        ctx.record_iteration(record)
        prompt = ctx.build_context_prompt()
        assert "architecture output" in prompt

    def test_thread_safety_concurrent_record(self):
        """LoopContextManager should be thread-safe for concurrent writes."""
        import threading

        ctx = LoopContextManager()

        def record_iterations(start):
            for i in range(start, start + 50):
                ctx.record_iteration(
                    IterationRecord(
                        iteration=i,
                        role=LoopRole.DEVELOPER,
                        status=IterationStatus.SUCCESS,
                    )
                )

        threads = [
            threading.Thread(target=record_iterations, args=(0,)),
            threading.Thread(target=record_iterations, args=(50,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert ctx.iteration_count == 100

    def test_context_prompt_with_failed_iteration(self):
        """Failed iterations should show failure status in context."""
        ctx = LoopContextManager()
        record = IterationRecord(
            iteration=1,
            role=LoopRole.DEVELOPER,
            status=IterationStatus.FAILED,
            error="timeout",
        )
        ctx.record_iteration(record)
        prompt = ctx.build_context_prompt()
        assert "❌" in prompt

    def test_build_context_prompt_no_role_attribute_error(self):
        """build_context_prompt should handle records even with role set."""
        ctx = LoopContextManager()
        record = IterationRecord(
            iteration=1,
            role=LoopRole.TESTER,
            status=IterationStatus.SUCCESS,
            summary="tested",
        )
        ctx.record_iteration(record)
        # Should not raise
        prompt = ctx.build_context_prompt()
        assert "测试者" in prompt


# ===========================================================================
# Extended LoopReporter Tests — edge cases
# ===========================================================================


class TestLoopReporterEdgeCases:
    """Edge cases for LoopReporter."""

    def test_format_iteration_done_no_focus(self):
        """Iteration done with no focus should show default."""
        reporter = LoopReporter()
        record = IterationRecord(iteration=1, status=IterationStatus.SUCCESS, focus="")
        result = reporter.format_iteration_done(1, record)
        assert "执行完成" in result

    def test_format_iteration_done_no_output(self):
        """Success with no output should not show output preview."""
        reporter = LoopReporter()
        record = IterationRecord(
            iteration=1, status=IterationStatus.SUCCESS, focus="done", output=""
        )
        result = reporter.format_iteration_done(1, record)
        assert "输出预览" not in result

    def test_format_iteration_done_long_output_truncated(self):
        """Long output should be truncated to 200 chars."""
        reporter = LoopReporter()
        record = IterationRecord(
            iteration=1,
            status=IterationStatus.SUCCESS,
            focus="done",
            output="A" * 500,
        )
        result = reporter.format_iteration_done(1, record)
        # Output preview uses output[:200]
        assert "A" * 200 in result

    def test_format_criteria_update_all_satisfied(self):
        """All criteria satisfied should show all checkmarks."""
        reporter = LoopReporter()
        project = LoopProject.create(name="test", root_path="/tmp")
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["a", "b"],
                raw_text="test",
            )
        )
        project.criteria_tracker.update(0, True, 1)
        project.criteria_tracker.update(1, True, 2)
        result = reporter.format_criteria_update(project)
        assert result.count("✅") == 2
        assert "🔲" not in result

    def test_format_criteria_update_none_satisfied(self):
        """No criteria satisfied should show all boxes."""
        reporter = LoopReporter()
        project = LoopProject.create(name="test", root_path="/tmp")
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["a", "b", "c"],
                raw_text="test",
            )
        )
        result = reporter.format_criteria_update(project)
        assert result.count("🔲") == 3
        assert "✅" not in result

    def test_format_status_all_statuses(self):
        """format_status should handle all LoopProjectStatus values."""
        reporter = LoopReporter()
        for status in LoopProjectStatus:
            project = LoopProject.create(name="test", root_path="/tmp")
            project.set_requirement(
                LoopRequirement(
                    goal="test",
                    acceptance_criteria=["c1"],
                    raw_text="test",
                )
            )
            project.status = status
            result = reporter.format_status(project)
            assert isinstance(result, str)
            assert len(result) > 0

    def test_format_project_done_with_duration(self):
        """Completed project should show duration."""
        reporter = LoopReporter()
        project = LoopProject.create(name="test", root_path="/tmp")
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )
        project.start()
        import time as _time

        _time.sleep(0.01)
        project.complete()
        result = reporter.format_project_done(project)
        assert "总耗时" in result

    def test_format_status_with_recent_iterations(self):
        """Status with iterations should show recent iteration list."""
        reporter = LoopReporter()
        project = LoopProject.create(name="test", root_path="/tmp")
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )
        project.status = LoopProjectStatus.RUNNING
        for i in range(1, 4):
            project.iterations.append(
                IterationRecord(
                    iteration=i, status=IterationStatus.SUCCESS, focus=f"task{i}"
                )
            )
        result = reporter.format_status(project)
        assert "最近迭代" in result
        assert "task1" in result

    def test_progress_bar_full(self):
        """100% progress bar."""
        reporter = LoopReporter()
        bar = reporter._make_progress_bar(10, 10)
        assert "100%" in bar
        assert "██████████" in bar

    def test_progress_bar_partial(self):
        """Partial progress bar (e.g. 30%)."""
        reporter = LoopReporter()
        bar = reporter._make_progress_bar(3, 10)
        assert "30%" in bar
        assert "███" in bar

    def test_get_progress_info_paused(self):
        """Progress info for paused project."""
        reporter = LoopReporter()
        project = LoopProject.create(name="test", root_path="/tmp")
        project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )
        project.status = LoopProjectStatus.PAUSED
        info = reporter.get_progress_info(project)
        assert not info["is_running"]
        assert info["is_paused"]


# ===========================================================================
# Engine Boundary Tests — _evaluate_criteria, _apply_termination, etc.
# ===========================================================================


class TestEngineInternalMethods:
    """Tests for internal methods of LoopEngine."""

    @patch("src.loop_engine.engine.get_settings")
    def _make_engine(self, mock_settings):
        mock_settings.return_value = _mock_settings()
        return LoopEngine(chat_id="c1", root_path="/tmp/test")

    def test_evaluate_criteria_partial_match(self):
        """_evaluate_criteria should handle partial PASS/FAIL results."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1", "c2", "c3"],
                raw_text="test",
            )
        )

        mock_session = MagicMock()
        mock_session.send_prompt.return_value = "CRITERIA_1: PASS\nCRITERIA_3: FAIL"
        # Note: CRITERIA_2 is missing

        progress = engine._evaluate_criteria(mock_session, ["c1", "c2", "c3"], 1)

        assert progress[0] is True
        assert 1 not in progress  # CRITERIA_2 not mentioned
        assert progress[2] is False

    def test_evaluate_criteria_chinese_colon(self):
        """_evaluate_criteria should handle Chinese colon (：) separator."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )

        mock_session = MagicMock()
        mock_session.send_prompt.return_value = "CRITERIA_1：PASS"

        progress = engine._evaluate_criteria(mock_session, ["c1"], 1)
        assert progress[0] is True

    def test_evaluate_criteria_case_insensitive(self):
        """_evaluate_criteria should be case-insensitive for PASS/FAIL."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )

        mock_session = MagicMock()
        mock_session.send_prompt.return_value = "criteria_1: pass"

        progress = engine._evaluate_criteria(mock_session, ["c1"], 1)
        assert progress[0] is True

    def test_evaluate_criteria_with_extra_text(self):
        """_evaluate_criteria should extract results from noisy output."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1", "c2"],
                raw_text="test",
            )
        )

        mock_session = MagicMock()
        mock_session.send_prompt.return_value = """
Let me evaluate the criteria:

CRITERIA_1: PASS - The login feature works correctly.
CRITERIA_2: FAIL - The registration is incomplete.

Overall assessment: partial completion.
"""

        progress = engine._evaluate_criteria(mock_session, ["c1", "c2"], 1)
        assert progress[0] is True
        assert progress[1] is False

    def test_apply_termination_complete(self):
        """_apply_termination should set COMPLETED for COMPLETE signal."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.start()

        engine._apply_termination(
            TerminationResult(
                signal=TerminationSignal.COMPLETE,
                reason="all done",
            )
        )
        assert engine._project.status == LoopProjectStatus.COMPLETED

    def test_apply_termination_converged(self):
        """_apply_termination should set COMPLETED for CONVERGED signal."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.start()

        engine._apply_termination(
            TerminationResult(
                signal=TerminationSignal.CONVERGED,
                reason="no progress",
            )
        )
        assert engine._project.status == LoopProjectStatus.COMPLETED

    def test_apply_termination_user_stop(self):
        """_apply_termination should set PAUSED for USER_STOP signal."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.start()

        engine._apply_termination(
            TerminationResult(
                signal=TerminationSignal.USER_STOP,
                reason="user stopped",
            )
        )
        assert engine._project.status == LoopProjectStatus.PAUSED

    def test_apply_termination_fatal(self):
        """_apply_termination should set ABORTED for FATAL signal."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.start()

        engine._apply_termination(
            TerminationResult(
                signal=TerminationSignal.FATAL,
                reason="too many failures",
            )
        )
        assert engine._project.status == LoopProjectStatus.ABORTED
        assert engine._project.error == "too many failures"

    def test_apply_termination_max_iter(self):
        """_apply_termination should set ABORTED for MAX_ITER signal."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.start()

        engine._apply_termination(
            TerminationResult(
                signal=TerminationSignal.MAX_ITER,
                reason="reached limit",
            )
        )
        assert engine._project.status == LoopProjectStatus.ABORTED

    def test_build_role_prompt_no_context_no_guidance(self):
        """_build_role_prompt without context or guidance should still work."""
        engine = self._make_engine()
        req = LoopRequirement(
            goal="test goal", acceptance_criteria=["c1"], raw_text="test"
        )
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1"])
        state = IterationState(
            iteration_number=1,
            requirement=req,
            criteria_tracker=tracker,
            recent_iterations=[],
            context_summary="",
        )
        selection = RoleSelection(
            role=LoopRole.ARCHITECT, reason="first", focus="design"
        )
        prompt = engine._build_role_prompt(state, selection)

        assert "test goal" in prompt
        assert "架构师" in prompt
        assert "已完成的工作" not in prompt  # no context
        assert "用户引导" not in prompt  # no guidance

    def test_build_iteration_state_with_mixed_failures(self):
        """_build_iteration_state should correctly compute consecutive_failures."""
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        req = LoopRequirement(goal="test", acceptance_criteria=["c1"], raw_text="test")
        engine._project.set_requirement(req)
        engine._project.iterations = [
            IterationRecord(
                iteration=1, role=LoopRole.ARCHITECT, status=IterationStatus.SUCCESS
            ),
            IterationRecord(
                iteration=2, role=LoopRole.DEVELOPER, status=IterationStatus.FAILED
            ),
            IterationRecord(
                iteration=3, role=LoopRole.DEVELOPER, status=IterationStatus.FAILED
            ),
        ]
        state = engine._build_iteration_state(4, req)
        assert state.consecutive_failures == 2
        assert state.last_role == LoopRole.DEVELOPER

    def test_save_state_and_reload(self):
        """save_state should produce a valid JSON file."""
        import tempfile
        import json

        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp/test")
        engine._project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name

        try:
            engine.save_state(filepath)
            with open(filepath, "r") as f:
                data = json.load(f)
            assert data["chat_id"] == "c1"
            assert data["project"]["name"] == "test"
        finally:
            import os

            os.unlink(filepath)


# ===========================================================================
# LoopEngineManager Edge Cases
# ===========================================================================


class TestLoopEngineManagerEdgeCases:
    """Edge cases for LoopEngineManager."""

    @patch("src.loop_engine.engine.get_settings")
    def _make_manager(self, mock_settings):
        mock_settings.return_value = _mock_settings()
        return LoopEngineManager()

    def test_get_or_create_different_paths(self):
        """Different root_paths should create different engines."""
        mgr = self._make_manager()
        e1 = mgr.get_or_create("c1", "/tmp/a")
        e2 = mgr.get_or_create("c1", "/tmp/b")
        assert e1 is not e2

    def test_get_or_create_different_chats(self):
        """Different chat_ids should create different engines."""
        mgr = self._make_manager()
        e1 = mgr.get_or_create("c1", "/tmp/test")
        e2 = mgr.get_or_create("c2", "/tmp/test")
        assert e1 is not e2

    def test_engine_name_switch_while_running_keeps_existing(self):
        """Switching engine name while running should keep existing engine."""
        mgr = self._make_manager()
        e1 = mgr.get_or_create("c1", "/tmp/test", engine_name="Coco")
        e1._is_running = True
        e2 = mgr.get_or_create("c1", "/tmp/test", engine_name="Claude")
        # Running engine should not be replaced
        assert e2 is e1
        assert e2.engine_name == "Coco"

    def test_list_engines_empty(self):
        """list_engines should return empty list for unknown chat."""
        mgr = self._make_manager()
        assert mgr.list_engines("nonexistent") == []

    def test_get_active_engines_none_running(self):
        """get_active_engines should return empty when none running."""
        mgr = self._make_manager()
        mgr.get_or_create("c1", "/tmp/a")
        mgr.get_or_create("c1", "/tmp/b")
        assert len(mgr.get_active_engines("c1")) == 0

    def test_cleanup_all_clears_everything(self):
        """cleanup_all should clear all engines."""
        mgr = self._make_manager()
        mgr.get_or_create("c1", "/tmp/a")
        mgr.get_or_create("c2", "/tmp/b")
        mgr.cleanup_all()
        assert mgr.list_engines() == []
