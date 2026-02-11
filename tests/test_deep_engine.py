"""Tests for deep_engine — ACP-driven DeepEngine."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from src.deep_engine.models import EngineRunState, DeepProject, DeepProjectStatus
from src.deep_engine.engine import DeepEngine, DeepEngineManager, DeepEngineCallbacks
from src.deep_engine.progress import DeepProgress
from src.acp.models import PlanEntryInfo, PlanInfo, ToolCallInfo


class TestDeepEngine:
    @patch("src.deep_engine.engine.get_settings")
    def _make_engine(self, mock_settings, **kwargs):
        s = MagicMock()
        s.coco_execution_timeout = 300
        s.claude_execution_timeout = 600
        mock_settings.return_value = s
        return DeepEngine(chat_id="c1", root_path="/tmp/test", **kwargs)

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
        engine._project.pause.assert_called_once()
        assert engine.run_state == EngineRunState.STOPPING

    def test_cleanup(self):
        engine = self._make_engine()
        engine._session = MagicMock()
        engine._project = MagicMock()
        engine.cleanup()
        assert engine._session is None
        assert engine._project is None
        assert engine.run_state == EngineRunState.IDLE

    def test_build_deep_prompt(self):
        engine = self._make_engine()
        prompt = engine._build_deep_prompt("add login feature")
        assert "add login feature" in prompt
        assert "/tmp/test" in prompt

    def test_get_rendered_content(self):
        engine = self._make_engine()
        content = engine.get_rendered_content()
        assert isinstance(content, str)

    def test_save_state_no_project(self):
        engine = self._make_engine()
        with pytest.raises(ValueError):
            engine.save_state()

    def test_inject_context(self):
        engine = self._make_engine()
        engine._run_state = EngineRunState.RUNNING
        engine._session = MagicMock()
        engine.inject_context("test context")

    def test_get_progress_no_project(self):
        engine = self._make_engine()
        assert engine.get_progress() is None

    def test_get_task_summary_no_project(self):
        engine = self._make_engine()
        assert engine.get_task_summary() == "暂无任务"


class TestDeepEngineManager:
    def test_get_or_create(self):
        with patch("src.deep_engine.engine.get_settings") as mock:
            mock.return_value = MagicMock(coco_execution_timeout=300, claude_execution_timeout=600)
            mgr = DeepEngineManager()
            engine = mgr.get_or_create("c1", "/tmp/test")
            assert engine is not None
            engine2 = mgr.get_or_create("c1", "/tmp/test")
            assert engine is engine2

    def test_get_returns_none_when_missing(self):
        mgr = DeepEngineManager()
        assert mgr.get("nonexistent", "/tmp") is None

    def test_get_active_engine(self):
        with patch("src.deep_engine.engine.get_settings") as mock:
            mock.return_value = MagicMock(coco_execution_timeout=300, claude_execution_timeout=600)
            mgr = DeepEngineManager()
            engine = mgr.get_or_create("c1", "/tmp/test")
            assert mgr.get_active_engine("c1") is None
            engine._run_state = EngineRunState.RUNNING
            assert mgr.get_active_engine("c1") is engine

    def test_engine_name_switch(self):
        with patch("src.deep_engine.engine.get_settings") as mock:
            mock.return_value = MagicMock(coco_execution_timeout=300, claude_execution_timeout=600)
            mgr = DeepEngineManager()
            e1 = mgr.get_or_create("c1", "/tmp/test", engine_name="Coco")
            assert e1.engine_name == "Coco"
            e2 = mgr.get_or_create("c1", "/tmp/test", engine_name="Claude")
            assert e2.engine_name == "Claude"
            assert e1 is not e2

    def test_cleanup_all(self):
        with patch("src.deep_engine.engine.get_settings") as mock:
            mock.return_value = MagicMock(coco_execution_timeout=300, claude_execution_timeout=600)
            mgr = DeepEngineManager()
            mgr.get_or_create("c1", "/tmp/test")
            mgr.get_or_create("c2", "/tmp/test2")
            mgr.cleanup_all()
            assert mgr.get("c1", "/tmp/test") is None


class TestDeepProgress:
    def test_initial_state(self):
        p = DeepProgress()
        assert p.completed_steps == 0
        assert p.total_steps == 0
        assert p.progress_percent == 0
        assert p.tool_calls == []
        assert p.modified_files == set()

    def test_update_plan(self):
        p = DeepProgress()
        plan = PlanInfo(entries=[
            PlanEntryInfo(content="s1", status="completed"),
            PlanEntryInfo(content="s2", status="in_progress"),
            PlanEntryInfo(content="s3", status="pending"),
        ])
        p.update_plan(plan)
        assert p.total_steps == 3
        assert p.completed_steps == 1

    def test_record_tool(self):
        p = DeepProgress()
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="completed",
                          locations=["/a.py"])
        p.record_tool(tc)
        assert len(p.tool_calls) == 1
        assert "/a.py" in p.modified_files

    def test_append_text(self):
        p = DeepProgress()
        p.append_text("hello ")
        p.append_text("world")
        assert p.text_buffer == "hello world"

    def test_progress_bar(self):
        p = DeepProgress()
        plan = PlanInfo(entries=[
            PlanEntryInfo(content="s1", status="completed"),
            PlanEntryInfo(content="s2", status="completed"),
            PlanEntryInfo(content="s3", status="pending"),
            PlanEntryInfo(content="s4", status="pending"),
        ])
        p.update_plan(plan)
        bar = p.progress_bar
        assert "50%" in bar
