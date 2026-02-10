"""Tests for loop_engine — subprocess-driven LoopEngine."""

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
from src.loop_engine.reporter import LoopReporter


class TestLoopEngine:
    @patch("src.loop_engine.engine.get_settings")
    def _make_engine(self, mock_settings, **kwargs):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        mock_settings.return_value = s
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

    def test_inject_guidance(self):
        engine = self._make_engine()
        engine.inject_guidance("focus on login")
        assert engine._user_guidance == "focus on login"

    def test_parse_requirement_with_criteria(self):
        engine = self._make_engine()
        text = """实现登录功能
- 支持邮箱登录
- 支持手机号登录
- 有错误提示
"""
        req = engine._parse_requirement(text)
        assert len(req.acceptance_criteria) == 3
        assert "支持邮箱登录" in req.acceptance_criteria

    def test_parse_requirement_checkbox_format(self):
        engine = self._make_engine()
        text = """功能需求
[ ] 第一项
[x] 第二项
"""
        req = engine._parse_requirement(text)
        assert len(req.acceptance_criteria) == 2

    def test_parse_requirement_no_criteria(self):
        engine = self._make_engine()
        text = "实现登录功能"
        req = engine._parse_requirement(text)
        assert len(req.acceptance_criteria) == 1
        assert "完成需求" in req.acceptance_criteria[0]

    def test_build_initial_prompt(self):
        engine = self._make_engine()
        req = LoopRequirement(
            goal="add login", acceptance_criteria=["email login", "phone login"], raw_text="test",
        )
        prompt = engine._build_initial_prompt(req)
        assert "add login" in prompt
        assert "email login" in prompt
        assert "/tmp/test" in prompt

    def test_build_iteration_prompt(self):
        engine = self._make_engine()
        req = LoopRequirement(
            goal="add login", acceptance_criteria=["email login"], raw_text="test",
        )
        prompt = engine._build_iteration_prompt(2, req)
        assert "第 2 轮" in prompt
        assert "email login" in prompt

    def test_build_iteration_prompt_with_guidance(self):
        engine = self._make_engine()
        engine._user_guidance = "prioritize email"
        req = LoopRequirement(
            goal="login", acceptance_criteria=["c1"], raw_text="test",
        )
        prompt = engine._build_iteration_prompt(3, req)
        assert "prioritize email" in prompt
        assert engine._user_guidance is None  # consumed

    def test_detect_convergence_not_enough_iterations(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        assert not engine._detect_convergence()

    def test_detect_convergence_short_output(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        for i in range(3):
            engine._project.iterations.append(
                IterationRecord(iteration=i+1, output="ok")
            )
        assert engine._detect_convergence()

    def test_detect_convergence_long_output_no_convergence(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        for i in range(3):
            engine._project.iterations.append(
                IterationRecord(iteration=i+1, output="x" * 100)
            )
        assert not engine._detect_convergence()

    def test_save_state_no_project(self):
        engine = self._make_engine()
        with pytest.raises(ValueError):
            engine.save_state()

    def test_resume_returns_project(self):
        engine = self._make_engine()
        engine._project = MagicMock()
        result = engine.resume()
        assert result is engine._project


class TestLoopEngineManager:
    @patch("src.loop_engine.engine.get_settings")
    def _make_manager(self, mock_settings):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        mock_settings.return_value = s
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
            goal="login", acceptance_criteria=["email", "phone"], raw_text="test",
        )
        project.set_requirement(req)
        assert project.total_criteria == 2
        assert not project.is_all_satisfied

    def test_loop_project_serialization(self):
        project = LoopProject.create(name="test", root_path="/tmp/test")
        req = LoopRequirement(
            goal="login", acceptance_criteria=["email"], raw_text="test",
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
            goal="test", acceptance_criteria=["c1", "c2"],
            constraints=["fast"], raw_text="raw",
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
        project.iterations.append(IterationRecord(iteration=1, status=IterationStatus.SUCCESS))
        project.iterations.append(IterationRecord(iteration=2, status=IterationStatus.FAILED))
        project.iterations.append(IterationRecord(iteration=3, status=IterationStatus.FAILED))
        assert project.success_count == 1
        assert project.failure_count == 2
        assert project.consecutive_failures == 2
        assert project.current_iteration == 3

    def test_loop_project_auto_name(self):
        project = LoopProject.create(root_path="/tmp/myproject")
        assert project.name == "myproject"


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
            iteration=1, role=LoopRole.DEVELOPER,
            status=IterationStatus.SUCCESS, summary="did stuff",
            output="some output",
        )
        ctx.record_iteration(record)
        result = ctx.build_context_prompt()
        assert "迭代历史" in result

    def test_get_iteration_summaries(self):
        ctx = LoopContextManager()
        ctx.record_iteration(IterationRecord(iteration=1, role=LoopRole.DEVELOPER, summary="step1"))
        ctx.record_iteration(IterationRecord(iteration=2, role=LoopRole.TESTER, summary=""))
        summaries = ctx.get_iteration_summaries()
        assert summaries == ["step1"]


class TestLoopReporter:
    def _make_project(self) -> LoopProject:
        project = LoopProject.create(name="test_project", root_path="/tmp/test")
        project.set_requirement(LoopRequirement(
            goal="implement login",
            acceptance_criteria=["email login", "phone login"],
            raw_text="test",
        ))
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
        record = IterationRecord(iteration=1, focus="Implement email", status=IterationStatus.SUCCESS)
        result = reporter.format_iteration_done(1, record)
        assert "完成" in result
        assert "Implement email" in result

    def test_format_iteration_done_failed(self):
        reporter = LoopReporter()
        record = IterationRecord(iteration=1, status=IterationStatus.FAILED, error="timeout")
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
