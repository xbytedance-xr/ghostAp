"""Tests for loop_engine — ACP-driven LoopEngine."""

import logging
import re
from unittest.mock import MagicMock, patch

import pytest

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo, ToolCallInfo
from src.engine_base import EngineRunState, PerspectiveReview, ReviewPerspective, ReviewResult
from src.loop_engine.engine import LoopEngine, LoopEngineCallbacks, LoopEngineManager
from src.loop_engine.models import (
    IterationRecord,
    IterationStatus,
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
)
from src.loop_engine.reporter import LoopReporter
from src.loop_engine.tracker import IterationTracker


class TestLoopEngine:
    @patch("src.engine_base.get_settings")
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
        assert engine._user_guidance == ["focus on login"]
        # Multiple injections accumulate
        engine.inject_guidance("also fix logout")
        assert engine._user_guidance == ["focus on login", "also fix logout"]

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

    @patch("src.loop_engine.engine.ChatOpenAI")
    def test_parse_requirement_no_criteria_uses_llm(self, mock_chat):
        """When no list markers, LLM decomposes the requirement."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="- 实现登录接口\n- 支持错误提示\n- 添加单元测试")
        mock_chat.return_value = mock_llm

        engine = self._make_engine()
        engine.settings.ark_api_key = "test-key"
        engine.settings.ark_model = "test-model"
        text = "实现登录功能，要有错误提示，还要有测试"
        req = engine._parse_requirement(text)
        assert len(req.acceptance_criteria) == 3
        assert "实现登录接口" in req.acceptance_criteria
        mock_llm.invoke.assert_called_once()

    @patch("src.loop_engine.engine.ChatOpenAI")
    def test_parse_requirement_llm_failure_fallback(self, mock_chat):
        """When LLM fails, fall back to raw text as criterion."""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("API error")
        mock_chat.return_value = mock_llm

        engine = self._make_engine()
        engine.settings.ark_api_key = "test-key"
        engine.settings.ark_model = "test-model"
        text = "实现登录功能"
        req = engine._parse_requirement(text)
        assert len(req.acceptance_criteria) == 1
        assert "完成需求" in req.acceptance_criteria[0]
        # Full text should be preserved, not truncated
        assert "实现登录功能" in req.acceptance_criteria[0]

    def test_parse_requirement_no_api_key_fallback(self):
        """When no API key configured, skip LLM and fall back."""
        engine = self._make_engine()
        engine.settings.ark_api_key = ""
        engine.settings.ark_model = ""
        text = "实现登录功能"
        req = engine._parse_requirement(text)
        assert len(req.acceptance_criteria) == 1
        assert "完成需求" in req.acceptance_criteria[0]

    def test_extract_criteria_from_llm_response(self):
        """Test various LLM response formats for criteria extraction."""
        # Dash format
        text = "- 标准1\n- 标准2\n- 标准3"
        result = LoopEngine._extract_criteria_from_llm_response(text)
        assert result == ["标准1", "标准2", "标准3"]

        # Numbered format
        text = "1. 标准一\n2. 标准二\n3. 标准三"
        result = LoopEngine._extract_criteria_from_llm_response(text)
        assert result == ["标准一", "标准二", "标准三"]

        # Chinese numbered format
        text = "1、标准一\n2、标准二"
        result = LoopEngine._extract_criteria_from_llm_response(text)
        assert result == ["标准一", "标准二"]

        # Mixed with extra text
        text = "以下是验收标准：\n- 标准A\n  some detail\n- 标准B"
        result = LoopEngine._extract_criteria_from_llm_response(text)
        assert result == ["标准A", "标准B"]

        # Empty or no criteria
        assert LoopEngine._extract_criteria_from_llm_response("") == []
        assert LoopEngine._extract_criteria_from_llm_response("没有格式化内容") == []

    def test_build_initial_prompt(self):
        engine = self._make_engine()
        req = LoopRequirement(
            goal="add login",
            acceptance_criteria=["email login", "phone login"],
            raw_text="test",
        )
        prompt = engine._build_initial_prompt(req)
        assert "add login" in prompt
        assert "email login" in prompt
        assert "/tmp/test" in prompt

    def test_build_iteration_prompt(self):
        engine = self._make_engine()
        req = LoopRequirement(
            goal="add login",
            acceptance_criteria=["email login"],
            raw_text="test",
        )
        prompt = engine._build_iteration_prompt(2, req)
        assert "第 2 轮" in prompt
        assert "email login" in prompt

    def test_build_iteration_prompt_with_guidance(self):
        engine = self._make_engine()
        engine.inject_guidance("prioritize email")
        req = LoopRequirement(
            goal="login",
            acceptance_criteria=["c1"],
            raw_text="test",
        )
        prompt = engine._build_iteration_prompt(3, req)
        assert "prioritize email" in prompt
        assert engine._user_guidance == []  # consumed

    def test_detect_convergence_not_enough_iterations(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        assert not engine._detect_convergence()

    def test_detect_convergence_short_output(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        for i in range(3):
            engine._project.iterations.append(IterationRecord(iteration=i + 1, output="ok"))
        assert engine._detect_convergence()

    def test_detect_convergence_long_output_no_convergence(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        for i in range(3):
            engine._project.iterations.append(IterationRecord(iteration=i + 1, output="x" * 100))
        assert not engine._detect_convergence()

    def test_save_state_no_project(self):
        engine = self._make_engine()
        with pytest.raises(ValueError):
            engine.save_state()

    def test_get_rendered_content(self):
        engine = self._make_engine()
        assert isinstance(engine.get_rendered_content(), str)

    def test_ttadk_startup_model_log_uses_real_or_auto(self, caplog):
        """启动点日志语义：model 字段只能是真实名或 (auto)。"""
        engine = self._make_engine(agent_type="ttadk_codex", model_name="gpt-5.2")

        caplog.set_level(logging.INFO, logger="src.agent_session")

        class _S:
            def __init__(self, *a, **k):
                self.session_id = "sid"
                self.created_at = 0.0
                self.last_active = 0.0
                self.message_count = 0
                self.last_query = ""
                self.is_resumed = False

            def describe_agent(self):
                return "dummy"

            def start(self, startup_timeout: float = 60, **kwargs):
                return "sid"

            def load_session(self, session_id: str):
                return None

            def load_local_history(self, session_id=None, limit: int = 200):
                return []

            def cancel(self):
                return None

            def close(self):
                return None

            def to_snapshot(self):
                return {}

            def get_session_info(self):
                return ""

            def is_server_running(self):
                return True

            def is_server_healthy(self, healthcheck_timeout: float = 2.0):
                return True

            def send_prompt(self, *a, **k):
                return MagicMock(stop_reason="end_turn")

            def send_prompt_with_retry(self, *a, **k):
                return MagicMock(stop_reason="end_turn")

        class _SessSettings:
            acp_startup_timeout = 20
            rate_limit_retry_enabled = False

        with (
            patch("src.agent_session.get_settings", return_value=_SessSettings()),
            patch("src.ttadk.startup_common.precheck_ttadk_startup_model") as mk_precheck,
            patch("src.agent_session.SyncTTADKCLISession", return_value=_S()),
        ):
            # SSOT：create_engine_session 统一走 start_agent_session；单测不应触发真实 ttadk/codex 探测。
            mk_precheck.return_value = {
                "tool": "codex",
                "input_model": "gpt-5.2",
                "model": "gpt-5.2-codex-ttadk",
                "validated": True,
                "source": "probe",
                "decision": "precheck_validated",
                "fail_phase": "",
                "warnings": [],
                "diagnostics": {"attempts": [{"phase": "precheck"}]},
            }
            caplog.clear()
            # 避免触发 LLM 拆解与 review 阶段噪声：直接 stub requirement 与 criteria evaluate
            engine._parse_requirement = lambda txt: LoopRequirement(goal="g", acceptance_criteria=["c"], raw_text=txt)
            engine._evaluate_criteria = lambda *a, **k: {"all_satisfied": True}
            engine._conduct_review = lambda *a, **k: None
            engine.execute("do something")

        text = "\n".join([r.getMessage() for r in caplog.records])
        assert "[SessionFactory] ttadk cli startup:" in text
        m = re.search(r"\bmodel=([^\s]+)", text)
        assert m is not None
        assert m.group(1) == "gpt-5.2-codex-ttadk"
        assert m.group(1) != "gpt-5.2"

    def test_ttadk_resume_model_log_uses_real_or_auto(self, caplog):
        """恢复路径同样要求：model 字段只能是真实名或 (auto)。"""
        engine = self._make_engine(agent_type="ttadk_codex", model_name="gpt-5.2")
        engine._project = LoopProject.create(name="p", root_path="/tmp/test")
        engine._project.status = LoopProjectStatus.PAUSED
        engine._project.requirement = LoopRequirement(goal="g", acceptance_criteria=["c"], raw_text="raw")

        caplog.set_level(logging.INFO, logger="src.agent_session")

        class _S:
            def __init__(self, *a, **k):
                self.session_id = "sid"
                self.created_at = 0.0
                self.last_active = 0.0
                self.message_count = 0
                self.last_query = ""
                self.is_resumed = False

            def describe_agent(self):
                return "dummy"

            def start(self, startup_timeout: float = 60, **kwargs):
                return "sid"

            def load_session(self, session_id: str):
                return None

            def load_local_history(self, session_id=None, limit: int = 200):
                return []

            def cancel(self):
                return None

            def close(self):
                return None

            def to_snapshot(self):
                return {}

            def get_session_info(self):
                return ""

            def is_server_running(self):
                return True

            def is_server_healthy(self, healthcheck_timeout: float = 2.0):
                return True

            def send_prompt(self, *a, **k):
                return MagicMock(stop_reason="end_turn")

        class _SessSettings:
            acp_startup_timeout = 20
            rate_limit_retry_enabled = False

        with (
            patch("src.agent_session.get_settings", return_value=_SessSettings()),
            patch("src.ttadk.startup_common.precheck_ttadk_startup_model") as mk_precheck,
            patch("src.agent_session.SyncTTADKCLISession", return_value=_S()),
        ):
            mk_precheck.return_value = {
                "tool": "codex",
                "input_model": "gpt-5.2",
                "model": None,  # (auto)
                "validated": False,
                "source": "defaults",
                "decision": "precheck_auto",
                "fail_phase": "",
                "warnings": ["no_m_passthrough"],
                "diagnostics": {"attempts": [{"phase": "precheck"}]},
            }
            caplog.clear()
            engine.resume()

        text = "\n".join([r.getMessage() for r in caplog.records])
        assert "[SessionFactory] ttadk cli startup:" in text
        assert "model=(auto)" in text
        assert re.search(r"\bmodel=gpt-5\.2\b", text) is None


class TestLoopEngineManager:
    @patch("src.engine_base.get_settings")
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

    def test_cleanup_all_keeps_running_engine(self):
        mgr = self._make_manager()
        engine = mgr.get_or_create("c1", "/tmp/test")
        engine._run_state = EngineRunState.RUNNING
        mgr.cleanup_all()
        assert mgr.get("c1", "/tmp/test") is engine
        assert engine.run_state == EngineRunState.STOPPING


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
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="in_progress", locations=["/a.py"])
        tracker.process(ACPEvent(event_type=ACPEventType.TOOL_CALL_START, tool_call=tc))
        assert "/a.py" in tracker.modified_files

    def test_process_tool_call_done(self):
        tracker = IterationTracker()
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="completed", locations=["/a.py"])
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
        tracker._text_chunks.append("hello")
        tracker.modified_files.add("/a.py")
        tracker.reset()
        assert tracker.text_buffer == ""
        assert tracker.modified_files == set()


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


# ===========================================================================
# Multi-Perspective Review — Model Tests
# ===========================================================================


class TestReviewPerspective:
    def test_all_perspectives(self):
        assert len(ReviewPerspective) == 5
        assert ReviewPerspective.ARCHITECT.value == "architect"
        assert ReviewPerspective.PRODUCT.value == "product"
        assert ReviewPerspective.USER.value == "user"
        assert ReviewPerspective.TESTER.value == "tester"
        assert ReviewPerspective.DESIGNER.value == "designer"

    def test_display_name(self):
        assert ReviewPerspective.ARCHITECT.display_name == "架构师"
        assert ReviewPerspective.PRODUCT.display_name == "产品经理"
        assert ReviewPerspective.USER.display_name == "用户"
        assert ReviewPerspective.TESTER.display_name == "测试"
        assert ReviewPerspective.DESIGNER.display_name == "设计师"

    def test_emoji(self):
        assert ReviewPerspective.ARCHITECT.emoji == "🏗️"
        assert ReviewPerspective.PRODUCT.emoji == "📦"
        assert ReviewPerspective.USER.emoji == "👤"
        assert ReviewPerspective.TESTER.emoji == "🧪"
        assert ReviewPerspective.DESIGNER.emoji == "🎨"

    def test_review_focus(self):
        for p in ReviewPerspective:
            assert isinstance(p.review_focus, str)
            assert len(p.review_focus) > 0


class TestPerspectiveReview:
    def test_basic_pass(self):
        pr = PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=True)
        assert pr.passed
        assert pr.suggestions == []
        assert pr.summary == ""

    def test_fail_with_suggestions(self):
        pr = PerspectiveReview(
            perspective=ReviewPerspective.TESTER,
            passed=False,
            suggestions=["缺少边界测试", "没有异常处理测试"],
            summary="2条建议",
        )
        assert not pr.passed
        assert len(pr.suggestions) == 2

    def test_to_dict(self):
        pr = PerspectiveReview(
            perspective=ReviewPerspective.PRODUCT,
            passed=False,
            suggestions=["缺少用户引导"],
            summary="1条建议",
        )
        d = pr.to_dict()
        assert d["perspective"] == "product"
        assert d["passed"] is False
        assert d["suggestions"] == ["缺少用户引导"]

    def test_from_dict(self):
        d = {"perspective": "user", "passed": True, "suggestions": [], "summary": "通过"}
        pr = PerspectiveReview.from_dict(d)
        assert pr.perspective == ReviewPerspective.USER
        assert pr.passed

    def test_from_dict_minimal(self):
        d = {"perspective": "architect", "passed": False}
        pr = PerspectiveReview.from_dict(d)
        assert pr.perspective == ReviewPerspective.ARCHITECT
        assert not pr.passed
        assert pr.suggestions == []


class TestReviewResult:
    def _make_all_pass(self) -> ReviewResult:
        return ReviewResult(
            reviews=[PerspectiveReview(perspective=p, passed=True) for p in ReviewPerspective],
            iteration=3,
        )

    def _make_mixed(self) -> ReviewResult:
        return ReviewResult(
            reviews=[
                PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=True),
                PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=False, suggestions=["s1", "s2"]),
                PerspectiveReview(perspective=ReviewPerspective.USER, passed=True),
                PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=False, suggestions=["s3"]),
            ],
            iteration=2,
        )

    def test_all_passed_true(self):
        r = self._make_all_pass()
        assert r.all_passed

    def test_all_passed_false(self):
        r = self._make_mixed()
        assert not r.all_passed

    def test_all_passed_empty(self):
        r = ReviewResult(reviews=[], iteration=1)
        assert not r.all_passed

    def test_total_suggestions(self):
        r = self._make_mixed()
        assert r.total_suggestions == 3

    def test_total_suggestions_all_pass(self):
        r = self._make_all_pass()
        assert r.total_suggestions == 0

    def test_failed_perspectives(self):
        r = self._make_mixed()
        failed = r.failed_perspectives
        assert len(failed) == 2
        assert failed[0].perspective == ReviewPerspective.PRODUCT
        assert failed[1].perspective == ReviewPerspective.TESTER

    def test_suggestions_by_perspective(self):
        r = self._make_mixed()
        by_p = r.suggestions_by_perspective()
        assert ReviewPerspective.PRODUCT in by_p
        assert ReviewPerspective.TESTER in by_p
        assert ReviewPerspective.ARCHITECT not in by_p

    def test_to_dict_and_from_dict_roundtrip(self):
        r = self._make_mixed()
        d = r.to_dict()
        restored = ReviewResult.from_dict(d)
        assert restored.iteration == 2
        assert len(restored.reviews) == 4
        assert restored.total_suggestions == 3
        assert not restored.all_passed

    def test_from_dict_empty(self):
        r = ReviewResult.from_dict({})
        assert r.reviews == []
        assert r.iteration == 0


class TestIterationRecordReview:
    def test_review_result_field_default_none(self):
        record = IterationRecord(iteration=1)
        assert record.review_result is None

    def test_review_result_in_to_dict(self):
        review = ReviewResult(
            reviews=[PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=True)],
            iteration=1,
        )
        record = IterationRecord(iteration=1, review_result=review)
        d = record.to_dict()
        assert d["review_result"] is not None
        assert d["review_result"]["iteration"] == 1

    def test_review_result_none_in_to_dict(self):
        record = IterationRecord(iteration=1)
        d = record.to_dict()
        assert d["review_result"] is None

    def test_review_result_from_dict(self):
        d = {
            "iteration": 1,
            "review_result": {
                "reviews": [{"perspective": "tester", "passed": False, "suggestions": ["add tests"]}],
                "iteration": 1,
            },
        }
        record = IterationRecord.from_dict(d)
        assert record.review_result is not None
        assert record.review_result.total_suggestions == 1

    def test_review_result_from_dict_no_review(self):
        d = {"iteration": 2}
        record = IterationRecord.from_dict(d)
        assert record.review_result is None


# ===========================================================================
# Multi-Perspective Review — Engine Tests
# ===========================================================================


class TestReviewParsing:
    @patch("src.engine_base.get_settings")
    def _make_engine(self, mock_settings):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_enabled = True
        s.loop_review_extra_iterations = 3
        mock_settings.return_value = s
        return LoopEngine(chat_id="c1", root_path="/tmp/test")

    def test_parse_all_pass(self):
        engine = self._make_engine()
        text = """[ARCHITECT]
