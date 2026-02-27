"""Tests for spec_engine — ACP-driven SpecEngine with structured methodology."""

import json
import os
import re
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

from src.deep_engine.models import EngineRunState
from src.spec_engine.engine import SpecEngine, SpecEngineManager, SpecEngineCallbacks
from src.spec_engine.models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    SpecCycle,
    SpecTask,
    SpecTaskStatus,
    SpecArtifact,
    PlanArtifact,
)
from src.spec_engine.tracker import PhaseTracker
from src.spec_engine.reporter import SpecReporter
from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo, PlanInfo, PlanEntryInfo
from src.loop_engine.models import (
    CriteriaTracker,
    ReviewPerspective,
    PerspectiveReview,
    ReviewResult,
)


# ======================================================================
# TestSpecModels — enums, creation, serialization, lifecycle
# ======================================================================

class TestSpecModels:
    def test_spec_phase_values(self):
        assert SpecPhase.SPEC.value == "spec"
        assert SpecPhase.PLAN.value == "plan"
        assert SpecPhase.TASK.value == "task"
        assert SpecPhase.BUILD.value == "build"
        assert SpecPhase.REVIEW.value == "review"

    def test_spec_phase_display_name(self):
        assert SpecPhase.SPEC.display_name == "规格定义"
        assert SpecPhase.BUILD.display_name == "执行构建"
        assert SpecPhase.REVIEW.display_name == "多视角审查"

    def test_spec_phase_emoji(self):
        assert SpecPhase.SPEC.emoji == "📋"
        assert SpecPhase.BUILD.emoji == "🔨"
        assert SpecPhase.REVIEW.emoji == "🔍"

    def test_project_status_enum(self):
        assert SpecProjectStatus.IDLE.value == "idle"
        assert SpecProjectStatus.RUNNING.value == "running"
        assert SpecProjectStatus.CLARIFYING.value == "clarifying"
        assert SpecProjectStatus.COMPLETED.value == "completed"
        assert SpecProjectStatus.ABORTED.value == "aborted"

    def test_task_status_enum(self):
        assert SpecTaskStatus.PENDING.value == "pending"
        assert SpecTaskStatus.COMPLETED.value == "completed"

    def test_spec_task_creation(self):
        task = SpecTask(task_id=1, description="Implement auth")
        assert task.task_id == 1
        assert task.description == "Implement auth"
        assert task.dependencies == []
        assert task.status == SpecTaskStatus.PENDING
        assert task.output == ""

    def test_spec_task_to_dict_from_dict(self):
        task = SpecTask(task_id=2, description="Add tests", dependencies=[1], status=SpecTaskStatus.COMPLETED, output="ok")
        d = task.to_dict()
        assert d["task_id"] == 2
        assert d["dependencies"] == [1]
        assert d["status"] == "completed"

        restored = SpecTask.from_dict(d)
        assert restored.task_id == 2
        assert restored.dependencies == [1]
        assert restored.status == SpecTaskStatus.COMPLETED
        assert restored.output == "ok"

    def test_spec_cycle_creation_and_lifecycle(self):
        cycle = SpecCycle(cycle_number=1)
        assert cycle.cycle_number == 1
        assert cycle.phase == SpecPhase.SPEC
        assert cycle.status == "running"
        assert cycle.completed_at is None

        cycle.complete()
        assert cycle.status == "completed"
        assert cycle.completed_at is not None
        assert cycle.duration > 0

    def test_spec_cycle_fail(self):
        cycle = SpecCycle(cycle_number=2)
        cycle.fail()
        assert cycle.status == "failed"
        assert cycle.completed_at is not None

    def test_spec_cycle_serialization(self):
        cycle = SpecCycle(cycle_number=1)
        cycle.spec_content = "spec output"
        cycle.spec_artifact = SpecArtifact(acceptance_criteria=["C1"])
        cycle.plan_content = "plan output"
        cycle.plan_artifact = PlanArtifact(file_changes=["a.py"])
        cycle.tasks = [SpecTask(task_id=1, description="T1")]
        cycle.build_output = "build done"
        cycle.complete()

        d = cycle.to_dict()
        assert d["cycle_number"] == 1
        assert d["spec_content"] == "spec output"
        assert d["spec_artifact"]["acceptance_criteria"] == ["C1"]
        assert d["plan_artifact"]["file_changes"] == ["a.py"]
        assert len(d["tasks"]) == 1
        assert d["status"] == "completed"

        restored = SpecCycle.from_dict(d)
        assert restored.cycle_number == 1
        assert restored.spec_content == "spec output"
        assert restored.spec_artifact is not None
        assert restored.spec_artifact.acceptance_criteria == ["C1"]
        assert restored.plan_artifact is not None
        assert restored.plan_artifact.file_changes == ["a.py"]
        assert len(restored.tasks) == 1
        assert restored.status == "completed"

    def test_spec_project_create(self):
        project = SpecProject.create(name="test_project", root_path="/tmp/test")
        assert project.name == "test_project"
        assert project.root_path == "/tmp/test"
        assert len(project.project_id) == 8
        assert project.status == SpecProjectStatus.IDLE

    def test_spec_project_create_default_name(self):
        project = SpecProject.create(root_path="/tmp/myapp")
        assert project.name == "myapp"

    def test_spec_project_lifecycle(self):
        project = SpecProject.create(root_path="/tmp")
        assert project.status == SpecProjectStatus.IDLE

        project.start()
        assert project.status == SpecProjectStatus.RUNNING
        assert project.started_at is not None

        project.pause()
        assert project.status == SpecProjectStatus.PAUSED

        project.resume()
        assert project.status == SpecProjectStatus.RUNNING

        project.complete()
        assert project.status == SpecProjectStatus.COMPLETED
        assert project.completed_at is not None

    def test_spec_project_abort(self):
        project = SpecProject.create(root_path="/tmp")
        project.start()
        project.abort("timeout")
        assert project.status == SpecProjectStatus.ABORTED
        assert project.error == "timeout"

    def test_spec_project_properties(self):
        project = SpecProject.create(root_path="/tmp")
        assert project.current_cycle is None
        assert project.current_cycle_number == 0
        assert project.satisfied_count == 0
        assert project.total_criteria == 0
        # CriteriaTracker requires total_count > 0 to be "all satisfied"
        assert not project.is_all_satisfied

        project.cycles.append(SpecCycle(cycle_number=1))
        assert project.current_cycle is not None
        assert project.current_cycle.cycle_number == 1
        assert project.current_cycle_number == 1

    def test_spec_project_duration(self):
        project = SpecProject.create(root_path="/tmp")
        assert project.duration() is None

        project.start()
        assert project.duration() is not None
        assert project.duration() >= 0

    def test_spec_project_serialization(self):
        project = SpecProject.create(name="test", root_path="/tmp/test")
        project.requirement = "Build a thing"
        project.acceptance_criteria = ["Criterion 1", "Criterion 2"]
        project.criteria_tracker.init_criteria(["Criterion 1", "Criterion 2"])
        project.criteria_tracker.batch_update({0: True}, 1)
        project.start()
        project.cycles.append(SpecCycle(cycle_number=1))

        d = project.to_dict()
        assert d["name"] == "test"
        assert d["requirement"] == "Build a thing"
        assert len(d["acceptance_criteria"]) == 2
        assert len(d["cycles"]) == 1

        restored = SpecProject.from_dict(d)
        assert restored.name == "test"
        assert restored.requirement == "Build a thing"
        assert len(restored.acceptance_criteria) == 2
        assert len(restored.cycles) == 1
        assert restored.criteria_tracker.satisfied_count == 1


# ======================================================================
# TestPhaseTracker — event processing
# ======================================================================

class TestPhaseTracker:
    def test_text_chunk(self):
        tracker = PhaseTracker()
        event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello ")
        tracker.process(event)
        event2 = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="world")
        tracker.process(event2)
        assert tracker.text_buffer == "hello world"

    def test_tool_call_start(self):
        tracker = PhaseTracker()
        tool = ToolCallInfo(id="t1", title="Write file", kind="edit", status="started", locations=["file.py"])
        event = ACPEvent(event_type=ACPEventType.TOOL_CALL_START, tool_call=tool)
        tracker.process(event)
        assert "file.py" in tracker.modified_files

    def test_tool_call_done(self):
        tracker = PhaseTracker()
        tool = ToolCallInfo(id="t2", title="Write file", kind="edit", status="done", locations=["a.py", "b.py"])
        event = ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tool)
        tracker.process(event)
        assert len(tracker.tool_calls) == 1
        assert "a.py" in tracker.modified_files
        assert "b.py" in tracker.modified_files

    def test_plan_update(self):
        tracker = PhaseTracker()
        plan = PlanInfo(entries=[])
        event = ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan)
        tracker.process(event)
        assert tracker.plan_progress is not None

    def test_reset(self):
        tracker = PhaseTracker()
        tracker.process(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hi"))
        tool = ToolCallInfo(id="t3", title="Read file", kind="read", status="done", locations=["f.py"])
        tracker.process(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tool))

        assert tracker.text_buffer != ""
        assert len(tracker.tool_calls) > 0

        tracker.reset()
        assert tracker.text_buffer == ""
        assert len(tracker.tool_calls) == 0
        assert len(tracker.modified_files) == 0
        assert tracker.plan_progress is None

    def test_empty_text_ignored(self):
        tracker = PhaseTracker()
        tracker.process(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=""))
        tracker.process(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=None))
        assert tracker.text_buffer == ""


