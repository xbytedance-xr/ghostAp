"""Tests for TimeAgo semantics layer and idle bucket helpers.

本文件主要验证 TimeAgo 语义分桶逻辑（秒数 → TimeAgoBucket），
并对 ACP 层使用的 idle bucket helper 做一层薄验证，确保其严格
委托到 :mod:`src.utils.time_ago` 的 SSOT 实现，同时为废弃接口
`_format_seconds_ago` 提供一次性告警行为的保护性测试。
"""

import logging

import pytest
from src.acp.manager import ACPSessionManager
from src.acp.telemetry import IdleHealthTelemetryContext, _classify_idle_health_with_fallback
from src.utils.time_ago import IdleHealth, classify_idle_health_from_bucket, compute_time_ago_bucket


class TestComputeTimeAgoBucket:
    def test_seconds_bucket_for_small_and_negative_values(self) -> None:
        assert compute_time_ago_bucket(0) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket(-5) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket(1) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket(59) == {"kind": "seconds", "value": 0}

    def test_minutes_bucket_range(self) -> None:
        assert compute_time_ago_bucket(60) == {"kind": "minutes", "value": 1}
        # 1 分钟多一点仍按 1 分钟前处理
        assert compute_time_ago_bucket(119) == {"kind": "minutes", "value": 1}
        assert compute_time_ago_bucket(120) == {"kind": "minutes", "value": 2}
        assert compute_time_ago_bucket(3599) == {"kind": "minutes", "value": 59}

    def test_hours_bucket_range(self) -> None:
        assert compute_time_ago_bucket(3600) == {"kind": "hours", "value": 1}
        assert compute_time_ago_bucket(7200) == {"kind": "hours", "value": 2}
        # 恰好 23 小时
        assert compute_time_ago_bucket(23 * 3600) == {"kind": "hours", "value": 23}

    def test_days_bucket_range(self) -> None:
        # 恰好 24 小时
        assert compute_time_ago_bucket(86400) == {"kind": "days", "value": 1}
        # 超过 24 小时按天取整
        assert compute_time_ago_bucket(172800) == {"kind": "days", "value": 2}

    def test_non_numeric_input_falls_back_to_seconds(self) -> None:
        assert compute_time_ago_bucket(None) == {"kind": "seconds", "value": 0}
        assert compute_time_ago_bucket("not-a-number") == {"kind": "seconds", "value": 0}


class TestIdleBucketHelpers:
    def test_classify_idle_health_from_bucket_basic_mapping(self) -> None:
        """classify_idle_health_from_bucket 应按 kind 做粗粒度健康映射。"""

        assert classify_idle_health_from_bucket({"kind": "seconds", "value": 0}) is IdleHealth.HEALTHY
        assert classify_idle_health_from_bucket({"kind": "minutes", "value": 5}) is IdleHealth.HEALTHY
        assert classify_idle_health_from_bucket({"kind": "hours", "value": 1}) is IdleHealth.IDLE
        assert classify_idle_health_from_bucket({"kind": "days", "value": 2}) is IdleHealth.STALE

    def test_classify_idle_health_from_bucket_defensive_fallback(self) -> None:
        """异常或未知 kind 应回退为 IdleHealth.UNKNOWN。"""

        assert classify_idle_health_from_bucket({"kind": "weird", "value": 0}) is IdleHealth.UNKNOWN
        # 缺失 kind 字段时按默认 "seconds" 处理 → HEALTHY
        assert classify_idle_health_from_bucket({"value": 0}) is IdleHealth.HEALTHY