PASS

[PRODUCT]
PASS

[USER]
PASS

[TESTER]
PASS

[DESIGNER]
PASS
"""
        result = engine._parse_review_output(text, 1)
        assert result.all_passed
        assert result.iteration == 1
        assert len(result.reviews) == 5
        assert result.total_suggestions == 0

    def test_parse_mixed(self):
        engine = self._make_engine()
        text = """[ARCHITECT]
FAIL
- 缺少抽象层
- 耦合度过高

[PRODUCT]
PASS

[USER]
FAIL
- 错误提示不够友好

[TESTER]
PASS
"""
        result = engine._parse_review_output(text, 2)
        assert not result.all_passed
        assert result.total_suggestions == 3
        assert len(result.failed_perspectives) == 2

        arch = [r for r in result.reviews if r.perspective == ReviewPerspective.ARCHITECT][0]
        assert not arch.passed
        assert len(arch.suggestions) == 2
        assert "缺少抽象层" in arch.suggestions

        user = [r for r in result.reviews if r.perspective == ReviewPerspective.USER][0]
        assert not user.passed
        assert len(user.suggestions) == 1

    def test_parse_all_fail(self):
        engine = self._make_engine()
        text = """[ARCHITECT]
FAIL
- issue1

[PRODUCT]
FAIL
- issue2

