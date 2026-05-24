"""Welcome card template for Slock Engine.

Provides the welcome card sent inside newly created Slock team groups.
"""

from __future__ import annotations

from .common import build_card_wrapper

__all__ = ["build_welcome_card"]


def build_welcome_card(*, team_name: str) -> dict:
    """Build a welcome card sent inside the newly created Slock team group."""
    content = (
        f"🎭 **Slock 协作团队「{team_name}」已就绪**\n\n"
        "💬 **直接说就行**:\n"
        "• 「创建一个编码角色」 — 创建虚拟 Agent\n"
        "• 「看看谁在」 — 查看所有角色\n"
        "• 「把代码审查交给 reviewer」 — 分配任务\n"
        "• 「看看任务进度」 — 查看任务看板\n"
        "• 「让 coder 和 reviewer 讨论一下」 — 触发讨论\n\n"
        "---\n"
        "📎 *也支持斜杠命令*: `/new-role`、`/role list`、`/task assign`、`/memory @角色名`、`/discuss 主题`、`/slock help`"
    )
    elements: list[dict] = [{"tag": "markdown", "content": content}]
    return build_card_wrapper(
        header_title=f"👋 欢迎加入 {team_name}",
        header_template="indigo",
        elements=elements,
        mobile_optimize=True,
    )
