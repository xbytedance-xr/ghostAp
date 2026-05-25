"""Command card templates for Slock Engine (mobile-optimized).

Provides command hub and panel cards for the /slock entry point,
using build_card_wrapper with mobile optimization enabled.

Cards:
- build_command_hub_card: Entry-point hub with 4 grouped action panels
- build_command_panel_card: Compact command panel with core actions
- build_command_panel_extended_card: Extended panel with input forms
"""

from __future__ import annotations

from .common import (
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_responsive_layout,
)

__all__ = [
    "build_command_hub_card",
    "build_command_panel_card",
    "build_command_panel_extended_card",
]


def build_command_hub_card(*, channel_id: str = "") -> dict:
    """Build the /slock entry-point hub card with 4 grouped action panels.

    Groups:
    1. Agent 管理 (create role, list roles, role info, remove role)
    2. 任务管理 (assign task, list tasks, task status)
    3. 团队管理 (create team, list teams, dissolve team)
    4. 系统控制 (status, stop, council, help)
    """

    def _hub_btn(label: str, command: str, *, style: str = "default") -> dict:
        value = {"action": "slock_hub_cmd", "cmd": command, "channel_id": channel_id}
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }

    groups = [
        {
            "title": "\U0001f916 Agent 管理",
            "buttons": [
                _hub_btn("\u2795 新建角色", "/new-role"),
                _hub_btn("\U0001f4cb 角色列表", "/role list"),
                _hub_btn("\u2139\ufe0f 角色详情", "/role info"),
                _hub_btn("\U0001f5d1 移除角色", "/role remove", style="danger"),
            ],
        },
        {
            "title": "\U0001f4dd 任务管理",
            "buttons": [
                _hub_btn("\U0001f4cb 任务列表", "/task list"),
                _hub_btn("\U0001f4ca 任务状态", "/task status"),
            ],
        },
        {
            "title": "\U0001f465 团队管理",
            "buttons": [
                _hub_btn("\u2795 新建团队", "/new-team"),
                _hub_btn("\U0001f4cb 团队列表", "/team list"),
                _hub_btn("\U0001f5d1 解散团队", "/team dissolve", style="danger"),
            ],
        },
        {
            "title": "\u2699\ufe0f 系统控制",
            "buttons": [
                _hub_btn("\U0001f4ca 状态面板", "/slock status"),
                _hub_btn("\U0001f3db Council", "/council"),
                _hub_btn("\u23f9 停止引擎", "/slock stop", style="danger"),
                _hub_btn("\u2753 帮助", "/slock help"),
            ],
        },
    ]

    elements: list[dict] = []
    for group in groups:
        # Group title
        elements.append({
            "tag": "markdown",
            "content": f"**{group['title']}**",
        })
        # Buttons via responsive layout (mobile-friendly vertical stacking for >2 buttons)
        elements.extend(build_responsive_layout(group["buttons"], mobile_force_vertical=True))
        # Divider between groups (except last)
        if group != groups[-1]:
            elements.append({"tag": "hr"})

    return build_card_wrapper(
        header_title="\U0001f39b Slock 命令面板",
        header_template="indigo",
        elements=elements,
        mobile_optimize=True,
    )


def build_command_panel_card(*, channel_id: str = "", project_id: str = "") -> dict:
    """Build a compact command panel with core actions and a 'more' button.

    Primary level shows 4 quick-action buttons + expand trigger.
    Extended forms are served via build_command_panel_extended_card().
    """
    elements: list[dict] = []

    # Primary quick-action buttons (first screen, max 3)
    primary_buttons = [
        build_callback_button("\U0001f3e0 查看团队", "slock_cmd_team_list", channel_id=channel_id, project_id=project_id, button_type="default"),
        build_callback_button("\U0001f3ad 查看角色", "slock_cmd_role_list", channel_id=channel_id, project_id=project_id, button_type="default"),
        build_callback_button("\U0001f4cb 任务面板", "slock_cmd_task_list", channel_id=channel_id, project_id=project_id, button_type="default"),
    ]
    elements.extend(build_responsive_layout(primary_buttons))

    # Secondary buttons in collapsible panel (default collapsed for mobile)
    secondary_buttons = [
        build_callback_button("\U0001f9e0 查看记忆", "slock_cmd_memory", channel_id=channel_id, project_id=project_id, button_type="default"),
        build_callback_button("\U0001f5e3 发起讨论", "slock_cmd_discuss", channel_id=channel_id, project_id=project_id, button_type="primary"),
    ]
    collapsible_elements = build_responsive_layout(secondary_buttons)
    # Add role naming guidance inside collapsible panel
    collapsible_elements.append({
        "tag": "markdown",
        "content": "\U0001f4cc **角色名语法提示**: 带空格的角色名请使用 `@role` 或双引号包裹（如 `\"Senior Coder\"`），避免解析歧义。",
    })
    elements.append(build_collapsible_panel(
        "\U0001f4c2 更多快捷操作",
        collapsible_elements,
        expanded=False,
        vertical_spacing="8px",
    ))

    # Expand button for extended operations
    elements.append({"tag": "hr"})
    elements.extend(build_responsive_layout([
        build_callback_button(
            "\u2699\ufe0f 更多操作...",
            "slock_cmd_panel_extended",
            channel_id=channel_id,
            project_id=project_id,
            button_type="default",
        ),
    ]))

    # Bottom hint
    elements.append({
        "tag": "markdown",
        "content": "<font color='grey'>\U0001f4a1 也可直接输入命令：/team、/role、/task、/council、/discuss、/memory</font>",
    })

    return build_card_wrapper(
        header_title="\U0001f4cb Slock 命令面板",
        header_template="blue",
        elements=elements,
        mobile_optimize=True,
    )


def build_command_panel_extended_card(*, channel_id: str = "", project_id: str = "") -> dict:
    """Build the extended command panel with action-based input forms.

    This is the second-level card triggered by '更多操作' button.
    Contains team creation, role creation, and council forms.
    Uses standard 'action' elements (not 'form') for Feishu Schema 2.0 compatibility.
    """
    elements: list[dict] = []

    # Team creation action
    elements.append({"tag": "markdown", "content": "**\U0001f3e0 创建团队**"})
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "input",
                "name": "team_name",
                "placeholder": {"tag": "plain_text", "content": "输入团队名称"},
                "width": "fill",
            },
            build_callback_button(
                "创建团队",
                "slock_form_new_team",
                channel_id=channel_id,
                project_id=project_id,
                button_type="primary",
            ),
        ],
    })

    # Role creation action
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "**\U0001f3ad 创建角色**"})
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "input",
                "name": "role_name",
                "placeholder": {"tag": "plain_text", "content": "输入角色名称（如: coder-小明）"},
                "width": "fill",
            },
            build_callback_button(
                "创建角色",
                "slock_form_new_role",
                channel_id=channel_id,
                project_id=project_id,
                button_type="primary",
            ),
        ],
    })

    # Council action
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "**\U0001f9d1\u200d\u2696\ufe0f Council 评审**"})
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "input",
                "name": "council_topic",
                "placeholder": {"tag": "plain_text", "content": "输入评审议题"},
                "width": "fill",
            },
            build_callback_button(
                "发起评审",
                "slock_form_council",
                channel_id=channel_id,
                project_id=project_id,
                button_type="primary",
            ),
        ],
    })

    # Bottom hint
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "content": "<font color='grey'>\U0001f4a1 返回主面板：输入 /slock</font>",
    })

    return build_card_wrapper(
        header_title="\u2699\ufe0f Slock 扩展操作",
        header_template="blue",
        elements=elements,
        mobile_optimize=True,
    )