[USER]
FAIL
- issue3

[TESTER]
FAIL
- issue4

[DESIGNER]
FAIL
- issue5
"""
        result = engine._parse_review_output(text, 3)
        assert not result.all_passed
        assert result.total_suggestions == 5
        assert len(result.failed_perspectives) == 5

    def test_parse_empty_output_fallback(self):
        engine = self._make_engine()
        result = engine._parse_review_output("", 1)
        # Should return a "failed" result for safety
        assert not result.all_passed
        assert len(result.reviews) == 5
        for r in result.reviews:
            assert not r.passed

    def test_parse_garbage_output_fallback(self):
        engine = self._make_engine()
        result = engine._parse_review_output("random garbage text\nno structure at all", 1)
        assert not result.all_passed
        assert len(result.reviews) == 5

    def test_parse_partial_perspectives(self):
        engine = self._make_engine()
        # Only ARCHITECT and TESTER present
        text = """[ARCHITECT]
PASS

[TESTER]
FAIL
- missing edge case tests
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 2
        assert not result.all_passed  # TESTER failed

    def test_parse_with_extra_whitespace(self):
        engine = self._make_engine()
        text = """  [ARCHITECT]
  PASS

  [PRODUCT]
  FAIL
  - suggestion with spaces

  [USER]
  PASS

  [TESTER]
  PASS
"""
        # The regex should still work with leading spaces on suggestion lines
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) >= 1  # At least some parsed

    def test_parse_with_star_bullet(self):
        engine = self._make_engine()
        text = """[ARCHITECT]
FAIL
* use dependency injection
* reduce coupling

[PRODUCT]
PASS

[USER]
PASS

[TESTER]
PASS
"""
        result = engine._parse_review_output(text, 1)
        arch = [r for r in result.reviews if r.perspective == ReviewPerspective.ARCHITECT][0]
        assert len(arch.suggestions) == 2

    def test_parse_same_line_verdict(self):
        engine = self._make_engine()
        text = """[ARCHITECT] FAIL
- a1

[PRODUCT] PASS

[USER] FAIL
- u1

[TESTER] PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed
        assert result.total_suggestions == 2

    def test_parse_chinese_headings(self):
        engine = self._make_engine()
        text = """🏗️ 架构师: FAIL
