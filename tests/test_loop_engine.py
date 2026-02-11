"""Tests for loop_engine — ACP-driven LoopEngine."""

import pytest
from unittest.mock import patch, MagicMock

from src.deep_engine.models import EngineRunState
from src.loop_engine.engine import LoopEngine, LoopEngineManager, LoopEngineCallbacks
from src.loop_engine.models import (
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    IterationRecord,
    IterationStatus,
)
from src.loop_engine.tracker import IterationTracker
from src.loop_engine.reporter import LoopReporter
from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo, PlanInfo, PlanEntryInfo


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
        assert engine.run_state == EngineRunState.IDLE
        assert engine.project is None
        assert not engine.is_running

    def test_stop(self):
        engine = self._make_engine()
        engine._run_state = EngineRunState.RUNNING
        engine._session = MagicMock()
        engine.stop()
        assert engine.run_state == EngineRunState.STOPPING
        engine._session.cancel.assert_called_once()

    def test_pause(self):
        engine = self._make_engine()
        engine._project = MagicMock()
        engine._session = MagicMock()
        engine._run_state = EngineRunState.RUNNING
        engine.pause()
        assert engine.run_state == EngineRunState.STOPPING

    def test_cleanup(self):
        engine = self._make_engine()
        engine._session = MagicMock()
        engine._project = MagicMock()
        engine.cleanup()
        assert engine._session is None
        assert engine._project is None
        assert engine.run_state == EngineRunState.IDLE

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

    def test_get_rendered_content(self):
        engine = self._make_engine()
        assert isinstance(engine.get_rendered_content(), str)


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
        engine._run_state = EngineRunState.RUNNING
        assert mgr.get_active_engine("c1") is engine

    def test_get_active_engines(self):
        mgr = self._make_manager()
        e1 = mgr.get_or_create("c1", "/tmp/a")
        e2 = mgr.get_or_create("c1", "/tmp/b")
        assert len(mgr.get_active_engines("c1")) == 0
        e1._run_state = EngineRunState.RUNNING
        e2._run_state = EngineRunState.RUNNING
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


class TestIterationTracker:
    def test_initial_state(self):
        tracker = IterationTracker()
        assert tracker.tool_calls == []
        assert tracker.modified_files == set()
        assert tracker.plan_progress is None
        assert tracker.text_buffer == ""

    def test_process_text_chunk(self):
        tracker = IterationTracker()
        event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello ")
        tracker.process(event)
        assert tracker.text_buffer == "hello "

    def test_process_tool_call_start(self):
        tracker = IterationTracker()
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="in_progress",
                          locations=["/a.py"])
        tracker.process(ACPEvent(event_type=ACPEventType.TOOL_CALL_START, tool_call=tc))
        assert "/a.py" in tracker.modified_files

    def test_process_tool_call_done(self):
        tracker = IterationTracker()
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="completed",
                          locations=["/a.py"])
        tracker.process(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        assert len(tracker.tool_calls) == 1
        assert "/a.py" in tracker.modified_files

    def test_process_plan_update(self):
        tracker = IterationTracker()
        plan = PlanInfo(entries=[PlanEntryInfo(content="step1")])
        tracker.process(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan))
        assert tracker.plan_progress is not None

    def test_reset(self):
        tracker = IterationTracker()
        tracker.text_buffer = "hello"
        tracker.modified_files.add("/a.py")
        tracker.reset()
        assert tracker.text_buffer == ""
        assert tracker.modified_files == set()


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
