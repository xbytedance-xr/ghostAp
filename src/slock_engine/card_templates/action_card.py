"""Action Card — unified side-effect gate for dangerous operations.

Implements the propose→confirm→execute→feedback pattern for operations
with side effects (shell execution, code commits, file deletion, config changes).
Agents generate a proposal card; humans confirm before execution proceeds.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .common import build_callback_button, build_card_wrapper, build_collapsible_panel


class ActionType(Enum):
    """Categories of side-effect actions requiring confirmation."""

    SHELL_EXECUTE = "shell_execute"
    CODE_COMMIT = "code_commit"
    FILE_DELETE = "file_delete"
    CONFIG_CHANGE = "config_change"
    DEPLOY = "deploy"
    CUSTOM = "custom"


class ActionStatus(Enum):
    """Action card lifecycle states."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


ACTION_TYPE_LABELS: dict[ActionType, str] = {
    ActionType.SHELL_EXECUTE: "🖥️ 命令执行",
    ActionType.CODE_COMMIT: "📝 代码提交",
    ActionType.FILE_DELETE: "🗑️ 文件删除",
    ActionType.CONFIG_CHANGE: "⚙️ 配置变更",
    ActionType.DEPLOY: "🚀 部署操作",
    ActionType.CUSTOM: "⚡ 自定义操作",
}

ACTION_TYPE_COLORS: dict[ActionType, str] = {
    ActionType.SHELL_EXECUTE: "orange",
    ActionType.CODE_COMMIT: "blue",
    ActionType.FILE_DELETE: "red",
    ActionType.CONFIG_CHANGE: "yellow",
    ActionType.DEPLOY: "purple",
    ActionType.CUSTOM: "grey",
}


@dataclass
class ActionProposal:
    """A proposed action awaiting human confirmation."""

    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    action_type: ActionType = ActionType.CUSTOM
    agent_id: str = ""
    agent_name: str = ""
    title: str = ""
    description: str = ""
    command: str = ""
    impact_summary: str = ""
    reversible: bool = True
    status: ActionStatus = ActionStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    result: str = ""
    channel_id: str = ""
    card_message_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "title": self.title,
            "description": self.description,
            "command": self.command,
            "impact_summary": self.impact_summary,
            "reversible": self.reversible,
            "status": self.status.value,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "result": self.result,
            "channel_id": self.channel_id,
        }


def build_action_proposal_card(proposal: ActionProposal) -> dict:
    """Build the proposal card shown to users for confirmation."""
    action_label = ACTION_TYPE_LABELS.get(proposal.action_type, "⚡ 操作")
    header_color = ACTION_TYPE_COLORS.get(proposal.action_type, "orange")

    elements: list[dict] = []

    # Agent info
    elements.append({
        "tag": "markdown",
        "content": f"**发起者:** {proposal.agent_name or proposal.agent_id}",
    })

    # Description
    if proposal.description:
        elements.append({
            "tag": "markdown",
            "content": proposal.description[:500],
        })

    # Command detail (in collapsible panel for long commands)
    if proposal.command:
        cmd_display = proposal.command[:300]
        if len(proposal.command) > 300:
            cmd_display += "..."
        elements.append(build_collapsible_panel(
            title="命令详情",
            elements=[{"tag": "markdown", "content": f"```\n{cmd_display}\n```"}],
            expanded=len(proposal.command) < 100,
        ))

    # Impact summary
    if proposal.impact_summary:
        elements.append({
            "tag": "markdown",
            "content": f"**影响范围:** {proposal.impact_summary[:200]}",
        })

    # Reversibility indicator + timeout notice
    reversible_text = "✅ 可撤销" if proposal.reversible else "⚠️ 不可撤销"
    elements.append({
        "tag": "markdown",
        "content": f"{reversible_text} | ⏱ 5分钟内未响应将自动拒绝",
        "text_size": "notation",
    })

    # Action buttons
    approve_btn = build_callback_button(
        text="✅ 确认执行",
        action=f"slock_action_approve_{proposal.action_id}",
        button_type="primary",
    )
    reject_btn = build_callback_button(
        text="❌ 拒绝",
        action=f"slock_action_reject_{proposal.action_id}",
        button_type="danger",
    )
    elements.append({
        "tag": "column_set",
        "columns": [
            {"tag": "column", "width": "auto", "elements": [approve_btn]},
            {"tag": "column", "width": "auto", "elements": [reject_btn]},
        ],
    })

    return build_card_wrapper(
        header_title=f"{action_label}: {proposal.title[:50]}",
        header_template=header_color,
        elements=elements,
        mobile_optimize=True,
    )


def build_action_result_card(proposal: ActionProposal) -> dict:
    """Build the result card after action execution completes."""
    if proposal.status == ActionStatus.COMPLETED:
        icon = "✅"
        header_color = "green"
        status_text = "执行完成"
    elif proposal.status == ActionStatus.FAILED:
        icon = "❌"
        header_color = "red"
        status_text = "执行失败"
    elif proposal.status == ActionStatus.REJECTED:
        icon = "🚫"
        header_color = "grey"
        status_text = "已拒绝"
    else:
        icon = "⏳"
        header_color = "blue"
        status_text = "执行中"

    action_label = ACTION_TYPE_LABELS.get(proposal.action_type, "操作")

    elements: list[dict] = []
    elements.append({
        "tag": "markdown",
        "content": f"{icon} **{status_text}** — {proposal.title[:80]}",
    })

    if proposal.result:
        result_display = proposal.result[:500]
        elements.append(build_collapsible_panel(
            title="执行结果",
            elements=[{"tag": "markdown", "content": f"```\n{result_display}\n```"}],
            expanded=True,
        ))

    elapsed = ""
    if proposal.resolved_at and proposal.created_at:
        elapsed_s = proposal.resolved_at - proposal.created_at
        elapsed = f" | ⏱ {elapsed_s:.1f}s"

    elements.append({
        "tag": "markdown",
        "content": f"{action_label} | {proposal.agent_name}{elapsed}",
        "text_size": "notation",
    })

    return build_card_wrapper(
        header_title=f"{icon} {action_label}: {proposal.title[:50]}",
        header_template=header_color,
        elements=elements,
        mobile_optimize=True,
    )