- 需要解耦

📦 产品经理：PASS

👤 用户: FAIL
- 文案不清晰

🧪 测试: PASS
"""
        result = engine._parse_review_output(text, 2)
        assert len(result.reviews) == 4
        assert not result.all_passed
        assert result.total_suggestions == 2

    def test_parse_duplicate_perspective_ignored(self):
        engine = self._make_engine()
        text = """[ARCHITECT]
PASS

[ARCHITECT]
FAIL
- something

[PRODUCT]
PASS

[USER]
PASS

[TESTER]
PASS
"""
        result = engine._parse_review_output(text, 1)
        arch_reviews = [r for r in result.reviews if r.perspective == ReviewPerspective.ARCHITECT]
        assert len(arch_reviews) == 1
        assert arch_reviews[0].passed  # First one wins

    def test_parse_bold_brackets(self):
        """LLM wraps [TAG] in bold: **[ARCHITECT]**"""
        engine = self._make_engine()
        text = """**[ARCHITECT]**: PASS

**[PRODUCT]**: FAIL
- 需求覆盖不足
- 缺少边界处理

**[USER]**: PASS

**[TESTER]**: FAIL
- 缺少单元测试
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed
        assert result.total_suggestions == 3

    def test_parse_bold_no_brackets(self):
        """LLM uses bold tags without brackets: **ARCHITECT**"""
        engine = self._make_engine()
        text = """**ARCHITECT**: PASS

**PRODUCT**: FAIL
- 边界场景未覆盖

**USER**: PASS

**TESTER**: PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed
        prod = [r for r in result.reviews if r.perspective == ReviewPerspective.PRODUCT][0]
        assert not prod.passed
        assert len(prod.suggestions) == 1

    def test_parse_markdown_heading_no_brackets(self):
        """LLM uses markdown headings without brackets: ### ARCHITECT"""
        engine = self._make_engine()
        text = """### ARCHITECT: PASS

### PRODUCT: FAIL
- 功能不完整

### USER: PASS

### TESTER: PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed

    def test_parse_plain_tag_with_colon(self):
        """LLM uses plain tag with colon: ARCHITECT: PASS"""
        engine = self._make_engine()
        text = """ARCHITECT: PASS

