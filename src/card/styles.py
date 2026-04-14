from dataclasses import dataclass


@dataclass
class ProjectTheme:
    name: str
    color: str
    emoji: str
    header_template: str


THEMES = {
    "green": ProjectTheme("green", "green", "🟢", "green"),
    "blue": ProjectTheme("blue", "blue", "🔵", "blue"),
    "purple": ProjectTheme("purple", "purple", "🟣", "purple"),
    "orange": ProjectTheme("orange", "orange", "🟠", "orange"),
    "red": ProjectTheme("red", "red", "🔴", "red"),
    "turquoise": ProjectTheme("turquoise", "turquoise", "🩵", "turquoise"),
    "violet": ProjectTheme("violet", "violet", "🟣", "violet"),
    "indigo": ProjectTheme("indigo", "indigo", "🟣", "indigo"),
    "carmine": ProjectTheme("carmine", "carmine", "🔴", "carmine"),
    "wathet": ProjectTheme("wathet", "wathet", "🔵", "wathet"),
    "grey": ProjectTheme("grey", "grey", "⚪", "grey"),
    "yellow": ProjectTheme("yellow", "yellow", "🟡", "yellow"),
}

# Engine Style Configuration
ENGINE_STYLES = {
    "loop": {
        "color": "indigo",
        "icon": "♾️",
        "label_static": "Loop Engine",
        "meta_separator": "\n",
        "features": {"history_button": True},
    },
    "spec": {
        "color": "green",
        "icon": "🧠",
        "label_format": "Deep Agent ({name})",
        "meta_separator": " · ",
        "features": {"history_button": False},
    },
    "claude": {
        "color": "violet",
        "icon": "🧠",
        "label_format": "Deep Agent ({name})",
        "meta_separator": " · ",
        "features": {"history_button": False},
    },
    "default": {
        "color": "turquoise",
        "icon": "🧠",
        "label_format": "Deep Agent ({name})",
        "meta_separator": " · ",
        "features": {"history_button": False},
    },
}

# Button Configuration
BUTTON_CONFIG = {
    # Deep/Loop Engine Buttons
    "pause": {"text": "⏸️ 暂停", "type": "default"},
    "stop": {"text": "🛑 停止", "type": "danger"},
    "resume": {"text": "▶️ 继续", "type": "primary"},
    "expand": {"text": "🔽 展开日志", "type": "default"},
    "collapse": {"text": "🔼 收起日志", "type": "default"},
    "expand_ac": {"text": "🔽 展开验收标准", "type": "default"},
    "collapse_ac": {"text": "🔼 收起验收标准", "type": "default"},
    "mode_full": {"text": "👁️ 当前: 完整", "type": "default"},
    "mode_compact": {"text": "👁️ 当前: 精简", "type": "default"},
    "history": {"text": "📜 历史", "type": "default"},
    # Mode Switch & Project Buttons
    "exit_claude": {"text": "🚪 退出Claude", "type": "default"},
    "exit_coco": {"text": "🚪 退出Coco", "type": "default"},
    "exit_gemini": {"text": "🚪 退出Gemini", "type": "default"},
    "exit_ttadk": {"text": "🚪 /exit 退到智能模式", "type": "default"},
    "enter_coco": {"text": "🤖 Coco模式", "type": "primary"},
    "enter_claude": {"text": "🔮 Claude模式", "type": "default"},
    "enter_gemini": {"text": "✨ Gemini模式", "type": "default"},
    "enter_ttadk": {"text": "🎮 TTADK模式", "type": "default"},
    "switch_ttadk_tool": {"text": "🔧 切换TTADK工具", "type": "primary"},
    "switch_project": {"text": "🔄 切换项目", "type": "default"},
}

