"""Tests for DeepEngineCallbacks and src.deep_engine.metrics modules."""
from __future__ import annotations

from src.deep_engine.metrics import DeepEngineMetrics


class TestDeepEngineMetrics:
    """DeepEngineMetrics unit tests."""

    def test_initial_state(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        assert m.tool_calls_total == 0
        assert m.text_chunks_total == 0
        assert m.plan_updates_total == 0
        assert m.status == "unknown"

    def test_record_tool_call(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.record_tool_call("bash")
        m.record_tool_call("bash")
        m.record_tool_call("file_write")
        assert m.tool_calls_total == 3
        assert m.tool_calls_by_kind == {"bash": 2, "file_write": 1}

    def test_record_tool_call_none_kind(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.record_tool_call(None)
        assert m.tool_calls_by_kind == {"unknown": 1}

    def test_record_text_chunk(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.record_text_chunk()
        m.record_text_chunk()
        assert m.text_chunks_total == 2

    def test_record_plan_update(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.record_plan_update()
        assert m.plan_updates_total == 1

    def test_duration_before_finish(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        assert m.duration >= 0

    def test_finish_sets_end_time_and_status(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.finish("success")
        assert m.end_time is not None
        assert m.status == "success"
        assert m.error_type is None

    def test_finish_with_error(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.finish("error", error_type="TimeoutError")
        assert m.status == "error"
        assert m.error_type == "TimeoutError"

    def test_to_dict(self):
        m = DeepEngineMetrics(trace_id="t1", project_id="p1")
        m.record_tool_call("bash")
        m.finish("success")
        d = m.to_dict()
        assert d["trace_id"] == "t1"
        assert d["project_id"] == "p1"
        assert d["tool_calls_total"] == 1
        assert d["status"] == "success"
        assert "duration" in d
        assert "timestamp" in d


class TestDeepEngineCallbacks:
    """DeepEngineCallbacks basic tests."""

    def test_can_instantiate(self):
        # DeepEngineCallbacks inherits from EngineCallbacks; verify import works
        from src.deep_engine.engine import DeepEngineCallbacks
        cb = DeepEngineCallbacks()
        # Planning aliases should point to analyzing callbacks
        assert cb.on_planning_start is cb.on_analyzing_start
        assert cb.on_planning_done is cb.on_analyzing_done

    def test_planning_setter_delegates(self):
        from src.deep_engine.engine import DeepEngineCallbacks
        cb = DeepEngineCallbacks()
        def sentinel():
            return None
        cb.on_planning_start = sentinel
        assert cb.on_analyzing_start is sentinel