PRODUCT: FAIL
- 遗漏需求点

USER: PASS

TESTER: PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed

    def test_parse_bold_chinese_headings(self):
        """LLM uses bold Chinese names: **架构师**: PASS"""
        engine = self._make_engine()
        text = """**架构师**: PASS

**产品经理**: FAIL
- 边界场景处理不足

**用户**: PASS

**测试**: PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed

    def test_parse_chinese_with_suffix(self):
        """LLM appends 审查/评审 to Chinese names."""
        engine = self._make_engine()
        text = """架构师审查: PASS

产品经理评审: FAIL
- 需要补充文档

用户视角: PASS

测试: PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed

    def test_parse_numbered_chinese(self):
        """LLM uses numbered list with Chinese: 1. 架构师: PASS"""
        engine = self._make_engine()
        text = """1. 架构师: PASS
2. 产品经理: FAIL
- 功能缺失
3. 用户: PASS
4. 测试: PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed

    def test_parse_markdown_heading_bold_chinese(self):
        """LLM uses ### **架构师** format."""
        engine = self._make_engine()
        text = """### **架构师**
PASS

### **产品经理**
FAIL
- 缺少异常场景

### **用户**
PASS

### **测试**
PASS
"""
        result = engine._parse_review_output(text, 1)
        assert len(result.reviews) == 4
        assert not result.all_passed

    @patch("src.engine_base.get_settings")
    def test_parse_llm_fallback_json(self, mock_settings):
        """When regex fails, LLM fallback extracts from JSON."""
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_enabled = True
        s.loop_review_extra_iterations = 3
        s.ark_api_key = "test-key"
        s.ark_model = "test-model"
        s.ark_base_url = "https://test.example.com"
        mock_settings.return_value = s
        engine = LoopEngine(chat_id="c1", root_path="/tmp/test")

        # Simulate LLM response
        llm_json = """[
  {"perspective": "ARCHITECT", "verdict": "PASS", "suggestions": []},
  {"perspective": "PRODUCT", "verdict": "FAIL", "suggestions": ["需要补充边界测试"]},
  {"perspective": "USER", "verdict": "PASS", "suggestions": []},
  {"perspective": "TESTER", "verdict": "FAIL", "suggestions": ["缺少集成测试", "覆盖率不足"]}
]"""
        with patch.object(
            engine, "_parse_review_with_llm", return_value=engine._extract_reviews_from_llm_response(llm_json)
        ):
            result = engine._parse_review_output("totally unstructured free text", 5)
        assert len(result.reviews) == 4
        assert not result.all_passed
        assert result.total_suggestions == 3

    def test_extract_reviews_from_llm_response_valid(self):
        """Test _extract_reviews_from_llm_response with valid JSON."""
        engine = self._make_engine()
        text = """```json
[
  {"perspective": "ARCHITECT", "verdict": "PASS", "suggestions": []},
  {"perspective": "PRODUCT", "verdict": "FAIL", "suggestions": ["issue1"]},
  {"perspective": "USER", "verdict": "PASS", "suggestions": []},
  {"perspective": "TESTER", "verdict": "PASS", "suggestions": []}
]
```"""
        reviews = engine._extract_reviews_from_llm_response(text)
        assert len(reviews) == 4
        prod = [r for r in reviews if r.perspective == ReviewPerspective.PRODUCT][0]
        assert not prod.passed
        assert prod.suggestions == ["issue1"]

    def test_extract_reviews_from_llm_response_invalid(self):
        """Test _extract_reviews_from_llm_response with invalid JSON."""
        engine = self._make_engine()
        assert engine._extract_reviews_from_llm_response("not json at all") == []
        assert engine._extract_reviews_from_llm_response("") == []
        assert engine._extract_reviews_from_llm_response("{}") == []

    def test_extract_reviews_pass_clears_suggestions(self):
        """PASS verdict should always have empty suggestions."""
        engine = self._make_engine()
        text = """[
  {"perspective": "ARCHITECT", "verdict": "PASS", "suggestions": ["this should be cleared"]},
  {"perspective": "PRODUCT", "verdict": "PASS", "suggestions": []},
  {"perspective": "USER", "verdict": "PASS", "suggestions": []},
  {"perspective": "TESTER", "verdict": "PASS", "suggestions": []}
]"""
        reviews = engine._extract_reviews_from_llm_response(text)
        assert len(reviews) == 4
        arch = [r for r in reviews if r.perspective == ReviewPerspective.ARCHITECT][0]
        assert arch.passed
        assert arch.suggestions == []