# UI Text Constants
UI_TEXT = {
    # Core Card UI
    "log_truncated_warning": "\n> ⚠️ **日志内容过长，已被截断**\n> 🔍 完整日志请查看服务器本地文件\n> (仅显示末尾内容)...\n",
    "claude_mode_title": "🔮 Claude 编程模式",
    "coco_mode_title": "编程模式",
    "smart_mode_title": "智能模式",
    "project_dir_label": "📂 项目目录: `{path}`",
    "image_alt_text": "图片 {index}",
    "time_just_now": "刚刚",
    "time_mins_ago": "{minutes} 分钟前",
    "time_hours_ago": "{hours} 小时前",
    "time_days_ago": "{days} 天前",
    # Deep Engine
    "deep_cmd_help_usage": "📝 请提供需求描述\n\n用法: `/deep <你的需求描述>`\n\n例如: `/deep 帮我写一个 Python 爬虫，爬取豆瓣电影 Top250`",
    "deep_cmd_update_usage": "📝 请提供上下文信息\n\n用法: `/deep_update <上下文描述>`\n\n例如: `/deep_update 数据库改用 PostgreSQL 而不是 SQLite`",
    "deep_cmd_unknown": "❓ 未知的 Deep 命令\n\n可用命令:\n• `/deep <需求>` - 启动 Deep Agent\n• `/deep_update <上下文>` - 注入执行上下文\n• `/deep_status` - 查看当前项目进度\n• `/deep_status --all` - 查看所有项目 Deep Agent 看板\n• `/stop_deep` - 停止当前项目任务\n• `/stop_deep --all` - 停止所有项目任务",
    "deep_task_exists": "⚠️ 当前项目已有 Deep Agent 任务在执行中\n\n发送 `/deep_status` 查看进度\n发送 `/stop_deep` 停止任务",
    "deep_no_task_running": "⚠️ 当前没有正在运行的 Deep Agent 任务\n\n请先使用 `/deep <需求>` 启动任务，或使用 `/deep_status all` 查看所有项目任务",
    "deep_board_empty": "当前没有 Deep Agent 任务\n\n发送 `/deep <需求>` 开始一个复杂任务",
    "deep_stop_all_success": "🛑 已发送停止信号：{count} 个 Deep Agent 任务将在当前步骤完成后停止",
    "deep_no_active_tasks": "📊 当前没有正在执行的 Deep Agent 任务",
    # Loop Engine
    "loop_cmd_help_usage": "📝 请提供产品诉求\n\n用法: `/loop <你的需求描述>`\n\n例如: `/loop 实现用户登录注册功能，支持邮箱和手机号`\n\n可用命令:\n• `/loop <需求>` - 启动 Loop 模式\n• `/loop_guide <引导>` - 注入引导信息\n• `/loop_status` - 查看进度\n• `/loop_pause` - 暂停迭代\n• `/loop_resume` - 恢复迭代\n• `/stop_loop` - 停止 Loop",
    "loop_cmd_guide_usage": "📝 请提供引导信息\n\n用法: `/loop_guide <引导描述>`\n\n例如: `/loop_guide 优先实现邮箱注册功能`",
    "loop_cmd_unknown": "❓ 未知的 Loop 命令",
    "loop_task_exists": "⚠️ 当前项目已有 Loop 任务在执行中\n\n发送 `/loop_status` 查看进度\n发送 `/stop_loop` 停止任务",
    "loop_no_task_running": "⚠️ 当前没有正在运行的 Loop 任务\n\n请先使用 `/loop <需求>` 启动任务",
    "loop_status_empty": "当前没有 Loop 任务\n\n发送 `/loop 你的需求` 开始迭代式开发",
    # Spec Engine
    "spec_cmd_guide_usage": "📝 请提供引导信息\n\n用法: `/spec_guide <引导描述>`\n\n例如: `/spec_guide 优先考虑性能优化`",
    "spec_cmd_help_usage": (
        "📋 **Spec 模式：结构化开发闭环**\n\n"
        "用法：`/spec <你的需求描述>`\n"
        "示例：`/spec 实现用户登录注册功能，支持邮箱和手机号`\n\n"
        "**Spec vs Deep vs Loop**\n"
        "- Spec：按 `Spec→Plan→Task→Build→Review` 产出结构化产物并迭代收敛\n"
        "- Deep：一次性深度拆解并执行一个复杂任务（更偏单次冲刺）\n"
        "- Loop：以验收标准为中心做多轮迭代闭环（更偏持续推进达标）\n\n"
        "**最小示例（推荐命令组合）**\n"
        "- Web：`/spec 做一个登录页+登录接口` → `/spec_status` → `/spec_guide 优先补测试与错误提示`\n"
        "- API：`/spec 新增 /v1/users 查询接口` → `/spec_status`\n"
        "- 脚本：`/spec 写一个批量重命名脚本，支持dry-run` → `/spec_status`\n\n"
        "**可用命令**\n"
        "- `/spec <需求>`：启动\n"
        "- `/spec_guide <引导>`：补充约束/偏好（下轮生效）\n"
        "- `/spec_status`：查看进度\n"
        "- `/spec_history`：查看 spec 文件与循环历史\n"
        "- `/spec_metrics`：查看目标达成度与指标变化\n"
        "- `/spec_config`：查看 Spec 长程配置（阈值/保留策略）\n"
        "- `/spec_export`：导出当前 Spec/Plan 报告\n"
        "- `/spec_save`：立即落盘保存状态（用于断点续传）\n"
        "- `/spec_pause`：暂停\n"
        "- `/spec_resume`：恢复\n"
        "- `/spec_recover`：列出或恢复异常中断的任务（需指定 Task ID）\n"
        "- `/stop_spec`：停止\n"
    ),
    # Generic Engine Lifecycle
    "engine_no_active_task": "当前没有正在执行的 {engine_prefix} 任务",
    "engine_multi_resume_conflict": "⚠️ 有多个项目存在可恢复的 {engine_prefix} 任务，请查看状态后切换项目再恢复",
    "engine_no_resumable_task": "当前没有可恢复的 {engine_prefix} 任务",
    "engine_multi_stop_conflict": "⚠️ 有多个项目正在执行 {engine_prefix} 任务，请先切换项目再停止",
    "engine_stop_no_active": "📊 当前没有正在执行的 {engine_prefix} 任务",
    # System Commands
    "system_help_deep_prompt": "🧠 启动 Deep Engine\n\n请发送: `/deep <你的需求>`\n\n例如: `/deep 帮我重构 src/feishu 模块`",
    "system_help_project_section": "\n\n📋 **项目管理命令**\n• `/projects` - 查看项目看板\n• `/new 名称 路径` - 创建新项目\n• `/switch 名称` - 切换项目\n• `/status` - 查看所有引擎任务状态（Deep/Loop/Spec）\n• `/status <task_id>` - 查看指定任务详情\n• `/diff` - 查看最近两次版本变更（Diff 报告）",
    "system_new_project_usage": "用法: `/new 项目名 [路径]`",
    "system_current_project": "当前项目: **{name}**\n\n{help}{project_help}",
    "system_help_default": "{help}{project_help}",
    "system_mode_smart": "🧠 智能模式",
    "system_mode_coco": "🤖 Coco 编程模式",
    "system_mode_claude": "🔮 Claude 编程模式",
    "system_mode_ttadk": "🎮 TTADK 多工具模式",
    "system_ttadk_refresh_success": "✅ 已触发 TTADK 模型列表强制刷新",
    "system_ttadk_no_tool": "⚠️ 未指定 TTADK 工具，建议先发送 `/ttadk` 选择工具",
    "system_ttadk_select_tool_error": "❌ 设置 TTADK 工具失败: {tool}",
    "system_ttadk_refresh_error": "❌ 刷新 TTADK 模型列表失败",
    "system_ttadk_get_tools_error": "❌ 获取 TTADK 工具列表失败: {error}",
    "system_ttadk_model_warning": "⚠️ TTADK 模型列表可能不完整/不可信: {warnings}",
    "system_ttadk_switching_model": "🔄 正在切换到模型: {model}...",
    "system_ttadk_set_model_error": "❌ 设置 TTADK 模型失败: {model}",
    "system_ttadk_handler_uninitialized": "❌ TTADK 处理器未初始化",
    "system_already_in_mode": "🧠 当前已经在智能模式中",
    "system_ttadk_info_header": "**🎮 TTADK 当前状态**\n",
    "system_ttadk_info_footer": "\n使用 `/ttadk` 切换工具或模型",
    # Project Commands
    "project_create_success": "✅ 项目 **{name}** 创建成功\n\n📁 路径: `{path}`",
    "project_create_error": "❌ 创建项目失败: {error}",
    "project_switch_success": "✅ 已切换到项目: **{name}**\n\n📁 路径: `{path}`",
    "project_switch_error": "❌ 切换项目失败: {error}",
    "project_not_found": "❌ 未找到项目: {name}",
    "project_close_success": "✅ 项目 **{name}** 已关闭",
    "project_close_error": "❌ 关闭项目失败: {error}",
    "project_dir_info": "📂 **项目目录**: `{root}`\n📁 **工作目录**: `{cwd}`",
    "project_dir_switched": "✅ 已切换到: `{path}`",
    "project_dir_not_exist": "目录不存在: {path}",
    "project_board_title": "📋 项目看板",
    "project_board_empty": "当前没有活动项目\n\n发送 `/new 项目名 [路径]` 创建新项目",
    "project_board_active_section": "**🟢 当前活动项目**",
    "project_board_other_section": "**⚪ 其他项目**",
    # ── Programming Mode Prompts (centralized) ──
    "mode_enter_thread_msg": "{emoji} 已开启{name}编程模式\n\n📝 发送你的编程需求，将自动创建编程话题\n\n说「退出模式」或发送 `/exit` 退出",
    "mode_enter_msg": "{emoji} 已进入{name}编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出",
    "mode_enter_no_project_msg": "{emoji} 已进入 {name} 编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出",
    "mode_already_in_thread_msg": "已开启{name}编程模式\n\n直接发送你的编程需求，将自动创建编程话题\n\n说「退出模式」或发送 /exit 退出",
    "mode_already_in_msg": "已经在{name}编程模式中\n\n{info}\n\n说「退出模式」或发送 /exit 退出",
    "mode_exit_msg": "👋 已退出{name}编程模式\n\n会话已保存，下次可以恢复\n\n当前为 🧠 智能模式",
    "mode_exit_pending_msg": "👋 已退出{name}编程模式\n\n当前为 🧠 智能模式",
    "mode_not_in_msg": "当前不在 {name} 模式中",
    "mode_thinking_msg": "{emoji} {name} 正在思考...",
    "mode_resume_msg": "🔄 已恢复 {name} 会话\n\n• 会话 ID: `{session_id}`\n• 历史对话: {query_count} 条\n\n{hint}",
    "mode_resume_hint_default": "继续之前的对话吧！",
    "mode_resume_hint_ttadk": "当前模式：🎮 TTADK（可点「切换TTADK工具」，或发送 `/exit` 退回智能模式）",
    "mode_session_fail_msg": "{name} 会话启动失败，请重新发送 /{cmd} 开始",
    "ttadk_extra_hint": "\n\n可点击「切换TTADK工具」重新选择工具链",
    # ── Deep Engine Card Prompts ──
    "deep_error_no_detail": "发生错误 (无详细信息)",
    "deep_error_expand_hint": '\n> (更多错误详情请点击下方"展开日志"按钮)...',
    "deep_executing_placeholder": "正在执行...",
    "deep_content_expand_hint": '\n> (更多内容请点击下方"展开日志"按钮)',
    "deep_folded_lines_hint": "...(已折叠 {count} 行)...\n",
    "deep_ac_expand_hint": '\n> (更多验收标准请点击"展开验收标准"按钮)...',
    # ── Shell Truncation ──
    "shell_truncated": "\n...(已截断)...",
    # ── ws_client Prompts ──
    "ws_session_fail_msg": "⚠️ {name} 会话启动失败，已退回智能模式，请重新发送 /{cmd} 重试",
    "ws_thread_pending_msg": "📝 当前已开启{name}编程模式\n\n请发送你的编程需求，将自动创建编程话题",
    "ws_topic_hint_msg": "💡 当前话题已在编程模式中，直接发送你的需求即可\n\n如需切换工具，请在主对话中发送对应命令创建新话题",
    "ws_exit_deferred_msg": "✅ 已收到 /exit，将在当前任务完成后退出（不中断执行）",
    "ws_active_topic_msg": "💡 你有一个活跃的 {name} 编程话题正在进行中\n\n请在话题中回复继续对话\n如需新建编程环境，请先发送对应的编程模式命令（如 /coco）",
}