# ======================================================================
# TestSpecReporter — content formatters and title helpers
# ======================================================================

class TestSpecReporter:
    def _make_project(self, **kwargs):
        project = SpecProject.create(name="test_proj", root_path="/tmp")
        project.requirement = kwargs.get("requirement", "Build auth system")
        if "criteria" in kwargs:
            project.acceptance_criteria = kwargs["criteria"]
            project.criteria_tracker.init_criteria(kwargs["criteria"])
        return project

    def test_format_analyzing_start(self):
        r = SpecReporter()
        result = r.format_analyzing_start("Build login")
        assert "Spec Agent 启动" in result
        assert "Build login" in result

    def test_format_analyzing_done(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1", "C2"])
        result = r.format_analyzing_done(project)
        assert "需求分析完成" in result
        assert "2 条" in result
        assert "C1" in result
        assert "C2" in result

    def test_format_analyzing_done_no_criteria(self):
        r = SpecReporter()
        project = self._make_project()
        result = r.format_analyzing_done(project)
        assert "需求分析失败" in result

    def test_format_cycle_start(self):
        r = SpecReporter()
        result = r.format_cycle_start(1, 10)
        assert "[1/10]" in result
        assert "Spec" in result

    def test_format_phase_start(self):
        r = SpecReporter()
        result = r.format_phase_start(2, SpecPhase.BUILD)
        assert "🔨" in result
        assert "执行构建" in result
        assert "循环 2" in result

    def test_format_phase_done(self):
        r = SpecReporter()
        result = r.format_phase_done(1, SpecPhase.SPEC, "spec content output")
        assert "规格定义完成" in result
        assert "spec content output" in result

    def test_format_phase_done_truncation(self):
        r = SpecReporter()
        long_content = "x" * 600
        result = r.format_phase_done(1, SpecPhase.PLAN, long_content)
        assert "..." in result

    def test_format_review_result_all_passed(self):
        r = SpecReporter()
        review = ReviewResult(reviews=[
            PerspectiveReview(perspective=p, passed=True, suggestions=[], summary="通过")
            for p in ReviewPerspective
        ], iteration=1)
        result = r.format_review_result(review, 1)
        assert "PASS" in result
        assert "无改进建议" in result

    def test_format_review_result_with_suggestions(self):
        r = SpecReporter()
        review = ReviewResult(reviews=[
            PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=False,
                              suggestions=["Fix security issue"], summary="1条建议"),
            PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=True,
                              suggestions=[], summary="通过"),
            PerspectiveReview(perspective=ReviewPerspective.USER, passed=True,
                              suggestions=[], summary="通过"),
            PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=True,
                              suggestions=[], summary="通过"),
        ], iteration=2)
        result = r.format_review_result(review, 2)
        assert "Fix security issue" in result
        assert "改进建议: 1 条" in result

    def test_format_criteria_brief(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1", "C2"])
        project.criteria_tracker.batch_update({0: True}, 1)
        result = r.format_criteria_brief(project)
        assert "✅" in result
        assert "🔲" in result
        assert "1/2" in result

    def test_format_criteria_brief_empty(self):
        r = SpecReporter()
        project = self._make_project()
        result = r.format_criteria_brief(project)
        assert result == ""

    def test_format_criteria_update(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1", "C2", "C3"])
        project.criteria_tracker.batch_update({0: True, 2: True}, 1)
        result = r.format_criteria_update(project)
        assert "(循环1)" in result
        assert "C1" in result

    def test_format_project_done_completed(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1"])
        project.criteria_tracker.init_criteria(["C1"])
        project.criteria_tracker.batch_update({0: True}, 1)
        project.start()
        project.cycles.append(SpecCycle(cycle_number=1))
        project.complete()
        result = r.format_project_done(project)
        assert "Spec 模式完成" in result
        assert "1 轮" in result

    def test_format_project_done_aborted(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1"])
        project.start()
        project.abort("timeout")
        result = r.format_project_done(project)
        assert "Spec 模式终止" in result
        assert "timeout" in result

    def test_format_project_done_paused(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1"])
        project.start()
        project.pause()
        result = r.format_project_done(project)
        assert "Spec 模式暂停" in result

    def test_format_error(self):
        r = SpecReporter()
        result = r.format_error("connection refused")
        assert "Spec Agent 错误" in result
        assert "connection refused" in result

    def test_format_status(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1", "C2"])
        project.start()
        cycle = SpecCycle(cycle_number=1, phase=SpecPhase.BUILD)
        project.cycles.append(cycle)
        result = r.format_status(project)
        assert "Spec 状态" in result
        assert "循环执行中" in result
        assert "1 轮" in result
        assert "🔨" in result

    def test_format_guidance_injected(self):
        r = SpecReporter()
        result = r.format_guidance_injected("focus on tests")
        assert "引导信息已注入" in result
        assert "focus on tests" in result

    # Title helpers
    def test_title_helpers(self):
        r = SpecReporter()
        assert "启动" in r.get_analyzing_start_title()
        assert "完成" in r.get_analyzing_done_title()
        assert "[2/10]" in r.get_cycle_start_title(2, 10)
        assert "规格定义" in r.get_phase_title(1, SpecPhase.SPEC)
        assert "审查通过" in r.get_review_title(1, all_passed=True)
        assert "多视角审查" in r.get_review_title(1, all_passed=False)
        assert "引导" in r.get_guidance_injected_title()
        assert "错误" in r.get_error_title()
        assert "状态" in r.get_status_title()

    def test_get_project_done_title(self):
        r = SpecReporter()
        project = self._make_project()
        project.status = SpecProjectStatus.COMPLETED
        assert "完成" in r.get_project_done_title(project)

        project.status = SpecProjectStatus.ABORTED
        assert "终止" in r.get_project_done_title(project)

        project.status = SpecProjectStatus.PAUSED
        assert "暂停" in r.get_project_done_title(project)

    def test_get_progress_info(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1", "C2"])
        project.start()
        project.cycles.append(SpecCycle(cycle_number=1))
        info = r.get_progress_info(project)
        assert info["satisfied_count"] == 0
        assert info["total_criteria"] == 2
        assert info["cycle_count"] == 1
        assert info["is_running"]
        assert not info["is_paused"]
        assert "project_name" in info
        assert "progress_bar" in info

    def test_format_history_uses_history_log_path(self):
        r = SpecReporter()
        project = self._make_project(criteria=["C1"])
        project.project_id = "pid"
        project.root_path = "/tmp/x"
        project.cycle_count_total = 10
        # Keep only 1 cycle in memory to simulate compact state
        project.cycles = [SpecCycle(cycle_number=10)]
        project.history_log_path = "/tmp/x/.spec_engine/pid/custom_history.jsonl"
        out = r.format_history(project, tail=5)
        assert "custom_history.jsonl" in out


# ======================================================================
# TestSpecEngine — core engine behavior
# ======================================================================

class TestSpecEngine:
    @patch("src.spec_engine.engine.get_settings")
    def _make_engine(self, mock_settings, **kwargs):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_max_cycles_limit = 5000
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        s.spec_cycle_tasks_max = 50
        s.spec_cycle_output_max_chars = 4000
        s.spec_state_filename = ".spec_engine_state.json"
        s.spec_artifacts_dirname = ".spec_engine"
        s.spec_persist_phase_artifacts = True
        s.spec_persist_every_phase = True
        # Keep legacy unit tests stable: discovery is tested separately.
        s.spec_discovery_enabled = False
        s.spec_discovery_max_questions = 3
        s.spec_discovery_force_nonempty = True
        s.spec_generated_specs_per_cycle = 1
        s.spec_allow_resume_from_disk = True
        s.ark_api_key = ""
        s.ark_model = ""
        mock_settings.return_value = s
        return SpecEngine(chat_id="c1", root_path="/tmp/test", **kwargs)

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
        assert engine._user_guidance == ["focus on login"]
        engine.inject_guidance("also fix logout")
        assert engine._user_guidance == ["focus on login", "also fix logout"]

    def test_consume_guidance(self):
        engine = self._make_engine()
        engine.inject_guidance("msg1")
        engine.inject_guidance("msg2")
        result = engine._consume_guidance()
        assert "msg1" in result
        assert "msg2" in result
        # After consume, guidance is empty
        assert engine._user_guidance == []
        assert engine._consume_guidance() == ""

    def test_parse_acceptance_criteria_with_list_markers(self):
        engine = self._make_engine()
        text = """实现登录功能
- 支持邮箱登录
- 支持手机号登录
- 有错误提示
"""
        criteria = engine._parse_acceptance_criteria(text)
        assert len(criteria) == 3
        assert "支持邮箱登录" in criteria

    def test_parse_acceptance_criteria_with_checkboxes(self):
        engine = self._make_engine()
        text = """功能需求
[ ] 第一项
[x] 第二项
"""
        criteria = engine._parse_acceptance_criteria(text)
        assert len(criteria) == 2

    def test_parse_acceptance_criteria_no_markers_fallback(self):
        engine = self._make_engine()
        text = "实现一个简单的登录页面"
        criteria = engine._parse_acceptance_criteria(text)
        assert len(criteria) == 1
        assert "完成需求:" in criteria[0]

    @patch("src.spec_engine.engine.ChatOpenAI")
    def test_parse_acceptance_criteria_llm_decompose(self, mock_chat):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="- 实现登录接口\n- 支持错误提示\n- 添加单元测试")
        mock_chat.return_value = mock_llm

        engine = self._make_engine()
        engine.settings.ark_api_key = "test-key"
        engine.settings.ark_model = "test-model"

        criteria = engine._parse_acceptance_criteria("实现登录功能")
        assert len(criteria) == 3

    def test_parse_tasks(self):
        text = """1. 创建数据模型 (依赖: 无)
2. 实现 API 接口 (依赖: 1)
3. 编写前端页面 (依赖: 1, 2)
4. 添加测试 (依赖: 2, 3)
"""
        tasks = SpecEngine._parse_tasks(text)
        assert len(tasks) == 4
        assert tasks[0].task_id == 1
        assert tasks[0].description == "创建数据模型"
        assert tasks[0].dependencies == []
        assert tasks[1].dependencies == [1]
        assert tasks[2].dependencies == [1, 2]
        assert tasks[3].dependencies == [2, 3]

    def test_parse_tasks_various_formats(self):
        text = """1. First task
2、Second task (depends: 1)
3) Third task (依赖: 1, 2)
"""
        tasks = SpecEngine._parse_tasks(text)
        assert len(tasks) == 3

    def test_parse_tasks_empty(self):
        tasks = SpecEngine._parse_tasks("no tasks here")
        assert tasks == []

    def test_extract_criteria_from_llm_response(self):
        text = """以下是验收标准：
- 实现登录接口
- 支持邮箱和密码登录
* 显示错误提示信息
1. 添加单元测试
2、集成测试通过
"""
        criteria = SpecEngine._extract_criteria_from_llm_response(text)
        assert len(criteria) == 5

    def test_extract_reviews_from_llm_response(self):
        text = """```json
[
  {"perspective": "ARCHITECT", "verdict": "PASS", "suggestions": []},
  {"perspective": "PRODUCT", "verdict": "FAIL", "suggestions": ["Need better UX"]},
  {"perspective": "USER", "verdict": "PASS", "suggestions": []},
  {"perspective": "TESTER", "verdict": "FAIL", "suggestions": ["Add unit tests", "Add integration tests"]}
]
```"""
        reviews = SpecEngine._extract_reviews_from_llm_response(text)
        assert len(reviews) == 4
        assert reviews[0].passed is True
        assert reviews[0].suggestions == []
        assert reviews[1].passed is False
        assert reviews[1].suggestions == ["Need better UX"]
        assert len(reviews[3].suggestions) == 2

    def test_extract_reviews_from_llm_invalid_json(self):
        reviews = SpecEngine._extract_reviews_from_llm_response("not json at all")
        assert reviews == []

    def test_build_spec_prompt(self):
        engine = self._make_engine()
        prompt = engine._build_spec_prompt("Build a login system")
        assert "Build a login system" in prompt
        assert "/tmp/test" in prompt
        assert "```json" in prompt
        assert "\"acceptance_criteria\"" in prompt
        assert "clarification_questions" in prompt

    def test_build_plan_prompt(self):
        engine = self._make_engine()
        prompt = engine._build_plan_prompt("spec output here")
        assert "spec output here" in prompt
        assert "```json" in prompt
        assert "file_changes" in prompt

    def test_build_task_prompt(self):
        engine = self._make_engine()
        prompt = engine._build_task_prompt("plan output here")
        assert "plan output here" in prompt
        assert "任务编号" in prompt

    def test_build_build_prompt(self):
        engine = self._make_engine()
        tasks = [
            SpecTask(task_id=1, description="Create models"),
            SpecTask(task_id=2, description="Add tests"),
        ]
        prompt = engine._build_build_prompt(tasks, "plan content")
        assert "Create models" in prompt
        assert "Add tests" in prompt
        assert "plan content" in prompt

    def test_build_review_prompt(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        engine._project.requirement = "Build auth"
        prompt = engine._build_review_prompt()
        assert "ARCHITECT" in prompt
        assert "PRODUCT" in prompt
        assert "USER" in prompt
        assert "TESTER" in prompt
        assert "Build auth" in prompt

    def test_build_refinement_input(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        engine._project.requirement = "Build auth"
        engine._project.acceptance_criteria = ["Criterion A"]
        engine._project.criteria_tracker.init_criteria(["Criterion A"])

        engine._last_review = ReviewResult(reviews=[
            PerspectiveReview(
                perspective=ReviewPerspective.ARCHITECT, passed=False,
                suggestions=["Fix security"], summary="1条建议",
            ),
            PerspectiveReview(
                perspective=ReviewPerspective.PRODUCT, passed=True,
                suggestions=[], summary="通过",
            ),
            PerspectiveReview(
                perspective=ReviewPerspective.USER, passed=True,
                suggestions=[], summary="通过",
            ),
            PerspectiveReview(
                perspective=ReviewPerspective.TESTER, passed=True,
                suggestions=[], summary="通过",
            ),
        ], iteration=1)

        result = engine._build_refinement_input("Build auth")
        assert "Build auth" in result
        assert "Fix security" in result
        assert "Criterion A" in result

    def test_detect_convergence_not_enough_cycles(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        assert not engine._detect_convergence()

    def test_detect_convergence_triggered(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        engine._project.criteria_tracker.init_criteria(["C1", "C2"])

        # 2 cycles, criteria satisfied count stays the same (0), review suggestions stay the same (1)
        def _make_review(iteration):
            return ReviewResult(reviews=[
                PerspectiveReview(
                    perspective=ReviewPerspective.ARCHITECT, passed=False,
                    suggestions=["S1"], summary="1条建议"
                ),
                PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=True,
                                  suggestions=[], summary="通过"),
                PerspectiveReview(perspective=ReviewPerspective.USER, passed=True,
                                  suggestions=[], summary="通过"),
                PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=True,
                                  suggestions=[], summary="通过"),
            ], iteration=iteration)

        engine._project.cycles = [
            SpecCycle(cycle_number=1, build_output="x" * 100, review_result=_make_review(1)),
            SpecCycle(cycle_number=2, build_output="y" * 100, review_result=_make_review(2)),
        ]
        assert engine._detect_convergence()

    def test_detect_convergence_not_triggered(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        engine._project.criteria_tracker.init_criteria(["C1", "C2"])

        # Simulate criteria progress in the window
        engine._project.criteria_tracker.update(0, True, 1)
        engine._project.criteria_tracker.update(1, True, 2)

        review_pass = ReviewResult(reviews=[
            PerspectiveReview(perspective=p, passed=True, suggestions=[], summary="通过")
            for p in ReviewPerspective
        ], iteration=1)

        engine._project.cycles = [
            SpecCycle(cycle_number=1, build_output="x" * 100, review_result=review_pass),
            SpecCycle(cycle_number=2, build_output="y" * 100, review_result=review_pass),
        ]
        assert not engine._detect_convergence()

    def test_format_criteria_status_empty(self):
        engine = self._make_engine()
        assert engine._format_criteria_status() == ""

    def test_format_criteria_status_with_project(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        engine._project.criteria_tracker.init_criteria(["C1", "C2"])
        engine._project.criteria_tracker.batch_update({0: True}, 1)
        result = engine._format_criteria_status()
        assert "[x]" in result
        assert "[ ]" in result

    def test_save_state(self, tmp_path):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path=str(tmp_path))
        engine._project.requirement = "test req"

        filepath = engine.save_state(str(tmp_path / "state.json"))
        assert os.path.exists(filepath)

        with open(filepath) as f:
            data = json.load(f)
        assert data["chat_id"] == "c1"
        assert data["project"]["requirement"] == "test req"

    def test_save_state_compaction_fields(self, tmp_path):
        """State file should include compaction hints (cycles_truncated_before, history_log_path)."""
        with patch("src.spec_engine.engine.get_settings") as mock_settings:
            s = MagicMock()
            s.spec_max_cycles = 10
            s.spec_max_cycles_limit = 5000
            s.spec_execution_timeout = 300
            s.spec_convergence_window = 2
            s.spec_review_enabled = False
            s.spec_cycle_tasks_max = 5
            s.spec_cycle_output_max_chars = 200
            s.spec_state_filename = ".spec_engine_state.json"
            s.spec_artifacts_dirname = ".spec_engine"
            s.spec_history_log_filename = "history.jsonl"
            s.spec_persist_every_phase = True
            s.spec_persist_phase_artifacts = False
            s.spec_allow_resume_from_disk = True
            s.spec_state_cycles_tail = 3
            s.spec_state_work_items_tail = 10
            s.spec_state_metrics_tail = 10
            s.spec_generated_specs_retention = 10
            s.spec_infinite_mode = False
            s.spec_disable_convergence = False
            s.spec_disable_early_stop = False
            s.ark_api_key = ""
            s.ark_model = ""
            mock_settings.return_value = s

            engine = SpecEngine(chat_id="c1", root_path=str(tmp_path))
            engine._project = SpecProject.create(root_path=str(tmp_path))
            engine._project.requirement = "req"
            # Create 10 cycles (state only keeps tail=3)
            for i in range(1, 11):
                c = SpecCycle(cycle_number=i)
                c.complete()
                engine._project.cycles.append(c)
            engine._project.cycle_count_total = 10

            fp = engine.save_state(str(tmp_path / "state.json"))
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)

            assert "_compact" in data["project"]
            assert data["project"]["_compact"]["cycles_tail"] == 3
            assert data["project"]["_compact"]["cycles_truncated_before"] == 10 - 3
            assert "history_log_path" in data["project"]

    def test_save_state_no_project_raises(self):
        engine = self._make_engine()
        with pytest.raises(ValueError, match="没有项目状态"):
            engine.save_state("/tmp/state.json")

    def test_get_rendered_content(self):
        engine = self._make_engine()
        result = engine.get_rendered_content()
        assert isinstance(result, str)

    def test_persist_state_best_effort_does_not_crash_on_write_error(self, tmp_path):
        """Best-effort persistence should never raise even if disk write fails."""
        engine = self._make_engine()
        engine.root_path = str(tmp_path)
        engine._project = SpecProject.create(root_path=str(tmp_path))
        engine._project.requirement = "req"
        # Patch open used by save_state -> raise
        with patch("builtins.open", side_effect=OSError("disk full")):
            engine._persist_state_best_effort()
        # No exception expected

    def test_parse_review_output_strict_format(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        text = """[ARCHITECT]
PASS

[PRODUCT]
FAIL
- Need better error messages
- Add loading states

[USER]
PASS

[TESTER]
PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert result.reviews[0].passed is True  # ARCHITECT
        assert result.reviews[1].passed is False  # PRODUCT
        assert len(result.reviews[1].suggestions) == 2

    def test_parse_review_output_fallback_all_fail(self):
        engine = self._make_engine()
        engine._project = SpecProject.create(root_path="/tmp")
        # Completely unparseable text
        result = engine._parse_review_output("random garbage", 1)
        assert len(result.reviews) == 4
        assert all(not r.passed for r in result.reviews)


# ======================================================================
# TestSpecEngineManager — get_or_create, active, cleanup
# ======================================================================

class TestSpecEngineManager:
    @patch("src.spec_engine.engine.get_settings")
    def test_get_or_create(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        e1 = mgr.get_or_create("chat1", "/tmp/a")
        e2 = mgr.get_or_create("chat1", "/tmp/a")
        assert e1 is e2  # Same instance

    @patch("src.spec_engine.engine.get_settings")
    def test_get_different_paths(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        e1 = mgr.get_or_create("chat1", "/tmp/a")
        e2 = mgr.get_or_create("chat1", "/tmp/b")
        assert e1 is not e2

    @patch("src.spec_engine.engine.get_settings")
    def test_get_active_engine(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        e = mgr.get_or_create("chat1", "/tmp/a")
        assert mgr.get_active_engine("chat1") is None

        e._run_state = EngineRunState.RUNNING
        assert mgr.get_active_engine("chat1") is e

    @patch("src.spec_engine.engine.get_settings")
    def test_engine_name_switch(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        e1 = mgr.get_or_create("chat1", "/tmp/a", engine_name="Coco")
        assert e1.engine_name == "Coco"
        e2 = mgr.get_or_create("chat1", "/tmp/a", engine_name="Claude")
        assert e2.engine_name == "Claude"
        assert e1 is not e2  # New instance because name changed

    @patch("src.spec_engine.engine.get_settings")
    def test_engine_name_switch_blocked_while_running(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        e1 = mgr.get_or_create("chat1", "/tmp/a", engine_name="Coco")
        e1._run_state = EngineRunState.RUNNING
        e2 = mgr.get_or_create("chat1", "/tmp/a", engine_name="Claude")
        assert e2 is e1  # Not replaced because still running

    @patch("src.spec_engine.engine.get_settings")
    def test_cleanup_all(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        mgr.get_or_create("chat1", "/tmp/a")
        mgr.get_or_create("chat2", "/tmp/b")
        mgr.cleanup_all()
        assert mgr.list_engines() == []

    @patch("src.spec_engine.engine.get_settings")
    def test_load_or_create_from_disk_hydrates_project_and_resume_meta(self, mock_settings, tmp_path):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_max_cycles_limit = 5000
        s.spec_execution_timeout = 300
        s.spec_convergence_window = 2
        s.spec_review_enabled = False
        s.spec_cycle_tasks_max = 5
        s.spec_cycle_output_max_chars = 200
        s.spec_state_filename = ".spec_engine_state.json"
        s.spec_artifacts_dirname = ".spec_engine"
        s.spec_history_log_filename = "history.jsonl"
        s.spec_persist_every_phase = True
        s.spec_persist_phase_artifacts = False
        s.spec_allow_resume_from_disk = True
        s.spec_state_cycles_tail = 3
        s.spec_state_work_items_tail = 10
        s.spec_state_metrics_tail = 10
        s.spec_generated_specs_retention = 10
        s.spec_infinite_mode = False
        s.spec_disable_convergence = False
        s.spec_disable_early_stop = False
        s.ark_api_key = ""
        s.ark_model = ""
        mock_settings.return_value = s

        # Create a state file
        engine = SpecEngine(chat_id="c1", root_path=str(tmp_path))
        engine._project = SpecProject.create(root_path=str(tmp_path))
        engine._project.requirement = "req"
        engine.save_state()  # default state path

        mgr = SpecEngineManager()
        e2 = mgr.load_or_create_from_disk("c1", str(tmp_path), engine_name="Coco")
        assert e2.project is not None
        assert e2.project.requirement == "req"
        assert getattr(e2, "_resume_meta", None)

    @patch("src.spec_engine.engine.get_settings")
    def test_get_none_for_missing(self, mock_settings):
        s = MagicMock()
        mock_settings.return_value = s
        mgr = SpecEngineManager()
        assert mgr.get("chat1", "/tmp/a") is None
        assert mgr.get_active_engine("chat1") is None

    @patch("src.spec_engine.engine.get_settings")
    def test_list_engines(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        mgr.get_or_create("c1", "/tmp/a")
        mgr.get_or_create("c1", "/tmp/b")
        mgr.get_or_create("c2", "/tmp/c")
        assert len(mgr.list_engines()) == 3
        assert len(mgr.list_engines("c1")) == 2
        assert len(mgr.list_engines("c2")) == 1

    @patch("src.spec_engine.engine.get_settings")
    def test_get_active_engines(self, mock_settings):
        s = MagicMock()
        s.spec_max_cycles = 10
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        mock_settings.return_value = s

        mgr = SpecEngineManager()
        e1 = mgr.get_or_create("c1", "/tmp/a")
        e2 = mgr.get_or_create("c1", "/tmp/b")
        e1._run_state = EngineRunState.RUNNING
        active = mgr.get_active_engines("c1")
        assert len(active) == 1
        assert active[0] is e1


# ======================================================================
# TestSpecHandler — command routing
# ======================================================================

class TestSpecHandler:
    def _make_handler(self):
        from src.feishu.handlers.spec import SpecHandler
        ctx = self._make_handler_context()
        return SpecHandler(ctx)

    def _make_handler_context(self):
        from src.feishu.handler_context import HandlerContext
        return HandlerContext(
            settings=MagicMock(),
            api_client_factory=MagicMock(),
            message_callback=MagicMock(),
            coco_manager=MagicMock(),
            claude_manager=MagicMock(),
            intent_recognizer=MagicMock(),
            scheduler=MagicMock(),
            project_manager=MagicMock(),
            message_mapper=MagicMock(),
            message_linker=MagicMock(),
            mode_manager=MagicMock(),
            context_manager=MagicMock(),
            deep_engine_manager=MagicMock(),
            progress_reporter=MagicMock(),
            loop_engine_manager=MagicMock(),
            loop_reporter=MagicMock(),
            spec_engine_manager=MagicMock(),
            spec_reporter=MagicMock(),
            streaming_manager_factory=MagicMock(),
            image_handler_factory=MagicMock(),
            working_dirs={},
            working_dir_lock=threading.Lock(),
            pending_image_keys={},
            pending_image_lock=threading.Lock(),
            enable_streaming=False,
        )

    def test_handle_spec_command_routing_spec(self):
        handler = self._make_handler()
        handler.start_spec_engine = MagicMock()
        handler.handle_spec_command("mid", "cid", "/spec Build login", None)
        handler.start_spec_engine.assert_called_once()

    def test_handle_spec_command_routing_status(self):
        handler = self._make_handler()
        handler.show_spec_status = MagicMock()
        handler.handle_spec_command("mid", "cid", "/spec_status", None)
        handler.show_spec_status.assert_called_once()

    def test_handle_spec_command_routing_stop(self):
        handler = self._make_handler()
        handler.stop_spec_engine = MagicMock()
        handler.handle_spec_command("mid", "cid", "/stop_spec", None)
        handler.stop_spec_engine.assert_called_once()

    def test_handle_spec_command_routing_pause(self):
        handler = self._make_handler()
        handler.pause_spec_engine = MagicMock()
        handler.handle_spec_command("mid", "cid", "/spec_pause", None)
        handler.pause_spec_engine.assert_called_once()

    def test_handle_spec_command_routing_resume(self):
        handler = self._make_handler()
        handler.resume_spec_engine = MagicMock()
        handler.handle_spec_command("mid", "cid", "/spec_resume", None)
        handler.resume_spec_engine.assert_called_once()

    def test_handle_spec_command_routing_guide(self):
        handler = self._make_handler()
        handler.update_spec_guidance = MagicMock()
        handler.handle_spec_command("mid", "cid", "/spec_guide focus on auth", None)
        handler.update_spec_guidance.assert_called_once()

    def test_update_spec_guidance_allows_when_clarifying(self):
        """/spec_guide should work even when engine is CLARIFYING (not running)."""
        handler = self._make_handler()
        handler.send_message = MagicMock()
        handler.reply_message = MagicMock()

        project = MagicMock()
        project.root_path = "/tmp/p"
        project.project_id = "p1"
        handler.project_manager.get_active_project.return_value = project

        engine = MagicMock()
        engine.engine_name = "Coco"
        engine.is_running = False
        sp = SpecProject.create(name="p", root_path="/tmp/p")
        sp.status = SpecProjectStatus.CLARIFYING
        engine.project = sp

        handler.ctx.spec_engine_manager.get.return_value = engine
        handler.ctx.spec_engine_manager.list_engines.return_value = [engine]

        reporter = MagicMock()
        reporter.format_guidance_injected.return_value = "ok"
        reporter.get_guidance_injected_title.return_value = "title"
        handler.ctx.spec_reporter = reporter

        with patch("src.feishu.handlers.spec.CardBuilder.build_deep_card", return_value=("interactive", "card")):
            handler.update_spec_guidance("mid", "cid", "Q1: answer", project=None)

        engine.inject_guidance.assert_called_once_with("Q1: answer")
        handler.reply_message.assert_not_called()
        handler.send_message.assert_called_once()


# ======================================================================
# TestSystemHandler — is_spec_command predicate
# ======================================================================

class TestSystemHandlerSpec:
    def test_is_spec_command(self):
        from src.feishu.handlers.system import SystemHandler
        assert SystemHandler.is_spec_command("/spec build auth")
        assert SystemHandler.is_spec_command("/spec_status")
        assert SystemHandler.is_spec_command("/stop_spec")
        assert SystemHandler.is_spec_command("/spec_pause")
        assert SystemHandler.is_spec_command("/spec_resume")
        assert SystemHandler.is_spec_command("/spec_guide focus")
        assert not SystemHandler.is_spec_command("/loop build")
        assert not SystemHandler.is_spec_command("/deep do stuff")
        assert not SystemHandler.is_spec_command("hello")


# ======================================================================
# TestIntentRecognizer — spec intents
# ======================================================================

class TestIntentRecognizerSpec:
    def test_spec_intent_types_exist(self):
        from src.agent.intent_recognizer import IntentType
        assert hasattr(IntentType, "ENTER_SPEC")
        assert hasattr(IntentType, "SPEC_STATUS")
        assert hasattr(IntentType, "STOP_SPEC")
        assert hasattr(IntentType, "SPEC_PAUSE")
        assert hasattr(IntentType, "SPEC_RESUME")
        assert hasattr(IntentType, "SPEC_GUIDE")

    def test_spec_exact_commands(self):
        from src.agent.intent_recognizer import IntentRecognizer
        recognizer = IntentRecognizer()
        # Quick match: /spec_status
        result = recognizer.recognize("/spec_status", "smart")
        from src.agent.intent_recognizer import IntentType
        assert result.primary_intent == IntentType.SPEC_STATUS

    def test_spec_guide_quick_match(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        recognizer = IntentRecognizer()
        result = recognizer.recognize("/spec_guide focus on tests", "smart")
        assert result.primary_intent == IntentType.SPEC_GUIDE


# ======================================================================
# TestConfig — spec engine settings
# ======================================================================

class TestConfigSpec:
    def test_spec_settings_defaults(self):
        from src.config import Settings
        s = Settings(app_id="", app_secret="")
        assert s.spec_max_cycles == 500
        assert s.spec_max_cycles_limit >= 5000
        assert s.spec_execution_timeout == 7200
        assert s.spec_convergence_window == 2
        assert s.spec_review_enabled is True
        assert s.spec_discovery_enabled is True


# ======================================================================
# TestCardBuilder — spec color
# ======================================================================

class TestCardBuilderSpec:
    def test_pick_engine_template_spec(self):
        from src.card.builder import CardBuilder
        assert CardBuilder._pick_engine_template("Spec(Coco)") == "green"
        assert CardBuilder._pick_engine_template("spec") == "green"
        assert CardBuilder._pick_engine_template("Coco") == "blue"
        assert CardBuilder._pick_engine_template("Claude") == "purple"


# ======================================================================
# TestSpecEngineExecution — integration tests for execute/resume/review
# ======================================================================

class TestSpecEngineExecution:
    """Integration tests for execute, resume, review, criteria evaluation."""

    def _mock_settings(self):
        s = MagicMock()
        s.spec_max_cycles = 3
        s.spec_max_cycles_limit = 5000
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        s.spec_review_enabled = True
        s.spec_cycle_tasks_max = 50
        s.spec_cycle_output_max_chars = 4000
        s.spec_state_filename = ".spec_engine_state.json"
        s.spec_artifacts_dirname = ".spec_engine"
        s.spec_persist_phase_artifacts = True
        s.spec_persist_every_phase = True
        # Legacy integration tests focus on spec/plan/task/build/review/criteria only.
        s.spec_discovery_enabled = False
        s.spec_discovery_max_questions = 3
        s.spec_discovery_force_nonempty = True
        s.spec_generated_specs_per_cycle = 1
        s.spec_allow_resume_from_disk = True
        s.ark_api_key = ""
        s.ark_model = ""
        return s

    def _make_mock_session(self, text_responses):
        """Mock session that returns text_responses sequentially via on_event."""
        session = MagicMock()
        call_index = [0]
        responses = list(text_responses)

        def fake_send_prompt(prompt, on_event=None, timeout=None):
            idx = call_index[0]
            call_index[0] += 1
            text = responses[idx] if idx < len(responses) else ""
            if on_event and text:
                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=text))

        session.send_prompt = fake_send_prompt
        return session

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_single_cycle_all_pass(self, mock_settings, mock_create):
        """Full execute: 1 cycle, all reviews PASS, criteria PASS → COMPLETED."""
        mock_settings.return_value = self._mock_settings()

        review_text = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        criteria_text = "CRITERIA_1: PASS"
        spec_json = """```json\n{\"goals\":[\"G\"],\"functional_spec\":[\"F\"],\"non_functional_requirements\":[\"N\"],\"acceptance_criteria\":[\"实现登录功能\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"A\",\"tech_stack\":[\"T\"],\"steps\":[\"S1\"],\"file_changes\":[\"x.py\"],\"test_plan\":[\"pytest\"],\"risks\":[],\"version\":\"1.0\"}\n```"""

        # Order: spec, plan, task, build, review, criteria_eval
        session = self._make_mock_session([
            spec_json, plan_json,
            "1. Task one (依赖: 无)",
            "build done " * 20,
            review_text, criteria_text,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        called = {"analyzing_start": False, "project_done": False, "cycles": []}
        callbacks = SpecEngineCallbacks(
            on_analyzing_start=lambda r: called.__setitem__("analyzing_start", True),
            on_cycle_done=lambda c, cy: called["cycles"].append(c),
            on_project_done=lambda p: called.__setitem__("project_done", True),
        )

        project = engine.execute("- 实现登录功能", callbacks)

        assert project.status == SpecProjectStatus.COMPLETED
        assert len(project.cycles) == 1
        assert project.cycles[0].status == "completed"
        assert "\"acceptance_criteria\"" in project.cycles[0].spec_content
        assert "\"file_changes\"" in project.cycles[0].plan_content
        assert project.cycles[0].spec_artifact is not None
        assert project.cycles[0].plan_artifact is not None
        assert len(project.cycles[0].tasks) == 1
        assert project.cycles[0].review_result.all_passed
        assert called["analyzing_start"]
        assert called["project_done"]
        assert called["cycles"] == [1]
        assert engine.run_state == EngineRunState.IDLE

    @patch("src.spec_engine.engine.close_session_safely")
    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_closes_session(self, mock_settings, mock_create, mock_close):
        """execute() should always close the underlying session in finally."""
        mock_settings.return_value = self._mock_settings()

        review_text = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        criteria_text = "CRITERIA_1: PASS"
        spec_json = """```json\n{\"goals\":[\"G\"],\"functional_spec\":[\"F\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"实现登录功能\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"A\",\"tech_stack\":[],\"steps\":[\"S\"],\"file_changes\":[],\"test_plan\":[],\"risks\":[],\"version\":\"1.0\"}\n```"""

        session = self._make_mock_session([
            spec_json, plan_json,
            "1. Task one (依赖: 无)",
            "build done " * 20,
            review_text, criteria_text,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        project = engine.execute("- 实现登录功能")
        assert project.status == SpecProjectStatus.COMPLETED
        mock_close.assert_called()
        # Ensure we attempted to close the same session instance
        assert mock_close.call_args[0][0] is session

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_enters_clarifying_when_questions_present(self, mock_settings, mock_create):
        """If spec artifact has clarification_questions, engine should stop and mark CLARIFYING."""
        mock_settings.return_value = self._mock_settings()

        spec_json = """```json\n{\"goals\":[\"G\"],\"functional_spec\":[\"F\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"需要登录\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[\"是否需要支持手机号登录？\"],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        calls = {"n": 0}
        session = MagicMock()

        def fake_send_prompt(prompt, on_event=None, timeout=None):
            calls["n"] += 1
            if on_event:
                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=spec_json))

        session.send_prompt = fake_send_prompt
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        project = engine.execute("- 需要登录")

        assert project.status == SpecProjectStatus.CLARIFYING
        assert len(project.cycles) == 1
        assert project.cycles[0].status == "clarifying"
        assert project.cycles[0].spec_artifact is not None
        assert project.cycles[0].spec_artifact.clarification_questions
        assert calls["n"] == 1  # only SPEC phase executed

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_multi_cycle_then_pass(self, mock_settings, mock_create):
        """Cycle 1 FAIL review → cycle 2 all PASS → COMPLETED in 2 cycles."""
        mock_settings.return_value = self._mock_settings()

        review_fail = "[ARCHITECT]\nFAIL\n- Fix issue\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        review_pass = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        spec1 = """```json\n{\"goals\":[\"G\"],\"functional_spec\":[\"F\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"功能要求可用\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan1 = """```json\n{\"architecture\":\"A\",\"tech_stack\":[],\"steps\":[\"S\"],\"file_changes\":[],\"test_plan\":[],\"risks\":[],\"version\":\"1.0\"}\n```"""
        spec2 = spec1
        plan2 = plan1
        session = self._make_mock_session([
            # Cycle 1
            spec1, plan1, "1. T1 (依赖: 无)", "build1 " * 20,
            review_fail, "CRITERIA_1: FAIL",
            # Cycle 2
            spec2, plan2, "1. T1 (依赖: 无)", "build2 " * 20,
            review_pass, "CRITERIA_1: PASS",
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        project = engine.execute("- 功能要求")

        assert project.status == SpecProjectStatus.COMPLETED
        assert len(project.cycles) == 2
        assert not project.cycles[0].review_result.all_passed
        assert project.cycles[1].review_result.all_passed

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_stop_mid_cycle(self, mock_settings, mock_create):
        """Stop during SPEC phase → cycle saved as failed, project PAUSED."""
        mock_settings.return_value = self._mock_settings()

        def fake_send_prompt(prompt, on_event=None, timeout=None):
            if on_event:
                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="partial"))

        session = MagicMock()
        session.send_prompt = fake_send_prompt
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")

        # Stop after first phase completes
        original = engine._run_phase
        def stop_after_first(cycle_num, phase, prompt, callbacks, timeout):
            result = original(cycle_num, phase, prompt, callbacks, timeout)
            engine._run_state = EngineRunState.STOPPING
            return result
        engine._run_phase = stop_after_first

        project = engine.execute("- test requirement")

        assert project.status == SpecProjectStatus.PAUSED
        assert len(project.cycles) == 1
        assert project.cycles[0].status == "failed"
        assert engine.run_state == EngineRunState.IDLE

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_exception_handling(self, mock_settings, mock_create):
        """Exception during session creation → ABORTED + on_error called."""
        mock_settings.return_value = self._mock_settings()
        mock_create.side_effect = RuntimeError("connection failed")

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        error_msgs = []
        callbacks = SpecEngineCallbacks(on_error=lambda e: error_msgs.append(e))

        project = engine.execute("- test req", callbacks)

        assert project.status == SpecProjectStatus.ABORTED
        assert len(error_msgs) == 1
        assert "connection failed" in error_msgs[0]
        assert engine.run_state == EngineRunState.IDLE

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_resume_from_paused(self, mock_settings, mock_create):
        """Resume a paused engine → continues from next cycle."""
        mock_settings.return_value = self._mock_settings()

        review_pass = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        session = self._make_mock_session([
            "spec_r", "plan_r", "1. T1 (依赖: 无)", "build_r " * 20,
            review_pass, "CRITERIA_1: PASS",
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        # Set up paused state with 1 existing cycle
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "- 功能要求"
        engine._project.acceptance_criteria = ["功能要求"]
        engine._project.criteria_tracker.init_criteria(["功能要求"])
        engine._project.status = SpecProjectStatus.PAUSED
        engine._project.started_at = time.time()
        cycle = SpecCycle(cycle_number=1)
        cycle.complete()
        engine._project.cycles.append(cycle)

        project = engine.resume()

        assert project.status == SpecProjectStatus.COMPLETED
        assert len(project.cycles) == 2  # 1 existing + 1 new
        assert project.cycles[1].cycle_number == 2
        assert engine.run_state == EngineRunState.IDLE

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_resume_saves_failed_cycle_on_stop(self, mock_settings, mock_create):
        """Resume with stop → failed cycle is saved (bug fix from review)."""
        mock_settings.return_value = self._mock_settings()

        def fake_send_prompt(prompt, on_event=None, timeout=None):
            if on_event:
                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="partial"))

        session = MagicMock()
        session.send_prompt = fake_send_prompt
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "- req"
        engine._project.acceptance_criteria = ["req"]
        engine._project.criteria_tracker.init_criteria(["req"])
        engine._project.status = SpecProjectStatus.PAUSED
        engine._project.started_at = time.time()

        # Stop after first phase
        original = engine._run_phase
        def stop_after_first(cycle_num, phase, prompt, callbacks, timeout):
            result = original(cycle_num, phase, prompt, callbacks, timeout)
            engine._run_state = EngineRunState.STOPPING
            return result
        engine._run_phase = stop_after_first

        project = engine.resume()

        assert project.status == SpecProjectStatus.PAUSED
        assert len(project.cycles) == 1
        assert project.cycles[0].status == "failed"

    @patch("src.spec_engine.engine.get_settings")
    def test_resume_not_paused_noop(self, mock_settings):
        """Resume when not paused → returns project unchanged."""
        mock_settings.return_value = self._mock_settings()

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        assert engine.resume() is None

        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.status = SpecProjectStatus.RUNNING
        result = engine.resume()
        assert result.status == SpecProjectStatus.RUNNING

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_resume_exception_handling(self, mock_settings, mock_create):
        """Exception during resume → ABORTED + on_error called."""
        mock_settings.return_value = self._mock_settings()
        mock_create.side_effect = RuntimeError("session failed")

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "req"
        engine._project.status = SpecProjectStatus.PAUSED
        error_msgs = []
        callbacks = SpecEngineCallbacks(on_error=lambda e: error_msgs.append(e))

        project = engine.resume(callbacks)

        assert project.status == SpecProjectStatus.ABORTED
        assert len(error_msgs) == 1
        assert "session failed" in error_msgs[0]
        assert engine.run_state == EngineRunState.IDLE

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_conduct_review_with_session(self, mock_settings, mock_create):
        """_conduct_review sends prompt and parses result."""
        mock_settings.return_value = self._mock_settings()

        review_text = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nFAIL\n- Add error handling\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        session = self._make_mock_session([review_text])

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = session
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "Build login"

        result = engine._conduct_review(1, SpecEngineCallbacks())

        assert len(result.reviews) == 4
        assert result.reviews[0].passed  # ARCHITECT
        assert not result.reviews[1].passed  # PRODUCT
        assert "Add error handling" in result.reviews[1].suggestions

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_conduct_review_exception(self, mock_settings, mock_create):
        """_conduct_review handles exception → all FAIL with error message."""
        mock_settings.return_value = self._mock_settings()

        session = MagicMock()
        session.send_prompt.side_effect = RuntimeError("timeout")

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = session
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "Build login"

        result = engine._conduct_review(1, SpecEngineCallbacks())

        assert len(result.reviews) == 4
        assert all(not r.passed for r in result.reviews)
        assert any("timeout" in s for r in result.reviews for s in r.suggestions)

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_conduct_review_no_session(self, mock_settings, mock_create):
        """_conduct_review without session → empty ReviewResult."""
        mock_settings.return_value = self._mock_settings()

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = None

        result = engine._conduct_review(1, SpecEngineCallbacks())
        assert result.reviews == []

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_evaluate_criteria_with_session(self, mock_settings, mock_create):
        """_evaluate_criteria parses PASS/FAIL per criterion."""
        mock_settings.return_value = self._mock_settings()

        eval_text = "CRITERIA_1: PASS\nCRITERIA_2: FAIL\nCRITERIA_3: PASS"
        session = self._make_mock_session([eval_text])

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = session
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.criteria_tracker.init_criteria(["C1", "C2", "C3"])

        result = engine._evaluate_criteria(["C1", "C2", "C3"], 1)

        assert not result["all_satisfied"]
        assert engine._project.criteria_tracker.satisfied.get(0) is True
        assert engine._project.criteria_tracker.satisfied.get(1) is False
        assert engine._project.criteria_tracker.satisfied.get(2) is True

    @patch("src.spec_engine.engine.get_settings")
    def test_evaluate_criteria_no_session(self, mock_settings):
        """_evaluate_criteria without session → not satisfied."""
        mock_settings.return_value = self._mock_settings()

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = None

        result = engine._evaluate_criteria(["C1"], 1)
        assert not result["all_satisfied"]

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_evaluate_criteria_exception(self, mock_settings, mock_create):
        """_evaluate_criteria handles exception → not satisfied."""
        mock_settings.return_value = self._mock_settings()

        session = MagicMock()
        session.send_prompt.side_effect = RuntimeError("oops")

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = session
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.criteria_tracker.init_criteria(["C1"])

        result = engine._evaluate_criteria(["C1"], 1)
        assert not result["all_satisfied"]

    def test_convergence_with_stagnant_review_suggestions(self):
        """Convergence detects stagnant review suggestions across window."""
        with patch("src.spec_engine.engine.get_settings") as mock_settings:
            s = MagicMock()
            s.spec_max_cycles = 10
            s.spec_convergence_window = 2
            s.spec_execution_timeout = 300
            mock_settings.return_value = s

            engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
            engine._project = SpecProject.create(root_path="/tmp/test")
            engine._project.criteria_tracker.init_criteria(["C1", "C2"])

            # 2 cycles with same non-zero suggestion count → converge
            def _make_review(n_suggestions, iteration):
                return ReviewResult(reviews=[
                    PerspectiveReview(
                        perspective=ReviewPerspective.ARCHITECT, passed=False,
                        suggestions=[f"S{i}" for i in range(n_suggestions)],
                        summary=f"{n_suggestions}条建议"),
                    PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=True,
                                      suggestions=[], summary="通过"),
                    PerspectiveReview(perspective=ReviewPerspective.USER, passed=True,
                                      suggestions=[], summary="通过"),
                    PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=True,
                                      suggestions=[], summary="通过"),
                ], iteration=iteration)

            engine._project.cycles = [
                SpecCycle(cycle_number=1, build_output="x" * 100,
                         review_result=_make_review(1, 1)),
                SpecCycle(cycle_number=2, build_output="y" * 100,
                         review_result=_make_review(1, 2)),
            ]
            assert engine._detect_convergence()

    def test_convergence_not_triggered_when_improving(self):
        """Convergence NOT triggered when suggestions are decreasing."""
        with patch("src.spec_engine.engine.get_settings") as mock_settings:
            s = MagicMock()
            s.spec_max_cycles = 10
            s.spec_convergence_window = 2
            s.spec_execution_timeout = 300
            mock_settings.return_value = s

            engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
            engine._project = SpecProject.create(root_path="/tmp/test")
            engine._project.criteria_tracker.init_criteria(["C1", "C2"])

            def _make_review(n_suggestions, iteration):
                return ReviewResult(reviews=[
                    PerspectiveReview(
                        perspective=ReviewPerspective.ARCHITECT, passed=False,
                        suggestions=[f"S{i}" for i in range(n_suggestions)],
                        summary=f"{n_suggestions}条建议"),
                    PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=True,
                                      suggestions=[], summary="通过"),
                    PerspectiveReview(perspective=ReviewPerspective.USER, passed=True,
                                      suggestions=[], summary="通过"),
                    PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=True,
                                      suggestions=[], summary="通过"),
                ], iteration=iteration)

            engine._project.cycles = [
                SpecCycle(cycle_number=1, build_output="x" * 100,
                         review_result=_make_review(3, 1)),
                SpecCycle(cycle_number=2, build_output="y" * 100,
                         review_result=_make_review(1, 2)),
            ]
            assert not engine._detect_convergence()

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_execute_review_disabled(self, mock_settings, mock_create):
        """When spec_review_enabled=False, review phase is skipped entirely."""
        s = self._mock_settings()
        s.spec_review_enabled = False
        mock_settings.return_value = s

        spec_json = """```json\n{\"goals\":[\"G\"],\"functional_spec\":[\"F\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"实现登录功能\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"A\",\"tech_stack\":[],\"steps\":[\"S\"],\"file_changes\":[],\"test_plan\":[],\"risks\":[],\"version\":\"1.0\"}\n```"""
        # Only need 5 prompts (spec, plan, task, build, criteria) — no review
        criteria_text = "CRITERIA_1: PASS"
        session = self._make_mock_session([
            spec_json, plan_json,
            "1. Task one (依赖: 无)",
            "build done " * 20,
            criteria_text,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        review_events = []
        callbacks = SpecEngineCallbacks(
            on_review_done=lambda c, r: review_events.append((c, r)),
        )

        project = engine.execute("- 实现登录功能", callbacks)

        assert project.status == SpecProjectStatus.COMPLETED
        assert len(project.cycles) == 1
        # Review was skipped — no review result, no callback fired
        assert project.cycles[0].review_result is None
        assert review_events == []

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_discovery_generates_spec_files_and_backlog(self, mock_settings, mock_create, tmp_path):
        """每轮循环后触发问题发现→生成 spec 文件→加入 backlog，并能被下一轮加载执行。"""
        s = self._mock_settings()
        s.spec_max_cycles = 2
        s.spec_review_enabled = False
        s.spec_discovery_enabled = True
        s.spec_discovery_max_questions = 1
        s.spec_generated_specs_per_cycle = 1
        s.spec_convergence_window = 0
        # Keep artifacts tiny for test
        s.spec_cycle_artifact_retention = 1
        mock_settings.return_value = s

        spec_json = """```json\n{\"goals\":[\"G\"],\"functional_spec\":[\"F\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"实现登录功能\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"A\",\"tech_stack\":[],\"steps\":[\"S\"],\"file_changes\":[],\"test_plan\":[],\"risks\":[],\"version\":\"1.0\"}\n```"""
        discovery1 = """```json\n[{"id":"Q-1","question":"如何提升错误提示可用性？","why":"用户体验","priority":"P1"}]\n```"""
        gen1 = """```json\n[{"id":"Q-1","spec":{"goals":["提升错误提示"],"functional_spec":["完善错误提示"],"non_functional_requirements":[],"acceptance_criteria":["错误提示清晰可读"],"out_of_scope":[],"risks":[],"clarification_questions":[],"decisions":[],"version":"1.0"}}]\n```"""
        discovery2 = """```json\n[{"id":"Q-2","question":"如何补齐关键测试覆盖？","why":"质量保证","priority":"P1"}]\n```"""
        gen2 = """```json\n[{"id":"Q-2","spec":{"goals":["补齐测试"],"functional_spec":["新增单元测试"],"non_functional_requirements":[],"acceptance_criteria":["关键路径有单测"],"out_of_scope":[],"risks":[],"clarification_questions":[],"decisions":[],"version":"1.0"}}]\n```"""

        # Cycle 1: spec, plan, task, build, criteria, discovery, gen
        # Cycle 2: (spec loaded from file), plan, task, build, criteria, discovery, gen
        session = self._make_mock_session([
            spec_json, plan_json,
            "1. T1 (依赖: 无)",
            "build ok",
            "CRITERIA_1: FAIL",
            discovery1, gen1,
            plan_json,
            "1. T2 (依赖: 无)",
            "build ok 2",
            "CRITERIA_1: PASS",
            discovery2, gen2,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path=str(tmp_path))
        project = engine.execute("- 实现登录功能")

        assert project.status == SpecProjectStatus.COMPLETED
        assert len(project.cycles) == 2
        assert len(project.work_items) >= 2
        # The first generated item should have been consumed in cycle 2
        assert project.work_items[0].used_in_cycle in (1, 2)
        assert os.path.exists(project.work_items[0].spec_path)

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_5000_cycles_stability_with_persistence_and_resume(self, mock_settings, mock_create, tmp_path):
        """验证 5000 次完整循环可稳定执行，并支持落盘 + 断点续传加载。"""
        s = MagicMock()
        s.spec_max_cycles = 5000
        s.spec_max_cycles_limit = 5000
        s.spec_execution_timeout = 300
        s.spec_convergence_window = 0
        s.spec_review_enabled = False
        s.spec_cycle_tasks_max = 5
        s.spec_cycle_output_max_chars = 200
        s.spec_state_filename = ".spec_engine_state.json"
        s.spec_artifacts_dirname = ".spec_engine"
        s.spec_persist_phase_artifacts = False
        s.spec_phase_output_persist_max_chars = 200
        s.spec_cycle_artifact_retention = 1
        s.spec_persist_every_phase = True
        s.spec_discovery_enabled = True
        s.spec_discovery_max_questions = 1
        s.spec_discovery_force_nonempty = True
        s.spec_generated_specs_per_cycle = 1
        s.spec_allow_resume_from_disk = True
        s.ark_api_key = ""
        s.ark_model = ""
        mock_settings.return_value = s

        class DynamicSession:
            def __init__(self):
                self.disc_n = 0

            def send_prompt(self, prompt, on_event=None, timeout=None):
                p = prompt or ""
                out = ""
                if "请使用 spec-kit 风格产出“规格（Spec）”" in p:
                    out = """```json\n{"goals":["G"],"functional_spec":["F"],"non_functional_requirements":[],"acceptance_criteria":["永不完成"],"out_of_scope":[],"risks":[],"clarification_questions":[],"decisions":[],"version":"1.0"}\n```"""
                elif "产出 Plan（规划）" in p and "\"file_changes\"" in p:
                    out = """```json\n{"architecture":"A","tech_stack":[],"steps":["S"],"file_changes":[],"test_plan":[],"risks":[],"version":"1.0"}\n```"""
                elif "格式（严格遵循）" in p and "任务编号" in p:
                    out = "1. T (依赖: 无)"
                elif "按以下任务列表逐步执行实现" in p:
                    out = "build"
                elif "请评估以下验收标准是否已满足" in p:
                    out = "CRITERIA_1: FAIL"
                elif "自动发现与目标相关的“可优化问题”" in p:
                    self.disc_n += 1
                    out = f"```json\n[{{\"id\":\"Q-{self.disc_n}\",\"question\":\"优化点 {self.disc_n}\",\"why\":\"why\",\"priority\":\"P1\"}}]\n```"
                elif "spec-kit 规格生成器" in p:
                    m = re.search(r'"id"\s*:\s*"(Q-[^"]+)"', p)
                    qid = m.group(1) if m else "Q-X"
                    out = (
                        "```json\n["
                        + json.dumps({"id": qid, "spec": {
                            "goals": [f"解决 {qid}"],
                            "functional_spec": ["F"],
                            "non_functional_requirements": [],
                            "acceptance_criteria": ["永不完成"],
                            "out_of_scope": [],
                            "risks": [],
                            "clarification_questions": [],
                            "decisions": [],
                            "version": "1.0",
                        }}, ensure_ascii=False)
                        + "]\n```"
                    )
                else:
                    out = ""

                if on_event and out:
                    on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=out))

        mock_create.return_value = DynamicSession()

        engine = SpecEngine(chat_id="c1", root_path=str(tmp_path))
        t0 = time.time()
        project = engine.execute("- 永不完成")
        elapsed = time.time() - t0

        assert len(project.cycles) == 5000
        assert project.cycles[-1].status == "completed"
        assert project.status == SpecProjectStatus.ABORTED
        # State file must exist and be loadable
        state_path = tmp_path / ".spec_engine_state.json"
        assert state_path.exists()
        loaded = SpecEngine.load_state(str(state_path))
        assert loaded is not None
        assert loaded.current_cycle_number == 5000
        # Basic performance guard (avoid regressions)
        assert elapsed < 60


# ======================================================================
# TestSpecEngineProjectTypes — web/api/script variants
# ======================================================================


class TestSpecEngineProjectTypes:
    def _mock_settings(self):
        s = MagicMock()
        s.spec_max_cycles = 2
        s.spec_max_cycles_limit = 5000
        s.spec_convergence_window = 2
        s.spec_execution_timeout = 300
        s.spec_review_enabled = True
        s.spec_cycle_tasks_max = 50
        s.spec_cycle_output_max_chars = 4000
        s.spec_state_filename = ".spec_engine_state.json"
        s.spec_artifacts_dirname = ".spec_engine"
        s.spec_persist_phase_artifacts = True
        s.spec_persist_every_phase = True
        s.spec_discovery_enabled = False
        s.spec_discovery_max_questions = 3
        s.spec_discovery_force_nonempty = True
        s.spec_generated_specs_per_cycle = 1
        s.spec_allow_resume_from_disk = True
        s.ark_api_key = ""
        s.ark_model = ""
        return s

    def _make_mock_session(self, text_responses):
        session = MagicMock()
        idx = [0]
        responses = list(text_responses)

        def fake_send_prompt(prompt, on_event=None, timeout=None):
            i = idx[0]
            idx[0] += 1
            text = responses[i] if i < len(responses) else ""
            if on_event and text:
                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=text))

        session.send_prompt = fake_send_prompt
        return session

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_web_app_flow_no_missing_artifacts(self, mock_settings, mock_create):
        mock_settings.return_value = self._mock_settings()

        spec_json = """```json\n{\"goals\":[\"Web 登录\"],\"functional_spec\":[\"页面\",\"接口\"],\"non_functional_requirements\":[\"性能\"],\"acceptance_criteria\":[\"Web 登录可用\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"MVC\",\"tech_stack\":[\"FastAPI\",\"React\"],\"steps\":[\"实现 API\",\"实现 UI\"],\"file_changes\":[\"src/app.py\"],\"test_plan\":[\"pytest\"],\"risks\":[],\"version\":\"1.0\"}\n```"""
        review_text = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        criteria_text = "CRITERIA_1: PASS"

        session = self._make_mock_session([
            spec_json, plan_json,
            "1. 实现 Web 登录 (依赖: 无)",
            "build ok " * 10,
            review_text, criteria_text,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        project = engine.execute("- Web 需求")
        assert project.status == SpecProjectStatus.COMPLETED
        assert project.cycles[0].spec_artifact is not None
        assert project.cycles[0].plan_artifact is not None

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_api_dev_flow_no_missing_artifacts(self, mock_settings, mock_create):
        mock_settings.return_value = self._mock_settings()

        spec_json = """```json\n{\"goals\":[\"API 开发\"],\"functional_spec\":[\"REST\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"API 返回符合预期\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"HTTP API\",\"tech_stack\":[\"FastAPI\"],\"steps\":[\"实现 endpoint\"],\"file_changes\":[\"src/api.py\"],\"test_plan\":[\"pytest -k api\"],\"risks\":[],\"version\":\"1.0\"}\n```"""
        review_text = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        criteria_text = "CRITERIA_1: PASS"

        session = self._make_mock_session([
            spec_json, plan_json,
            "1. 实现 API (依赖: 无)",
            "build ok " * 10,
            review_text, criteria_text,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        project = engine.execute("- API 需求")
        assert project.status == SpecProjectStatus.COMPLETED
        assert project.cycles[0].spec_artifact is not None
        assert project.cycles[0].plan_artifact is not None

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.spec_engine.engine.get_settings")
    def test_script_tool_flow_no_missing_artifacts(self, mock_settings, mock_create):
        mock_settings.return_value = self._mock_settings()

        spec_json = """```json\n{\"goals\":[\"脚本工具\"],\"functional_spec\":[\"CLI\"],\"non_functional_requirements\":[],\"acceptance_criteria\":[\"CLI 可执行并输出正确\"],\"out_of_scope\":[],\"risks\":[],\"clarification_questions\":[],\"decisions\":[],\"version\":\"1.0\"}\n```"""
        plan_json = """```json\n{\"architecture\":\"单文件脚本\",\"tech_stack\":[\"Python\"],\"steps\":[\"实现命令解析\"],\"file_changes\":[\"tools/foo.py\"],\"test_plan\":[\"pytest -k tool\"],\"risks\":[],\"version\":\"1.0\"}\n```"""
        review_text = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nPASS\n\n[USER]\nPASS\n\n[TESTER]\nPASS\n"
        criteria_text = "CRITERIA_1: PASS"

        session = self._make_mock_session([
            spec_json, plan_json,
            "1. 实现脚本工具 (依赖: 无)",
            "build ok " * 10,
            review_text, criteria_text,
        ])
        mock_create.return_value = session

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        project = engine.execute("- 脚本需求")
        assert project.status == SpecProjectStatus.COMPLETED
        assert project.cycles[0].spec_artifact is not None
        assert project.cycles[0].plan_artifact is not None