class TestBuildReviewPrompt:
    @patch("src.engine_base.get_settings")
    def _make_engine(self, mock_settings):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_enabled = True
        s.loop_review_extra_iterations = 3
        mock_settings.return_value = s
        return LoopEngine(chat_id="c1", root_path="/tmp/test")

    def test_build_review_prompt_contains_perspectives(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="implement login",
                acceptance_criteria=["email", "phone"],
                raw_text="test",
            )
        )
        prompt = engine._build_review_prompt()
        assert "[ARCHITECT]" in prompt
        assert "[PRODUCT]" in prompt
        assert "[USER]" in prompt
        assert "[TESTER]" in prompt
        assert "PASS" in prompt
        assert "FAIL" in prompt

    def test_build_review_prompt_includes_goal(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="build authentication system",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )
        prompt = engine._build_review_prompt()
        assert "build authentication system" in prompt


class TestIterationPromptWithReview:
    @patch("src.engine_base.get_settings")
    def _make_engine(self, mock_settings):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_enabled = True
        s.loop_review_extra_iterations = 3
        mock_settings.return_value = s
        return LoopEngine(chat_id="c1", root_path="/tmp/test")

    def test_iteration_prompt_includes_review_feedback(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._last_review = ReviewResult(
            reviews=[
                PerspectiveReview(
                    perspective=ReviewPerspective.ARCHITECT, passed=False, suggestions=["use DI pattern"]
                ),
                PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=True),
                PerspectiveReview(perspective=ReviewPerspective.USER, passed=True),
                PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=False, suggestions=["add unit tests"]),
            ],
            iteration=1,
        )
        req = LoopRequirement(goal="login", acceptance_criteria=["c1"], raw_text="test")
        prompt = engine._build_iteration_prompt(2, req)
        assert "上轮审查反馈" in prompt
        assert "use DI pattern" in prompt
        assert "add unit tests" in prompt
        assert "架构师" in prompt
        assert "测试" in prompt

    def test_iteration_prompt_no_review_feedback_when_all_pass(self):
        engine = self._make_engine()
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._last_review = ReviewResult(
            reviews=[PerspectiveReview(perspective=p, passed=True) for p in ReviewPerspective],
            iteration=1,
        )
        req = LoopRequirement(goal="login", acceptance_criteria=["c1"], raw_text="test")
        prompt = engine._build_iteration_prompt(2, req)
        assert "上轮审查反馈" not in prompt

    def test_iteration_prompt_no_review_when_none(self):
        engine = self._make_engine()
        engine._last_review = None
        req = LoopRequirement(goal="login", acceptance_criteria=["c1"], raw_text="test")
        prompt = engine._build_iteration_prompt(2, req)
        assert "上轮审查反馈" not in prompt


