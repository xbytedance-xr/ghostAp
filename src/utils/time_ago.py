"""TimeAgo semantics utilities.

本模块仅负责「秒数 → 语义化时间区间」的纯计算逻辑，不直接关心任何具体文案或 UI 展示。

设计原则：
- 只依赖 Python 标准库与类型标注；
- 作为 TimeAgo 语义层的单一事实来源（SSOT）；
- 上层模块（如文案层、卡片渲染、管理器逻辑）应依赖本模块暴露的语义结果
  再自行决定如何渲染为最终文案。

非目标：
- 不在本模块内引入任何 UI 文案、本地化或主题配置逻辑；
- 不承诺在当前 "seconds/minutes/hours/days" 之外暴露更细粒度区间（如周/月），
  此类扩展需在保持既有行为等价的前提下通过后续演进完成。
"""

from enum import Enum
from typing import Literal, TypedDict


TimeAgoKind = Literal["seconds", "minutes", "hours", "days"]


class IdleHealth(str, Enum):
    """会话空闲健康状态的语义枚举。

    设计约定：
    - 仅表示基于 *空闲时长* 粗粒度划分的健康等级；
    - 作为 TimeAgo 之上的第二层语义，不直接绑定任何 UI 文案或 emoji；
    - 所有需要展示/序列化的调用方应基于该枚举再做映射，避免 magic string 扩散。
    """

    HEALTHY = "healthy"
    IDLE = "idle"
    STALE = "stale"
    UNKNOWN = "unknown"


class TimeAgoBucket(TypedDict):
    """语义化的相对时间区间。

    kind/value 约定：
    - kind == "seconds" 且 value == 0 表示「刚刚」（<60 秒，按统一文案处理，不再区分具体秒数）；
    - kind == "minutes" 表示按分钟取整后的区间（1-59 分钟）；
    - kind == "hours" 表示按小时取整后的区间（1-23 小时）；
    - kind == "days" 表示按天取整后的区间（>=1 天）。

    注意：负数或异常输入会在计算阶段被归一化为 0 秒，对应 "seconds"/0。
    """

    kind: TimeAgoKind
    value: int


def compute_time_ago_bucket(seconds: float | None) -> TimeAgoBucket:
    """将秒数转换为相对时间语义段，而不绑定具体文案。

    输入规则：
    - None、NaN 或无法被转换为 float 的值都按 0 处理；
    - 负数会被 clamp 到 0，避免出现「负几天前」等文案；
    - 内部使用 ``int`` 对秒数向下取整，与文案层 `format_time_ago` 现有行为保持一致。

    本函数作为 **TimeAgo 语义层的 SSOT（single source of truth）**：
    所有基于「多久之前」做逻辑分支或展示的调用方，应优先依赖本函数
    得到 :class:`TimeAgoBucket`，再在各自的 UI 层选择合适的渲染方式。

    返回值示例：
    - {"kind": "seconds", "value": 0}   # 刚刚（<60 秒）
    - {"kind": "minutes", "value": 3}   # 3 分钟前
    - {"kind": "hours", "value": 2}     # 2 小时前
    - {"kind": "days", "value": 1}      # 1 天前
    """

    try:
        s = int(max(0, float(seconds or 0.0)))
    except Exception:
        s = 0

    if s < 60:
        return {"kind": "seconds", "value": 0}

    minutes = s // 60
    if minutes < 60:
        return {"kind": "minutes", "value": int(minutes)}

    hours = minutes // 60
    if hours < 24:
        return {"kind": "hours", "value": int(hours)}

    days = hours // 24
    return {"kind": "days", "value": int(days)}


def classify_idle_health_from_bucket(bucket: TimeAgoBucket) -> IdleHealth:
    """基于 :class:`TimeAgoBucket` 计算会话空闲健康状态。

    当前映射策略与原 `ACPSessionManager.classify_idle_health` 保持语义等价：

    - kind in {"seconds", "minutes"} → IdleHealth.HEALTHY
    - kind == "hours" → IdleHealth.IDLE
    - kind == "days" → IdleHealth.STALE
    - 其他/异常输入 → IdleHealth.UNKNOWN

    说明：
    - 该函数只表达语义分类，不做任何清理决策；
    - 调用方可将 IdleHealth 用于日志、诊断卡片等上层展示。
    """

    try:
        kind = str(bucket.get("kind", "seconds"))
    except Exception:  # pragma: no cover - 极端防御
        kind = "seconds"

    if kind in ("seconds", "minutes"):
        return IdleHealth.HEALTHY
    if kind == "hours":
        return IdleHealth.IDLE
    if kind == "days":
        return IdleHealth.STALE
    return IdleHealth.UNKNOWN


__all__ = [
    "TimeAgoKind",
    "TimeAgoBucket",
    "IdleHealth",
    "compute_time_ago_bucket",
    "classify_idle_health_from_bucket",
]
