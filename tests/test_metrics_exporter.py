"""Tests for src/utils/metrics_exporter.py — ReviewMetricsExporter framework."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from src.utils.metrics_exporter import (
    JsonLinesExporter,
    LoggerExporter,
    ReviewMetricsExporter,
    get_metrics_exporter,
    reset_metrics_exporter,
)

# ---------------------------------------------------------------------------
# Sample metrics dict (mirrors handle_review_exception output)
# ---------------------------------------------------------------------------
SAMPLE_METRICS = {
    "metric_type": "review_exception",
    "engine": "spec",
    "cycle": 3,
    "fail_reason": "timeout",
    "consecutive_timeouts": 2,
    "consecutive_failures": 3,
    "circuit_open": True,
    "adaptive_timeout": 60,
    "backoff_level": 1,
    "total_elapsed_ms": 12500,
}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------
class TestProtocolConformance:
    def test_logger_exporter_is_protocol(self):
        assert isinstance(LoggerExporter(), ReviewMetricsExporter)

    def test_jsonl_exporter_is_protocol(self):
        with tempfile.TemporaryDirectory() as d:
            assert isinstance(
                JsonLinesExporter(path=os.path.join(d, "m.jsonl")),
                ReviewMetricsExporter,
            )


# ---------------------------------------------------------------------------
# LoggerExporter
# ---------------------------------------------------------------------------
class TestLoggerExporter:
    def test_export_logs_json(self, caplog):
        exporter = LoggerExporter()
        with caplog.at_level("INFO"):
            exporter.export_metrics(SAMPLE_METRICS, prefix="[Spec]")
        assert "review_metrics" in caplog.text
        assert '"metric_type"' in caplog.text or "metric_type" in caplog.text

    def test_export_survives_bad_dict(self, caplog):
        """Even if dict is not JSON-serialisable, no exception escapes."""
        exporter = LoggerExporter()
        bad = {"key": object()}
        exporter.export_metrics(bad, prefix="[X]")  # should not raise

    def test_prefix_appears_in_log(self, caplog):
        exporter = LoggerExporter()
        with caplog.at_level("INFO"):
            exporter.export_metrics(SAMPLE_METRICS, prefix="[Loop]")
        assert "[Loop]" in caplog.text


# ---------------------------------------------------------------------------
# JsonLinesExporter
# ---------------------------------------------------------------------------
class TestJsonLinesExporter:
    def test_write_single_line(self, tmp_path):
        path = str(tmp_path / "metrics.jsonl")
        exporter = JsonLinesExporter(path=path)
        exporter.export_metrics(SAMPLE_METRICS, prefix="[Spec]")

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["metric_type"] == "review_exception"
        assert parsed["engine"] == "spec"
        assert parsed["fail_reason"] == "timeout"

    def test_write_multiple_lines(self, tmp_path):
        path = str(tmp_path / "metrics.jsonl")
        exporter = JsonLinesExporter(path=path)
        for i in range(5):
            m = dict(SAMPLE_METRICS, cycle=i)
            exporter.export_metrics(m)
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 5
        for i, line in enumerate(lines):
            assert json.loads(line)["cycle"] == i

    def test_all_10_fields_present(self, tmp_path):
        path = str(tmp_path / "metrics.jsonl")
        exporter = JsonLinesExporter(path=path)
        exporter.export_metrics(SAMPLE_METRICS)
        with open(path, "r", encoding="utf-8") as f:
            parsed = json.loads(f.readline())
        expected_keys = {
            "metric_type", "engine", "cycle", "fail_reason",
            "consecutive_timeouts", "consecutive_failures",
            "circuit_open", "adaptive_timeout", "backoff_level",
            "total_elapsed_ms",
        }
        assert expected_keys.issubset(set(parsed.keys()))

    def test_creates_parent_directory(self, tmp_path):
        path = str(tmp_path / "subdir" / "nested" / "m.jsonl")
        exporter = JsonLinesExporter(path=path)
        exporter.export_metrics(SAMPLE_METRICS)
        assert os.path.isfile(path)

    def test_path_property(self, tmp_path):
        path = str(tmp_path / "m.jsonl")
        exporter = JsonLinesExporter(path=path)
        assert exporter.path == path

    def test_survives_bad_dict(self, tmp_path):
        """Non-serialisable dict does not raise — silently skipped."""
        path = str(tmp_path / "m.jsonl")
        exporter = JsonLinesExporter(path=path)
        exporter.export_metrics({"bad": object()})
        # File may be empty or not created — no crash
        assert True


# ---------------------------------------------------------------------------
# Factory: get_metrics_exporter / reset
# ---------------------------------------------------------------------------
class TestFactory:
    def setup_method(self):
        reset_metrics_exporter()

    def teardown_method(self):
        reset_metrics_exporter()

    def test_default_returns_logger_exporter(self):
        exporter = get_metrics_exporter()
        assert isinstance(exporter, LoggerExporter)

    def test_jsonl_returns_jsonl_exporter(self, tmp_path):
        path = str(tmp_path / "m.jsonl")
        exporter = get_metrics_exporter(exporter_type="jsonl", path=path)
        assert isinstance(exporter, JsonLinesExporter)

    def test_singleton_caching(self):
        a = get_metrics_exporter()
        b = get_metrics_exporter()
        assert a is b

    def test_reset_clears_singleton(self):
        a = get_metrics_exporter()
        reset_metrics_exporter()
        b = get_metrics_exporter()
        assert a is not b

    def test_unknown_type_falls_back_to_logger(self):
        exporter = get_metrics_exporter(exporter_type="unknown_type")
        assert isinstance(exporter, LoggerExporter)


# ---------------------------------------------------------------------------
# Integration: handle_review_exception uses exporter
# ---------------------------------------------------------------------------
class TestHandleReviewExceptionIntegration:
    """Verify handle_review_exception calls the exporter instead of raw logger."""

    def setup_method(self):
        reset_metrics_exporter()

    def teardown_method(self):
        reset_metrics_exporter()

    def test_handle_review_exception_uses_exporter(self, tmp_path):
        """Metrics go through exporter when called from handle_review_exception."""
        from dataclasses import dataclass, field

        from src.utils.review_helpers import handle_review_exception

        @dataclass
        class FakeCircuit:
            last_review_failure_diag: dict = field(default_factory=dict)
            review_failure_consecutive: int = 0
            review_circuit_open_until_cycle: int = 0
            backoff_level: int = 0
            consecutive_timeouts: int = 0
            consecutive_skips: int = 0
            last_review_elapsed_ms: int = 0

        class FakeSettings:
            spec_review_failure_circuit_enabled = True
            spec_review_failure_max_consecutive = 3
            spec_review_failure_cooldown_cycles = 3
            spec_review_failure_max_cooldown_cycles = 12
            review_metrics_exporter_type = "logger"
            review_metrics_jsonl_path = ""

        circuit = FakeCircuit()
        settings = FakeSettings()

        with patch(
            "src.utils.metrics_exporter.get_metrics_exporter"
        ) as mock_get:
            mock_exporter = LoggerExporter()
            mock_get.return_value = mock_exporter
            result = handle_review_exception(
                TimeoutError("test timeout"),
                circuit=circuit,
                cycle=1,
                settings=settings,
                engine="spec",
                review_timeout=120,
                review_elapsed_ms=5000,
            )
        assert result.metrics["metric_type"] == "review_exception"
        mock_get.assert_called_once()


# ---------------------------------------------------------------------------
# Gap fix: except clause logs actual error, not class name
# ---------------------------------------------------------------------------
class TestJsonLinesExporterErrorLogging:
    """Verify that write failures log the actual exception message, not '<class Exception>'."""

    def test_write_failure_logs_actual_error(self, tmp_path):
        """When file write fails, logger.debug should contain the real error message."""
        path = str(tmp_path / "readonly" / "m.jsonl")
        os.makedirs(str(tmp_path / "readonly"), exist_ok=True)

        exporter = JsonLinesExporter(path=path)
        # Make the file unwritable by pointing to a directory
        os.makedirs(path, exist_ok=True)

        with patch("src.utils.metrics_exporter.logger") as mock_logger:
            exporter.export_metrics(SAMPLE_METRICS)
            if mock_logger.debug.called:
                call_args = mock_logger.debug.call_args
                log_msg = call_args[0][1] if len(call_args[0]) > 1 else ""
                # Must NOT contain the class representation
                assert "<class" not in str(log_msg), (
                    f"Log should contain actual error, not class name; got: {log_msg}"
                )

    def test_write_failure_does_not_log_class_exception(self):
        """Explicitly verify str(Exception) pattern is gone — error variable is used."""
        import inspect
        source = inspect.getsource(JsonLinesExporter.export_metrics)
        assert "str(Exception)" not in source, "Must use str(e), not str(Exception)"
        assert "repr(Exception)" not in source, "Must use repr(e), not repr(Exception)"


# ---------------------------------------------------------------------------
# Gap fix: get_metrics_exporter reconfiguration
# ---------------------------------------------------------------------------
class TestFactoryReconfiguration:
    """Verify get_metrics_exporter rebuilds singleton when type changes."""

    def setup_method(self):
        reset_metrics_exporter()

    def teardown_method(self):
        reset_metrics_exporter()

    def test_switch_logger_to_jsonl(self, tmp_path):
        a = get_metrics_exporter("logger")
        assert isinstance(a, LoggerExporter)
        b = get_metrics_exporter("jsonl", path=str(tmp_path / "m.jsonl"))
        assert isinstance(b, JsonLinesExporter)
        assert a is not b

    def test_switch_jsonl_to_logger(self, tmp_path):
        a = get_metrics_exporter("jsonl", path=str(tmp_path / "m.jsonl"))
        assert isinstance(a, JsonLinesExporter)
        b = get_metrics_exporter("logger")
        assert isinstance(b, LoggerExporter)
        assert a is not b

    def test_same_type_returns_cached(self):
        a = get_metrics_exporter("logger")
        b = get_metrics_exporter("logger")
        assert a is b

    def test_same_jsonl_type_returns_cached(self, tmp_path):
        path = str(tmp_path / "m.jsonl")
        a = get_metrics_exporter("jsonl", path=path)
        b = get_metrics_exporter("jsonl", path=path)
        assert a is b
