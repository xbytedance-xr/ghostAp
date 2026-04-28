"""IdleHealth Telemetry 与 IdleHealthConfig SSOT 抽象层。

本模块作为 IdleHealth 相关行为的**唯一推荐配置入口与可观测性适配层**，
向业务代码暴露的心智模型刻意收敛为：

- 业务侧（如 FeishuWSClient / 各类 *Manager*）只需通过
  :func:`build_idle_health_config_for_manager` 构造 :class:`IdleHealthConfig`
  实例，并在创建 Manager 时以 ``idle_health_config=...`` 形式注入；
- 会话级 Telemetry（如 `on_session_start` / `on_session_end`）由
  :class:`TelemetryAdapter` 体系负责，IdleHealth UNKNOWN 回退相关的细粒度
  Telemetry 由 :class:`IdleHealthTelemetry` 负责，二者可以通过
  :class:`IdleHealthConfig` 组合；
- IdleHealth 判定与 UNKNOWN 回退语义（包括监控埋点与集中日志）统一收敛于
  本模块及 `src.utils.time_ago`，调用方无需感知底层细节。

推荐使用方式（新 Manager 接入 IdleHealth 的一段式示例）::

    from src.acp.manager import ACPSessionManager
    from src.acp.telemetry import build_idle_health_config_for_manager, TelemetryAdapter

    class MySessionTelemetry(TelemetryAdapter):  # 可选：自定义会话级 Telemetry
        ...

    idle_cfg = build_idle_health_config_for_manager(
        session_telemetry=MySessionTelemetry(),  # 可选：未提供则使用默认适配器
    )

    manager = ACPSessionManager(
        "coco",  # 或其他 agent_type
        idle_health_config=idle_cfg,
    )

除上述 builder + ``idle_health_config=`` 注入路径外，模块中其余 IdleHealth
相关工厂函数、协议类型与服务实现（例如
:class:`IdleHealthServiceProtocol`、:class:`IdleHealthService`、
``get_idle_health_telemetry_for_manager``、
``get_idle_health_service_for_manager``、
``IdleHealthConfig.resolve_for_manager`` 等）默认视为 Telemetry 层内部或
测试/高级用法入口。它们会继续保持稳定以兼容现有调用点，但**不建议业务
代码在新逻辑中直接依赖**，以免再次放大对外配置面与心智负担。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol, TYPE_CHECKING, TypedDict

from src.utils.time_ago import IdleHealth, TimeAgoBucket, compute_time_ago_bucket
from .helper import SessionKeyCodec

# 模块公开 API 面：其余未在 __all__ 中列出的符号视为内部实现细节或测试工具。
#
# 对业务调用方（包括 Handler / Manager / FeishuWSClient 等），IdleHealth 相关
# 的**唯一推荐入口**刻意收敛为三个：
# - :class:`IdleHealthConfig`：ACPSessionManager 级 IdleHealth 高层配置对象；
# - :func:`build_idle_health_config_for_manager`：构造 IdleHealthConfig 的便捷工厂；
# - :class:`IdleHealthTelemetryContext`：仅在需要透出 Telemetry-only 上下文
#   类型时使用。
#
# 其余 IdleHealth 工厂函数、协议类型与服务实现（例如
# :class:`IdleHealthServiceProtocol`、:class:`IdleHealthService`、
# ``classify_idle_health_for_manager``、``get_idle_health_*``、
# ``IdleHealthConfig.resolve_for_manager`` 等）均视为 Telemetry 层内部或
# 测试/高级用法入口：
# - 不再通过 ``__all__`` 导出；
# - 在命名上以下划线前缀标明内部使用，避免在 IDE 自动补全中放大心智负担；
# - 业务代码在新逻辑中不应直接依赖这些符号。
__all__ = [
    # === 业务推荐入口（Stable API） ===
    "IdleHealthConfig",
    "build_idle_health_config_for_manager",
    "IdleHealthTelemetryContext",
]

logger = logging.getLogger(__name__)


class IdleHealthContext(TypedDict, total=False):
    """[INTERNAL] IdleHealth UNKNOWN 回退场景下使用的结构化日志/监控上下文。

    语义约束：
    - **仅用于 IdleHealth UNKNOWN 回退时的 Telemetry（日志/监控）用途**；
    - 不参与业务决策、不参与 ACP 协议/领域模型建模，也不会出现在对外 API
      或持久化结构中；
    - ``idle_bucket`` 使用 TimeAgo 语义层的 SSOT 结构，仅作排障与路由诊断
      关联，不承载额外业务含义。

    换言之：调用方应将本类型视为「运维/可观测性上下文」，而不是 IdleHealth
    领域模型的一部分。如需在业务层做决策，请基于 ``IdleHealth`` 枚举本身或
    上游协议模型进行建模，而不要扩展/复用 ``IdleHealthContext`` 字段。
    """

    # 管理器与路由维度
    manager_agent_type: str
    session_key: str
    chat_id: str
    project_id: Optional[str]
    thread_id: Optional[str]

    # 会话标识与空闲语义
    session_id: str
    idle_seconds: float
    idle_bucket: TimeAgoBucket


# Telemetry-only 语义别名：推荐在类型标注中使用该名称，
# 进一步弱化 IdleHealthContext 被误解为领域模型的可能性。
IdleHealthTelemetryContext = IdleHealthContext
"""【Telemetry-only · UNKNOWN 回退】IdleHealth UNKNOWN 回退路径的日志/监控上下文（不得用于业务协议或领域模型）。