class TestInitialPromptReviewNote:
    @patch("src.engine_base.get_settings")
    def _make_engine(self, mock_settings, review_enabled=True):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_enabled = review_enabled
        s.loop_review_extra_iterations = 3
        mock_settings.return_value = s
        return LoopEngine(chat_id="c1", root_path="/tmp/test")

    def test_initial_prompt_has_review_note_when_enabled(self):
        engine = self._make_engine(review_enabled=True)
        req = LoopRequirement(goal="test", acceptance_criteria=["c1"], raw_text="test")
        prompt = engine._build_initial_prompt(req)
        assert "审查机制" in prompt
        assert "架构师" in prompt

    def test_initial_prompt_no_review_note_when_disabled(self):
        engine = self._make_engine(review_enabled=False)
        req = LoopRequirement(goal="test", acceptance_criteria=["c1"], raw_text="test")
        prompt = engine._build_initial_prompt(req)
        assert "审查机制" not in prompt


class TestConductReview:
    @patch("src.engine_base.get_settings")
    def _make_engine(self, mock_settings):
        s = MagicMock()
        s.loop_max_iterations = 15
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_enabled = True
        s.loop_review_extra_iterations = 3
        mock_settings.return_value = s
        return LoopEngine(chat_id="c1", root_path="/tmp/test")

    def test_conduct_review_no_session(self):
        engine = self._make_engine()
        engine._session = None
        engine._project = LoopProject.create(name="test", root_path="/tmp")
        engine._project.set_requirement(
            LoopRequirement(
                goal="test",
                acceptance_criteria=["c1"],
                raw_text="test",
            )
        )
        callbacks = LoopEngineCallbacks()
        result = engine._conduct_review(1, callbacks)
        assert result.reviews == []

    def test_conduct_review_calls_session(self):
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
        review_output = """[ARCHITECT]
PASS

[PRODUCT]
PASS

[USER]
PASS

[TESTER]
PASS
"""

        def mock_send(prompt, on_event=None, timeout=None, retry_policy=None, before_retry=None):
            if on_event:
                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=review_output))
            return MagicMock(stop_reason="end_turn")

        mock_session.send_prompt_with_retry = mock_send
        engine._session = mock_session

        callback_called = []
        callbacks = LoopEngineCallbacks(
            on_review_done=lambda it, r: callback_called.append((it, r)),
        )
        result = engine._conduct_review(1, callbacks)
        assert result.all_passed
        assert len(callback_called) == 1
        assert callback_called[0][0] == 1

    def test_conduct_review_session_exception(self):
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
        mock_session.send_prompt_with_retry.side_effect = RuntimeError("timeout")
        engine._session = mock_session

        callbacks = LoopEngineCallbacks()
        result = engine._conduct_review(1, callbacks)
        assert not result.all_passed  # Exception → treated as having suggestions


