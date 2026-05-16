from __future__ import annotations

from src.acp.telemetry import (
    IdleHealthConfig,
    TelemetryAdapter,
    build_idle_health_config_for_manager,
)
from src.acp.telemetry import _DefaultIdleHealthTelemetry as DefaultIdleHealthTelemetry
from src.acp.telemetry import _IdleHealthServiceProtocol as IdleHealthServiceProtocol


class _DummySessionTelemetry(TelemetryAdapter):  # type: ignore[misc]
    def on_session_start(self, *, manager_agent_type, session_key, session_id, backend_kind, model_name):  # type: ignore[no-untyped-def]
        return None

    def on_session_start_failed(self, *, manager_agent_type, session_key, backend_kind, error, diagnostics=None):  # type: ignore[no-untyped-def]
        return None

    def on_session_end(self, *, manager_agent_type, session_key, session_id, message_count, reason=None, extra=None):  # type: ignore[no-untyped-def]
        return None


class _DummyService(IdleHealthServiceProtocol):  # type: ignore[misc]
    def classify_session_idle_health(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        last_active: float,
        now: float | None = None,
        message_count: int | None = None,
    ):
        # 返回一个最小可用的三元组（IdleHealth.UNKNOWN 在 runtime 导入）
        from src.utils.time_ago import IdleHealth, compute_time_ago_bucket

        bucket = compute_time_ago_bucket(0.0)
        return IdleHealth.UNKNOWN, bucket, 0.0, {
            "manager_agent_type": manager_agent_type,
            "session_key": session_key,
            "session_id": session_id,
            "idle_seconds": 0.0,
            "idle_bucket": bucket,
        }


def test_idle_health_config_dataclass_defaults_are_none():
    cfg = IdleHealthConfig()
    assert cfg.idle_health_telemetry is None
    assert cfg.session_telemetry is None
    assert cfg.idle_health_service is None


def test_idle_health_config_does_not_expose_session_lifecycle_methods():
    """IdleHealthConfig 作为纯配置对象，不应暴露会话生命周期 Telemetry 方法。"""

    cfg = IdleHealthConfig()

    # 配置对象不再承载生命周期行为，调用方应通过 session_telemetry 适配器处理这些事件。
    assert not hasattr(cfg, "on_session_start_failed")
    assert not hasattr(cfg, "on_session_end")


def test_build_idle_health_config_for_manager_uses_manager_defaults(monkeypatch):
    calls: dict[str, object] = {}

    def fake_get_idle_health_telemetry_for_manager(telemetry=None):  # type: ignore[no-untyped-def]
        calls["telemetry"] = telemetry
        return DefaultIdleHealthTelemetry()

    def fake_get_idle_health_service_for_manager(service=None, *, telemetry=None):  # type: ignore[no-untyped-def]
        calls["service"] = service
        calls["service_telemetry"] = telemetry
        return _DummyService()

    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_telemetry_for_manager",
        fake_get_idle_health_telemetry_for_manager,
    )
    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_service_for_manager",
        fake_get_idle_health_service_for_manager,
    )

    cfg = build_idle_health_config_for_manager()

    # builder 应该通过 manager 工厂获取非 None 的 Telemetry 与 Service
    assert cfg.idle_health_telemetry is not None
    assert cfg.idle_health_service is not None
    # session_telemetry 保持 None，交由 ACPSessionManager 决定默认实现
    assert cfg.session_telemetry is None

    # 未显式传入参数时，工厂应当收到 None 并返回默认实现
    assert calls["telemetry"] is None
    assert calls["service"] is None
    assert isinstance(calls["service_telemetry"], DefaultIdleHealthTelemetry)


def test_build_idle_health_config_for_manager_respects_explicit_overrides(monkeypatch):
    base_telemetry = DefaultIdleHealthTelemetry()
    base_service = _DummyService()
    session_adapter = _DummySessionTelemetry()

    def fake_get_idle_health_telemetry_for_manager(telemetry=None):  # type: ignore[no-untyped-def]
        # 即便传入 None，也应回退为 base_telemetry
        return base_telemetry if telemetry is None else telemetry

    def fake_get_idle_health_service_for_manager(service=None, *, telemetry=None):  # type: ignore[no-untyped-def]
        # 如果显式给了 service，就直接用；否则回退 base_service
        return service or base_service

    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_telemetry_for_manager",
        fake_get_idle_health_telemetry_for_manager,
    )
    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_service_for_manager",
        fake_get_idle_health_service_for_manager,
    )

    # 显式传入 telemetry 与 service，应被 builder 原样保留
    custom_telemetry = DefaultIdleHealthTelemetry()
    custom_service = _DummyService()

    cfg = build_idle_health_config_for_manager(
        idle_health_telemetry=custom_telemetry,
        session_telemetry=session_adapter,
        idle_health_service=custom_service,
    )

    assert cfg.idle_health_telemetry is custom_telemetry
    assert cfg.idle_health_service is custom_service
    assert cfg.session_telemetry is session_adapter