用于在 UNKNOWN 回退路径中承载结构化日志/监控所需的路由与会话诊断信息，字段
与 :class:`IdleHealthContext` 保持一致，由 Telemetry 层消费。
"""


class _IdleHealthTelemetry(Protocol):
    """[INTERNAL] IdleHealth UNKNOWN 回退路径的监控与日志抽象接口。

    设计约束：
    - 仅暴露 IdleHealth UNKNOWN 回退场景下需要的监控/日志能力；
    - 不承载 IdleHealth 判定本身的业务语义，该语义由 SSOT helper 提供；
    - 默认实现需要与历史行为等价，避免引入回归（将在后续迁移中完成）。
    """

    def record_idle_health_fallback_metric(self, *, error_type: str) -> None:
        """记录 IdleHealth 兜底回退场景下的监控埋点。"""

    def log_idle_health_classification_fallback(
        self,
        *,
        bucket: "TimeAgoBucket",
        error: Exception,
        context: IdleHealthContext | None = None,
    ) -> None:
        """记录 IdleHealth UNKNOWN 回退场景的集中日志。"""


class _DefaultIdleHealthTelemetry:
    """[INTERNAL] :class:`IdleHealthTelemetry` 的默认实现。

    当前实现保持与历史模块级 hook 行为语义等价：
    - metric 埋点通过 `_record_idle_health_fallback_metric` 触发（默认 no-op）；
    - 日志通过 `_log_idle_health_classification_fallback` 统一输出。
    """

    def record_idle_health_fallback_metric(self, *, error_type: str) -> None:
        _record_idle_health_fallback_metric(error_type=error_type)

    def log_idle_health_classification_fallback(
        self,
        *,
        bucket: "TimeAgoBucket",
        error: Exception,
        context: IdleHealthContext | None = None,
    ) -> None:
        _log_idle_health_classification_fallback(bucket=bucket, error=error, context=context)


class TelemetryAdapter(Protocol):
    """会话生命周期 Telemetry 抽象接口（业务可自定义实现）。

    该接口关注的是「一次会话从启动到结束」过程中关键事件的埋点/统计，
    与 IdleHealth UNKNOWN 回退相关的细粒度 Telemetry 由
    :class:`IdleHealthTelemetry` 负责，二者相互独立、可按需组合。

    设计约束：
    - 不参与业务决策，仅负责构造和上报 Telemetry 数据；
    - 不做网络/I/O 细节的强约束，具体上报后端由实现方决定；
    - 调用方（例如 ACPSessionManager）应只在生命周期关键路径调用本接口，
      避免在热路径中引入过多 Telemetry 分支。
    """

    def on_session_start(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        backend_kind: str,
        model_name: str | None,
    ) -> None:
        """在会话成功启动后被调用。"""

    def on_session_start_failed(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        backend_kind: str,
        error: Exception,
        diagnostics: dict | None = None,
    ) -> None:
        """在会话启动失败（包括重试最终失败或降级失败）时被调用。"""

    def on_session_end(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        message_count: int,
        reason: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """在会话结束并清理后被调用。"""


class DefaultSessionTelemetryAdapter:
    """TelemetryAdapter 的默认实现（当前为 no-op，业务可直接复用）。

    该实现仅提供稳定的鸭子类型，方便在生产环境未接入 Telemetry 时保持
    零开销；测试或上层系统如需观测会话生命周期事件，可注入自定义实现
    替换本适配器。
    """

    def on_session_start(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        backend_kind: str,
        model_name: str | None,
    ) -> None:  # pragma: no cover - 默认实现为 no-op
        return None

    def on_session_start_failed(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        backend_kind: str,
        error: Exception,
        diagnostics: dict | None = None,
    ) -> None:  # pragma: no cover - 默认实现为 no-op
        return None

    def on_session_end(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        message_count: int,
        reason: str | None = None,
        extra: dict | None = None,
    ) -> None:  # pragma: no cover - 默认实现为 no-op
        return None


@dataclass
class IdleHealthConfig:
    """ACPSessionManager 级别的 IdleHealth 高层配置对象（业务推荐入口）。

    设计意图：
    - 将 idle_health 相关三个协作者的注入统一收敛为一个配置对象，减少
      :class:`ACPSessionManager` 构造函数中的协作者参数膨胀；
    - 对调用方（业务层/Handler/测试）暴露“两层心智”：
      1. 默认路径：只关心 `ACPSessionManager`，完全交由 Telemetry 模块
         通过默认工厂提供 IdleHealth 行为；
      2. 进阶路径：当需要精细可观测性或替换 IdleHealth 实现时，通过
         :class:`IdleHealthConfig`（或 :func:`build_idle_health_config_for_manager`
         的返回值）显式构造并注入。
    - 字段为可选类型：``None`` 表示「不在配置层声明覆写」，由使用方
      再通过默认工厂或显式参数决定最终实现。

    与 ACPSessionManager 的契约：
    - 对于业务调用方，推荐优先通过 ``idle_health_config=IdleHealthConfig(...)``
      或 ``idle_health_config=build_idle_health_config_for_manager(...)`` 注入
      IdleHealth 协作者，而**不直接**依赖 `IdleHealthTelemetry` / `IdleHealthService`
      等具体实现类型；
    - ACPSessionManager.__init__ 会将 idle_health_config 与显式协作者参数
      统一交由 :meth:`IdleHealthConfig._resolve_for_manager` 解析，保持 Telemetry
      层作为 IdleHealth 配置的单一事实来源（SSOT）。

    注意：
    - IdleHealthConfig **本身不做默认值推导，也不承载任何会话生命周期 Telemetry
      行为**，仅作为协作者与相关参数的配置载体；如需带默认值的便捷构造，可通
      过 :func:`build_idle_health_config_for_manager`；
    - 当 ACPSessionManager 同时收到 idle_health_config 与显式参数时，应由
      显式参数优先，IdleHealthConfig 仅作为「次级来源」参与解析；
    - 会话级 Telemetry 生命周期回调（如 ``on_session_start_failed``、
      ``on_session_end``）统一由 :class:`TelemetryAdapter` 实例（默认为
      :class:`DefaultSessionTelemetryAdapter`）承担，调用方不应再从
      IdleHealthConfig 上调用这些方法。
    """

    idle_health_telemetry: _IdleHealthTelemetry | None = None
    session_telemetry: TelemetryAdapter | None = None
    idle_health_service: _IdleHealthServiceProtocol | None = None

    @classmethod
    def _resolve_for_manager(
        cls,
        *,
        config: "IdleHealthConfig | None" = None,
        idle_health_telemetry: "_IdleHealthTelemetry | None" = None,
        session_telemetry: "TelemetryAdapter | None" = None,
        idle_health_service: "_IdleHealthServiceProtocol | None" = None,
    ) -> tuple["_IdleHealthTelemetry", "TelemetryAdapter", "_IdleHealthServiceProtocol"]:
        """[INTERNAL] 为 :class:`ACPSessionManager` 解析 IdleHealth 协作者三元组的高层入口。

        解析优先级遵循「显式参数 > config 字段 > 默认工厂/适配器」：

        - Telemetry：idle_health_telemetry 参数优先，其次为 ``config.idle_health_telemetry``，
          最后通过 :func:`get_idle_health_telemetry_for_manager` 获取默认实例；
        - Service：idle_health_service 参数优先，其次为 ``config.idle_health_service``，
          最后通过 :func:`get_idle_health_service_for_manager` 基于最终 Telemetry 构造默认协作者；
        - Session Telemetry：session_telemetry 参数优先，其次为 ``config.session_telemetry``，
          最后回退到 :class:`DefaultSessionTelemetryAdapter` no-op 实现。

        通过该方法，ACPSessionManager.__init__ 可直接获取解析后的协作者三元组，
        避免在构造函数内部重复实现优先级与默认策略。
        """

        cfg = config

        # IdleHealthTelemetry 解析
        if idle_health_telemetry is not None:
            eff_idle_health_telemetry: _IdleHealthTelemetry = idle_health_telemetry
        elif cfg is not None and cfg.idle_health_telemetry is not None:
            # 当 config 中已提供 Telemetry 实例时，直接使用该实例，避免经由工厂再次包装。
            eff_idle_health_telemetry = cfg.idle_health_telemetry
        else:
            # 未提供任何实例时，才通过 manager 工厂获取默认 Telemetry。
            eff_idle_health_telemetry = _get_idle_health_telemetry_for_manager(None)

        # IdleHealthService 解析
        if idle_health_service is not None:
            eff_idle_health_service: _IdleHealthServiceProtocol = idle_health_service
        elif cfg is not None and cfg.idle_health_service is not None:
            # 与 Telemetry 一致：优先复用 config 中的显式协作者实例。
            eff_idle_health_service = cfg.idle_health_service
        else:
            # 未提供 Service 实例时，基于最终 Telemetry 构造默认 IdleHealthService。
            eff_idle_health_service = _get_idle_health_service_for_manager(
                None,
                telemetry=eff_idle_health_telemetry,
            )

        # 会话生命周期 Telemetry 解析
        if session_telemetry is not None:
            eff_session_telemetry: TelemetryAdapter = session_telemetry
        elif cfg is not None and cfg.session_telemetry is not None:
            eff_session_telemetry = cfg.session_telemetry
        else:
            eff_session_telemetry = DefaultSessionTelemetryAdapter()

        return eff_idle_health_telemetry, eff_session_telemetry, eff_idle_health_service


class _IdleHealthServiceProtocol(Protocol):
    """[INTERNAL] IdleHealth 业务协作者协议：供 Telemetry/测试注入 IdleHealthService 实现使用。

    设计约束：
    - 仅暴露会话级 IdleHealth 判定所需的 classify_session_idle_health 能力；
    - 不关心具体 Telemetry 实现与 UNKNOWN 回退策略细节，这些由实现类负责；
    - 调用方（例如 ACPSessionManager）应只依赖本协议而非具体实现类型，便于
      在测试中注入 Fake/Stub 实现。
    """

    def classify_session_idle_health(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        last_active: float,
        now: Optional[float] = None,
        message_count: Optional[int] = None,
    ) -> tuple[IdleHealth, "TimeAgoBucket", float, IdleHealthTelemetryContext]:
        ...


class _IdleHealthService:
    """[INTERNAL] IdleHealth 业务协作者：集中封装 Bucket→IdleHealth 判定与 UNKNOWN 回退策略。

    设计意图：
    - 将「Idle 秒数/TimeAgoBucket → IdleHealth 枚举」以及 UNKNOWN 回退策略
      收敛在 Telemetry 层的协作者对象中，manager 仅负责提供上下文；
    - 通过构造函数注入 :class:`IdleHealthTelemetry`，便于测试中注入 Fake
      实现观察回退路径，而生产环境沿用默认 Telemetry 行为；
    - 保持与历史实现等价：当前实现仅是对
      :func:`_classify_idle_health_with_fallback` 的薄封装，不改变判定语义。

    注意：
    - 本类关注的是「Bucket → IdleHealth」与 Telemetry 协作，不直接关心会话
      生命周期或路由细节；
    - ACPSessionManager 在后续重构中可以选择注入该协作者，以减少自身对
      Telemetry 细节与兜底逻辑的感知。
    """

    def __init__(self, telemetry: _IdleHealthTelemetry | None = None) -> None:
        # 统一通过 manager 兼容工厂获得默认 Telemetry 行为，以保持历史语义。
        self._telemetry: _IdleHealthTelemetry = telemetry or _get_manager_compat_idle_health_telemetry()

    def classify_idle_health(
        self,
        bucket: "TimeAgoBucket",
        *,
        context: IdleHealthTelemetryContext | None = None,
    ) -> IdleHealth:
        """基于给定 TimeAgoBucket 计算 IdleHealth，并在预期异常时回退为 UNKNOWN。

        当前实现直接委托 :func:`classify_idle_health_with_fallback`，并统一
        使用构造时注入的 Telemetry 实例，方便调用方在测试中注入 Fake/
        Stub Telemetry 验证回退路径。
        """

        # Telemetry-only 上下文：调用方传入的 IdleHealthTelemetryContext 与
        # _classify_idle_health_with_fallback 的 IdleHealthContext 结构兼容，
        # 这里直接透传即可。
        return _classify_idle_health_with_fallback(bucket, context=context, telemetry=self._telemetry)

    def classify_session_idle_health(
        self,
        *,
        manager_agent_type: str,
        session_key: str,
        session_id: str,
        last_active: float,
        now: Optional[float] = None,
        message_count: Optional[int] = None,
    ) -> tuple[IdleHealth, "TimeAgoBucket", float, IdleHealthTelemetryContext]:
        """基于会话快照计算 IdleHealth，并构造 Telemetry 上下文。

        该方法用于将 ACPSessionManager.list_active_sessions 中的 IdleHealth
        分支抽离到协作者中：

        - 由本方法负责 idle_seconds 与 TimeAgoBucket 计算；
        - 统一通过 SessionKeyCodec 解析 session_key 以补充路由信息；
        - 使用 :meth:`classify_idle_health` 完成枚举判定与 UNKNOWN 回退；
        - 返回 (IdleHealth, bucket, idle_seconds, context) 供调用方序列化或
          进一步处理。
        """

        eff_now = float(now if now is not None else time.time())
        try:
            last_active_f = float(last_active or 0.0)
        except Exception:
            last_active_f = 0.0

        idle_seconds = max(0.0, eff_now - last_active_f) if last_active_f > 0 else 0.0
        idle_bucket = compute_time_ago_bucket(idle_seconds)

        # 基于 session_key 构造路由上下文，任何解析异常都不应影响主逻辑。
        try:
            chat_id, project_id, thread_id = SessionKeyCodec.decode(session_key or "")
        except Exception:
            chat_id, project_id, thread_id = "", None, None

        ctx: IdleHealthTelemetryContext = {
            "manager_agent_type": manager_agent_type,
            "session_key": session_key,
            "session_id": session_id,
            "idle_seconds": idle_seconds,
            "idle_bucket": idle_bucket,
        }
        if chat_id:
            ctx["chat_id"] = chat_id
        if project_id is not None:
            ctx["project_id"] = project_id
        if thread_id:
            ctx["thread_id"] = thread_id

        health = self.classify_idle_health(idle_bucket, context=ctx)
        return health, idle_bucket, idle_seconds, ctx


def _get_idle_health_service_for_manager(
    idle_health_service: _IdleHealthServiceProtocol | None = None,
    *,
    telemetry: _IdleHealthTelemetry | None = None,
) -> _IdleHealthServiceProtocol:
    """[INTERNAL] 为 ACPSessionManager 及 Telemetry 层提供 IdleHealthService 注入/默认构造门面。

    设计约束：
    - 如果调用方显式传入 ``idle_health_service``，优先直接返回该实例；
    - 否则基于给定的 ``telemetry``（若为空则保持 IdleHealthService 的默认
      Telemetry 语义）构造 :class:`IdleHealthService` 作为默认实现；
    - 通过该门面函数，ACPSessionManager 只依赖协议和工厂，而不感知具体类名。
    """

    if idle_health_service is not None:
        return idle_health_service

    return _IdleHealthService(telemetry=telemetry)


class _ManagerHookIdleHealthTelemetry(_DefaultIdleHealthTelemetry):
    """面向 ACPSessionManager.classify_idle_health 的兼容 Telemetry 实现。

    说明：
    - 当前仅继承 :class:`DefaultIdleHealthTelemetry` 行为，不做额外扩展；
    - 单独定义子类是为了在需要时可以针对静态入口定制附加行为，
      同时在日志中更容易区分来源；
    - 所有实际日志与监控埋点仍通过本模块内的私有 hook 完成，保持
      「调用收口在 telemetry 层」的设计约束。
    """

    # NOTE: 暂无额外逻辑，仅作为可扩展占位。
    pass


def _classify_idle_health_with_fallback(
    bucket: "TimeAgoBucket",
    context: IdleHealthContext | None = None,
    telemetry: _IdleHealthTelemetry | None = None,
    ) -> IdleHealth:
    """IdleHealth 统一入口：基于 bucket 分类并在预期异常场景下集中兜底。

    语义约定：

    - 正常情况下直接委托 :func:`src.utils.time_ago.classify_idle_health_from_bucket`；
    - 仅在出现 *预期输入/状态错误*（ValueError/TypeError/KeyError/AttributeError）时：
      - 通过给定或默认的 :class:`IdleHealthTelemetry` 实例记录日志与监控埋点；
      - 始终回退为 ``IdleHealth.UNKNOWN``；
    - 其他异常（如 RuntimeError）向上传播，避免吞掉真实逻辑错误。

    调用方应统一通过本函数完成 IdleHealth 粗粒度分类与 UNKNOWN 回退策略，
    使得兜底逻辑完全收敛在 Telemetry 层，`ACPSessionManager` 等管理器仅构造
    :class:`IdleHealthContext` 并注入合适的 telemetry 实现。

    注意：
    - 生产侧推荐通过 :func:`_classify_idle_health_for_manager` 间接使用本函数；
    - 直接调用通常仅出现在 Telemetry 模块内部或针对兜底策略的单元测试中。
    """

    telemetry = telemetry or _get_default_idle_health_telemetry()

    expected_errors = (ValueError, TypeError, KeyError, AttributeError)

    try:
        # 通过模块引用而不是直接导入函数，保证 tests 能通过 monkeypatch
        # `src.utils.time_ago.classify_idle_health_from_bucket` 精确控制行为。
        import src.utils.time_ago as time_ago_mod

        health: IdleHealth = time_ago_mod.classify_idle_health_from_bucket(bucket)  # type: ignore[assignment]
        return health
    except expected_errors as exc:
        try:
            telemetry.log_idle_health_classification_fallback(
                bucket=bucket,
                error=exc,
                context=context,
            )
            telemetry.record_idle_health_fallback_metric(error_type=type(exc).__name__)
        except Exception:
            # Telemetry 作为附加可观测性，不得影响主流程。
            logger.debug("telemetry recording failed", exc_info=True)

        return IdleHealth.UNKNOWN


def _classify_idle_health_for_manager(
    bucket: "TimeAgoBucket",
    context: IdleHealthContext | None = None,
    telemetry: _IdleHealthTelemetry | None = None,
    ) -> IdleHealth:
    """面向 ACPSessionManager 的首选 IdleHealth 分类入口（官方入口）。

    设计约束：

    - 仅暴露 «给我 bucket → 返回 IdleHealth» 的心智模型，调用方无需感知
      Telemetry 工厂与实现分层；
    - 对于 ACPSessionManager 及其上层业务，这是 IdleHealth 分类的**唯一
      推荐公共入口**，新代码不应直接依赖 :func:`_classify_idle_health_with_fallback`；
    - 如果显式传入 ``telemetry``，则优先使用该实例；否则统一通过
      :func:`get_manager_compat_idle_health_telemetry` 获取专用于 manager
      静态入口的 Telemetry 实例；
    - 实际分类与 UNKNOWN 回退策略仍由 :func:`classify_idle_health_with_fallback`
      承担，保持与历史行为等价。
    """

    eff_telemetry = telemetry or _get_manager_compat_idle_health_telemetry()
    return _classify_idle_health_with_fallback(bucket, context=context, telemetry=eff_telemetry)


def _get_idle_health_telemetry_for_manager(
    telemetry: _IdleHealthTelemetry | None = None,
) -> _IdleHealthTelemetry:
    """[INTERNAL] 为 :class:`ACPSessionManager` 实例提供 IdleHealthTelemetry 注入/默认构造门面。

    设计约束：

    - 如果调用方已经构造了自定义 ``telemetry`` 实例，则直接返回该实例；
    - 如果未显式传入，则回退到 :func:`get_manager_compat_idle_health_telemetry`
      以保持与历史行为等价；
    - 通过该门面函数，`ACPSessionManager` 无需了解默认实现类型或工厂细节，
      仅依赖协议与高层入口。
    """

    if telemetry is not None:
        return telemetry
    return _get_manager_compat_idle_health_telemetry()


def _record_idle_health_fallback_metric(*, error_type: str) -> None:
    """IdleHealth 兜底回退的监控埋点钩子（兼容入口）。

    说明：
    - 历史上该函数作为可 monkeypatch 的全局监控钩子，默认实现为 no-op；
    - 引入 IdleHealthTelemetry 后，新逻辑应优先通过实例上的 telemetry
      对象上报指标；
    - 为保持兼容性，测试仍可以 monkeypatch 本函数来观察调用行为，但
      长期建议迁移到基于 telemetry 注入的方式。
    """

    # 默认实现保持 no-op 语义，避免在未配置监控系统时引入额外依赖；
    # 具体监控系统的对接由上层通过 telemetry 注入完成。
    return None


def _log_idle_health_classification_fallback(
    *,
    bucket: "TimeAgoBucket",
    error: Exception,
    context: IdleHealthContext | None = None,
) -> None:
    """IdleHealth UNKNOWN 回退的集中日志出口（兼容入口）。

    说明：
    - 历史上该函数作为集中日志输出点，便于统一附加 IdleHealthContext；
    - 引入 IdleHealthTelemetry 后，新逻辑建议通过 telemetry 对象实现相同
      行为，并在需要时扩展到外部日志后端；
    - 为保持兼容，旧调用点仍可直接调用本函数，长期建议迁移到 telemetry。
    """

    try:
        try:
            error_type = type(error).__name__
        except Exception:
            error_type = "Exception"

        ctx_suffix = ""
        if context:
            try:
                parts: list[str] = []
                agent_type = (context.get("manager_agent_type") or "").strip()
                if agent_type:
                    parts.append(f"agent_type={agent_type}")

                session_key = (context.get("session_key") or "").strip()
                if session_key:
                    parts.append(f"session_key={session_key}")

                chat_id = (context.get("chat_id") or "").strip()
                if chat_id:
                    parts.append(f"chat_id={chat_id}")

                project_id = context.get("project_id")
                if project_id is not None:
                    project_id_str = str(project_id or "").strip() or "_default_"
                    parts.append(f"project_id={project_id_str}")

                thread_id = (context.get("thread_id") or "").strip()
                if thread_id:
                    parts.append(f"thread_id={thread_id}")

                session_id = (context.get("session_id") or "").strip()
                if session_id:
                    parts.append(f"session_id={session_id}")

                if parts:
                    ctx_suffix = " " + " ".join(parts)
            except Exception:
                # 附加上下文失败不应影响主日志。
                ctx_suffix = ""

        logger.warning(
            "[ACP] IdleHealth classification fallback to UNKNOWN due to input error: bucket=%r, error_type=%s, error=%s%s",
            bucket,
            error_type,
            error,
            ctx_suffix,
        )
    except Exception:
        # 日志本身绝不能影响调用方逻辑。
        return


def _get_default_idle_health_telemetry() -> _IdleHealthTelemetry:
    """构造默认 IdleHealthTelemetry 实例的工厂函数。

    当前仅返回 :class:`DefaultIdleHealthTelemetry`，后续如果需要按配置
    切换监控后端或注入外部实现，可以集中在本工厂函数内演进。
    """

    return _DefaultIdleHealthTelemetry()


def _get_manager_compat_idle_health_telemetry() -> _IdleHealthTelemetry:
    """为 `ACPSessionManager.classify_idle_health` 提供的兼容 Telemetry 工厂。

    设计目标：
    - 将静态入口的可观测性依赖收敛到 telemetry 模块内；
    - 保持与历史基于模块级 hook 的行为等价（默认仍使用模块级私有 hook）；
    - 为未来按配置扩展静态入口专属 Telemetry 行为预留注入点。
    """

    return _ManagerHookIdleHealthTelemetry()


def build_idle_health_config_for_manager(
    *,
    idle_health_telemetry: _IdleHealthTelemetry | None = None,
    session_telemetry: TelemetryAdapter | None = None,
    idle_health_service: _IdleHealthServiceProtocol | None = None,
) -> IdleHealthConfig:
    """面向 ACPSessionManager 的 IdleHealthConfig 便捷工厂。

    语义约定：

    - 如果 ``idle_health_telemetry`` 为空，则通过
      :func:`get_idle_health_telemetry_for_manager` 获取与 manager 静态入口
      等价的默认 Telemetry 实现；
    - 如果 ``idle_health_service`` 为空，则基于上一步确定的 telemetry
      调用 :func:`get_idle_health_service_for_manager` 获取默认协作者；
    - ``session_telemetry`` 默认为 ``None``，由 ACPSessionManager 在未显式
      指定时回退到 :class:`DefaultSessionTelemetryAdapter`，避免在 builder
      层提前做多余绑定；
    - 调用方可以通过显式传入任意字段来覆盖上述默认值；传入 ``None`` 与
      不传的语义一致，均表示「使用默认工厂」。
    """

    eff_telemetry = _get_idle_health_telemetry_for_manager(idle_health_telemetry)
    eff_service = _get_idle_health_service_for_manager(idle_health_service, telemetry=eff_telemetry)

    return IdleHealthConfig(
        idle_health_telemetry=eff_telemetry,
        session_telemetry=session_telemetry,
        idle_health_service=eff_service,
    )