# ──────────────────────────────────────────────────────────────
# Centralized Thresholds — single source of truth for all
# truncation / folding / pagination limits across the project.
# ──────────────────────────────────────────────────────────────
THRESHOLDS = {
    # Card content element max characters (core.py _build_content_element)
    "CONTENT_MAX_CHARS": 25000,
    # Deep/Loop/Spec engine card folding — compact mode line threshold
    "COMPACT_LINE_THRESHOLD": 15,
    # Deep/Loop/Spec engine card folding — full mode line threshold
    "FULL_LINE_THRESHOLD": 50,
    # Acceptance criteria folding line threshold
    "AC_LINE_THRESHOLD": 10,
    # Compact mode long-line character fallback
    "COMPACT_CHAR_FALLBACK": 1500,
    # Shell command stdout max characters
    "SHELL_STDOUT_MAX": 16000,
    # Shell command stderr max characters
    "SHELL_STDERR_MAX": 8000,
    # BaseRenderer collapsible section item threshold
    "COLLAPSE_ITEM_THRESHOLD": 8,
    # BaseRenderer collapsible section long-text line threshold
    "COLLAPSE_LINE_THRESHOLD": 30,
    # BaseRenderer collapsible section display lines (when folded)
    "COLLAPSE_DISPLAY_LINES": 15,
    # Streaming card default visible characters
    "STREAMING_VISIBLE_CHARS": 25000,
    # Streaming card pagination step
    "PAGINATION_STEP": 5000,
}
