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
    "exit_ttadk": {"text": "🚪 退出TTADK", "type": "default"},
    "enter_coco": {"text": "🤖 Coco模式", "type": "primary"},
    "enter_claude": {"text": "🔮 Claude模式", "type": "default"},
    "enter_ttadk": {"text": "🎮 TTADK模式", "type": "default"},
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
}