# ===========================================================================
# Multi-Perspective Review — Reporter Tests
# ===========================================================================


class TestReviewReporter:
    def test_format_review_result_all_pass(self):
        reporter = LoopReporter()
        review = ReviewResult(
            reviews=[PerspectiveReview(perspective=p, passed=True) for p in ReviewPerspective],
            iteration=3,
        )
        result = reporter.format_review_result(review)
        assert "多视角审查" in result
        assert "第3轮" in result
        assert "所有视角均通过" in result
        # All should show PASS
        assert result.count("✅ PASS") == 5

    def test_format_review_result_mixed(self):
        reporter = LoopReporter()
        review = ReviewResult(
            reviews=[
                PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=True),
                PerspectiveReview(perspective=ReviewPerspective.PRODUCT, passed=False, suggestions=["建议1", "建议2"]),
                PerspectiveReview(perspective=ReviewPerspective.USER, passed=True),
                PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=False, suggestions=["建议3"]),
            ],
            iteration=2,
        )
        result = reporter.format_review_result(review)
        assert "改进建议: 3 条" in result
        assert "建议1" in result
        assert "建议2" in result
        assert "建议3" in result
        assert "有建议" in result

    def test_format_review_result_all_fail(self):
        reporter = LoopReporter()
        review = ReviewResult(
            reviews=[PerspectiveReview(perspective=p, passed=False, suggestions=["issue"]) for p in ReviewPerspective],
            iteration=1,
        )
        result = reporter.format_review_result(review)
        assert "改进建议: 5 条" in result

    def test_get_review_title_passed(self):
        reporter = LoopReporter()
        title = reporter.get_review_title(3, all_passed=True)
        assert "审查通过" in title
        assert "第3轮" in title

    def test_get_review_title_not_passed(self):
        reporter = LoopReporter()
        title = reporter.get_review_title(2, all_passed=False)
        assert "多视角审查" in title
        assert "第2轮" in title