def test_build_idle_health_config_for_manager_explicit_none_behaves_like_default(monkeypatch):
    """传入显式 None 与完全省略参数应具有相同语义（均回退到默认工厂）。"""

    marker_telemetry = DefaultIdleHealthTelemetry()
    marker_service = _DummyService()

    def fake_get_idle_health_telemetry_for_manager(telemetry=None):  # type: ignore[no-untyped-def]
        # 任何形式的 None 都回退到 marker_telemetry
        return marker_telemetry

    def fake_get_idle_health_service_for_manager(service=None, *, telemetry=None):  # type: ignore[no-untyped-def]
        # 任何形式的 None 都回退到 marker_service
        return marker_service

    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_telemetry_for_manager",
        fake_get_idle_health_telemetry_for_manager,
    )
    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_service_for_manager",
        fake_get_idle_health_service_for_manager,
    )

    cfg_default = build_idle_health_config_for_manager()
    cfg_none = build_idle_health_config_for_manager(
        idle_health_telemetry=None,
        idle_health_service=None,
    )

    assert cfg_default.idle_health_telemetry is marker_telemetry
    assert cfg_default.idle_health_service is marker_service
    assert cfg_none.idle_health_telemetry is marker_telemetry
    assert cfg_none.idle_health_service is marker_service


def test_resolve_for_manager_uses_defaults_when_all_none(monkeypatch):
    """当既未提供 config，也未提供显式参数时，应统一回退到 manager 工厂与默认适配器。"""

    calls: dict[str, object] = {}

    def fake_get_idle_health_telemetry_for_manager(telemetry=None):  # type: ignore[no-untyped-def]
        calls["telemetry_arg"] = telemetry
        return DefaultIdleHealthTelemetry()

    def fake_get_idle_health_service_for_manager(service=None, *, telemetry=None):  # type: ignore[no-untyped-def]
        calls["service_arg"] = service
        calls["service_telemetry"] = telemetry
        return _DummyService()

    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_telemetry_for_manager",
        fake_get_idle_health_telemetry_for_manager,
    )
    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_service_for_manager",
        fake_get_idle_health_service_for_manager,
    )

    telemetry, session_adapter, service = IdleHealthConfig._resolve_for_manager(config=None)

    from src.acp.telemetry import DefaultSessionTelemetryAdapter

    assert isinstance(telemetry, DefaultIdleHealthTelemetry)
    assert isinstance(service, _DummyService)
    assert isinstance(session_adapter, DefaultSessionTelemetryAdapter)

    # 工厂应当看到 None 作为输入，并基于此返回默认实现
    assert calls["telemetry_arg"] is None
    assert calls["service_arg"] is None
    assert isinstance(calls["service_telemetry"], DefaultIdleHealthTelemetry)


def test_resolve_for_manager_prefers_config_when_no_explicit_overrides(monkeypatch):
    """当未传显式参数时，应优先使用 IdleHealthConfig 中的协作者。"""

    base_telemetry = DefaultIdleHealthTelemetry()
    base_service = _DummyService()
    session_adapter = _DummySessionTelemetry()

    # 即便工厂被 monkeypatch，也不应影响 config 中的显式实例
    def fake_get_idle_health_telemetry_for_manager(telemetry=None):  # type: ignore[no-untyped-def]
        return DefaultIdleHealthTelemetry()

    def fake_get_idle_health_service_for_manager(service=None, *, telemetry=None):  # type: ignore[no-untyped-def]
        return _DummyService()

    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_telemetry_for_manager",
        fake_get_idle_health_telemetry_for_manager,
    )
    monkeypatch.setattr(
        "src.acp.telemetry._get_idle_health_service_for_manager",
        fake_get_idle_health_service_for_manager,
    )

    cfg = IdleHealthConfig(
        idle_health_telemetry=base_telemetry,
        session_telemetry=session_adapter,
        idle_health_service=base_service,
    )

    telemetry, session_adapter_resolved, service = IdleHealthConfig._resolve_for_manager(config=cfg)

    assert telemetry is base_telemetry
    # Service 由工厂基于 Telemetry 构造，这里只要求保持类型与 config 中一致
    assert isinstance(service, type(base_service))
    assert session_adapter_resolved is session_adapter


def test_resolve_for_manager_explicit_args_override_config(monkeypatch):
    """显式传入的参数应覆盖 IdleHealthConfig 中的配置。"""

    class TelemetryA(DefaultIdleHealthTelemetry):
        pass

    class TelemetryB(DefaultIdleHealthTelemetry):
        pass

    class ServiceA(IdleHealthServiceProtocol):  # type: ignore[misc]
        def classify_session_idle_health(
            self,
            *,
            manager_agent_type: str,
            session_key: str,
            session_id: str,
            last_active: float,
            now: float | None = None,
            message_count: int | None = None,
        ):
            from src.utils.time_ago import IdleHealth, compute_time_ago_bucket

            bucket = compute_time_ago_bucket(0.0)
            return IdleHealth.HEALTHY, bucket, 0.0, {
                "manager_agent_type": manager_agent_type,
                "session_key": session_key,
                "session_id": session_id,
                "idle_seconds": 0.0,
                "idle_bucket": bucket,
            }

    class ServiceB(ServiceA):
        pass

    class SessionTelemetryA(_DummySessionTelemetry):
        pass

    class SessionTelemetryB(_DummySessionTelemetry):
        pass

    cfg = IdleHealthConfig(
        idle_health_telemetry=TelemetryA(),
        idle_health_service=ServiceA(),
        session_telemetry=SessionTelemetryA(),
    )

    telemetry, session_adapter_resolved, service = IdleHealthConfig._resolve_for_manager(
        config=cfg,
        idle_health_telemetry=TelemetryB(),
        session_telemetry=SessionTelemetryB(),
        idle_health_service=ServiceB(),
    )

    assert isinstance(telemetry, TelemetryB)
    assert isinstance(service, ServiceB)
    assert isinstance(session_adapter_resolved, SessionTelemetryB)