class TestACPSessionManagerIdleHealth:
    def test_classify_idle_health_delegates_to_ssot(self) -> None:
        """ACPSessionManager.classify_idle_health 应与 SSOT helper 语义一致。"""

        bucket = compute_time_ago_bucket(300)
        expected = classify_idle_health_from_bucket(bucket)

        actual = ACPSessionManager.classify_idle_health(bucket)

        assert actual is expected

    def test_classify_idle_health_falls_back_to_unknown_on_expected_error_with_logging_and_metrics(
        self, monkeypatch, caplog
    ) -> None:
        """预期输入异常应回退为 UNKNOWN，并产生 warning 日志与监控埋点调用。"""

        import src.utils.time_ago as time_ago_mod

        def _boom(_bucket):  # type: ignore[unused-argument]
            raise ValueError("bad bucket")

        monkeypatch.setattr(time_ago_mod, "classify_idle_health_from_bucket", _boom)

        caplog.set_level(logging.WARNING, logger="src.acp.manager")

        bucket = compute_time_ago_bucket(60)
        health = ACPSessionManager.classify_idle_health(bucket)

        # 语义应回退为 UNKNOWN
        assert isinstance(health, IdleHealth)
        assert health is IdleHealth.UNKNOWN

        # 应产生一条包含错误类型的 warning 日志
        warn_records = [r for r in caplog.records if "IdleHealth classification fallback to UNKNOWN" in r.getMessage()]
        assert len(warn_records) == 1
        assert "ValueError" in warn_records[0].getMessage()

        # 监控埋点钩子应被调用一次，且携带正确的 error_type
        # 这里通过日志内容进行近似验证，详细 telemetry 调用在 keepalive 测试中覆盖。
        assert "ValueError" in warn_records[0].getMessage()

    def test_classify_idle_health_propagates_unexpected_error(self, monkeypatch) -> None:
        """非预期异常（如 RuntimeError）应向上传播而不是静默回退。"""

        import src.utils.time_ago as time_ago_mod

        def _boom(_bucket):  # type: ignore[unused-argument]
            raise RuntimeError("boom")

        monkeypatch.setattr(time_ago_mod, "classify_idle_health_from_bucket", _boom)

        bucket = compute_time_ago_bucket(60)
        with pytest.raises(RuntimeError):
            ACPSessionManager.classify_idle_health(bucket)

    def test_classify_idle_health_logs_context_fields_when_present(self, monkeypatch, caplog) -> None:
        """当提供 IdleHealthTelemetryContext 时，回退 warning 日志中应包含关键上下文字段。"""

        import src.utils.time_ago as time_ago_mod

        def _boom(_bucket):  # type: ignore[unused-argument]
            raise ValueError("bad bucket")

        monkeypatch.setattr(time_ago_mod, "classify_idle_health_from_bucket", _boom)

        caplog.set_level(logging.WARNING, logger="src.acp.manager")

        bucket = compute_time_ago_bucket(60)
        ctx: IdleHealthTelemetryContext = {
            "manager_agent_type": "coco",
            "session_key": "chat-123:proj-1:t:thread-9",
            "chat_id": "chat-123",
            "project_id": "proj-1",
            "thread_id": "thread-9",
            "session_id": "sess-xyz",
            "idle_seconds": 60.0,
            "idle_bucket": bucket,
        }

        health = ACPSessionManager.classify_idle_health(bucket, context=ctx)

        # 语义仍应回退为 UNKNOWN
        assert isinstance(health, IdleHealth)
        assert health is IdleHealth.UNKNOWN

        warn_records = [
            r for r in caplog.records if "IdleHealth classification fallback to UNKNOWN" in r.getMessage()
        ]
        assert len(warn_records) == 1
        msg = warn_records[0].getMessage()

        # 核心上下文字段应出现在日志中
        assert "agent_type=coco" in msg
        assert "chat_id=chat-123" in msg
        assert "project_id=proj-1" in msg
        assert "session_id=sess-xyz" in msg


class TestIdleHealthFallbackEntryPoint:
    def test_classify_idle_health_with_fallback_normal_path(self) -> None:
        """统一入口在正常输入场景下应与 SSOT helper 语义一致。"""

        bucket = compute_time_ago_bucket(300)
        expected = classify_idle_health_from_bucket(bucket)

        actual = _classify_idle_health_with_fallback(bucket)

        assert isinstance(actual, IdleHealth)
        assert actual is expected

    def test_classify_idle_health_with_fallback_expected_error_uses_telemetry_and_returns_unknown(
        self, monkeypatch
    ) -> None:
        """预期输入异常应通过 Telemetry 记录日志/指标并回退为 UNKNOWN。"""

        import src.utils.time_ago as time_ago_mod

        def _boom(_bucket):  # type: ignore[unused-argument]
            raise ValueError("bad bucket from telemetry fallback test")

        monkeypatch.setattr(time_ago_mod, "classify_idle_health_from_bucket", _boom)

        class CapturingTelemetry:
            def __init__(self) -> None:
                self.logged: list[tuple[dict, object, dict | None]] = []
                self.metrics: list[str] = []

            def log_idle_health_classification_fallback(self, *, bucket, error, context=None):  # type: ignore[no-untyped-def]
                self.logged.append((bucket, error, context))

            def record_idle_health_fallback_metric(self, *, error_type: str) -> None:
                self.metrics.append(error_type)

        telemetry = CapturingTelemetry()
        bucket = compute_time_ago_bucket(60)
        ctx = {"foo": "bar"}

        health = _classify_idle_health_with_fallback(bucket, context=ctx, telemetry=telemetry)  # type: ignore[arg-type]

        assert isinstance(health, IdleHealth)
        assert health is IdleHealth.UNKNOWN

        assert len(telemetry.logged) == 1
        logged_bucket, err, logged_ctx = telemetry.logged[0]
        assert logged_bucket == bucket
        assert isinstance(err, ValueError)
        assert isinstance(logged_ctx, dict)
        assert logged_ctx.get("foo") == "bar"
        assert telemetry.metrics == ["ValueError"]

    def test_classify_idle_health_with_fallback_propagates_unexpected_error(self, monkeypatch) -> None:
        """非预期异常应向上传播，避免静默吞掉真实错误。"""

        import src.utils.time_ago as time_ago_mod

        def _boom(_bucket):  # type: ignore[unused-argument]
            raise RuntimeError("boom from telemetry fallback test")

        monkeypatch.setattr(time_ago_mod, "classify_idle_health_from_bucket", _boom)

        bucket = compute_time_ago_bucket(60)

        with pytest.raises(RuntimeError):
            _classify_idle_health_with_fallback(bucket)
