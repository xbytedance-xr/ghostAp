from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class EngineCardState:
    title: str = ""
    content: str = ""
    progress_bar: Optional[str] = None
    project_id: Optional[str] = None
    engine_project_id: Optional[str] = None
    is_executing: bool = False
    is_paused: bool = False
    engine_name: str = "Coco"
    show_buttons: bool = True
    working_dir: Optional[str] = None
    status_line: Optional[str] = None
    duration_line: Optional[str] = None
    criteria_section: Optional[str] = None
    footer_note: Optional[str] = None
    compact: bool = False
    expanded: bool = False
    expand_ac: bool = False
    action_prefix: str = "deep"
    extra_buttons: Optional[list[dict]] = None
    warning_banner: Optional[str] = None

    @property
    def deep_project_id(self):
        return self.engine_project_id


DeepCardState = EngineCardState


class KeyEventKind(str, Enum):
    """卡片侧“关键事件”最小集合（跨 Normal/Deep/Loop/Spec 统一语义）。

    说明：这里的 kind 只描述“是什么类型的信息”，不绑定具体引擎实现。
    """

    STAGE = "stage"  # Deep: 阶段；Spec: phase；Loop: lifecycle stage
    TURN = "turn"  # Normal: 轮次
    ITERATION = "iteration"  # Loop: 迭代
    PLAN = "plan"  # 计划/Checklist 更新
    TOOL = "tool"  # 工具调用（开始/进度/完成）
    ARTIFACT = "artifact"  # 产物（文件、链接、构建产物、报告等）
    ISSUE = "issue"  # 错误/告警/风险
    USER_ACTION = "user_action"  # 用户交互点（按钮点击、继续/停止等）
    CONCLUSION = "conclusion"  # 最终结论/交付摘要


class KeyEventSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueDisposition(str, Enum):
    """错误/告警条目的处置状态（用于“错误与告警”面板）。"""

    UNHANDLED = "unhandled"  # 未处理
    IN_PROGRESS = "in_progress"  # 处理中
    RECOVERED = "recovered"  # 已恢复
    NEED_USER = "need_user"  # 需用户介入


@dataclass(frozen=True)
class KeyEvent:
    """用于卡片展示与交互定位的“关键事件”统一模型。

    字段契约（核心）：
    - event_id: 稳定标识（用于引用/定位/交互 value 中传递）
    - ts: 时间戳（秒，float）
    - kind: 事件类型（KeyEventKind）
    - title/body: 可展示文本（建议 Markdown）
    - refs: 被引用的事件ID列表（用于跨区域避免重复正文）
    - severity/disposition: ISSUE 事件的严重级别与处置状态
    - axis_key: 主轴定位键（如 turn:3 / iteration:2 / phase:Plan），帮助在卡片里快速定位
    """

    event_id: str
    ts: float
    kind: KeyEventKind
    title: str
    body: str = ""
    refs: tuple[str, ...] = ()
    severity: KeyEventSeverity = KeyEventSeverity.INFO
    disposition: Optional[IssueDisposition] = None
    axis_key: Optional[str] = None
    mode: Optional[str] = None  # Normal/Deep/Loop/Spec
    meta: dict = field(default_factory=dict)


def build_sample_key_events(now: Optional[float] = None) -> list[KeyEvent]:
    """生成一组可复用的样例关键事件（用于测试/回归/演示）。"""

    base = now if now is not None else time.time()
    return [
        KeyEvent(
            event_id="run:start",
            ts=base,
            kind=KeyEventKind.STAGE,
            title="执行开始",
            body="模式=Deep · engine=Coco",
            axis_key="stage:run",
            mode="Deep",
        ),
        KeyEvent(
            event_id="plan:1",
            ts=base + 1,
            kind=KeyEventKind.PLAN,
            title="计划更新",
            body="- ⏳ 读取代码\n- ⏳ 修改卡片渲染\n- ⏳ 运行测试",
            axis_key="stage:plan",
            mode="Deep",
        ),
        KeyEvent(
            event_id="tool:read:1",
            ts=base + 2,
            kind=KeyEventKind.TOOL,
            title="读取文件 src/acp/models.py",
            body="读取并解析 ACPEvent 定义",
            axis_key="stage:task",
            mode="Deep",
        ),
        KeyEvent(
            event_id="issue:1",
            ts=base + 3,
            kind=KeyEventKind.ISSUE,
            title="发现卡片存在折叠区块",
            body="需要移除 expand/collapse 交互，保证无折叠/省略。",
            severity=KeyEventSeverity.WARNING,
            disposition=IssueDisposition.IN_PROGRESS,
            refs=("tool:read:1",),
            axis_key="stage:analysis",
            mode="Deep",
        ),
        KeyEvent(
            event_id="run:done",
            ts=base + 10,
            kind=KeyEventKind.CONCLUSION,
            title="执行完成",
            body="状态=成功 · 产物=卡片渲染契约与测试通过",
            refs=("plan:1", "issue:1"),
            axis_key="stage:finish",
            mode="Deep",
        ),
    ]
