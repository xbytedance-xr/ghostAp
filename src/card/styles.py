from dataclasses import dataclass


@dataclass
class ProjectTheme:
    name: str
    color: str
    emoji: str
    header_template: str


# 优化后的主题配色系统，确保 WCAG AA 级对比度（至少 4.5:1）
# 选择更适合移动端显示的、对比度更好的颜色
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
    # 深色主题变体 - 为深色模式优化的配色
    "dark_green": ProjectTheme("dark_green", "dark_green", "🌲", "dark_green"),
    "dark_blue": ProjectTheme("dark_blue", "dark_blue", "🌙", "dark_blue"),
    "dark_purple": ProjectTheme("dark_purple", "dark_purple", "🪻", "dark_purple"),
    "dark_orange": ProjectTheme("dark_orange", "dark_orange", "🍂", "dark_orange"),
    "dark_red": ProjectTheme("dark_red", "dark_red", "🍎", "dark_red"),
    "dark": ProjectTheme("dark", "dark", "⚫", "dark"),
}

# 深色主题名称列表（不参与自动分配）
DARK_THEME_NAMES = {"dark_green", "dark_blue", "dark_purple", "dark_orange", "dark_red", "dark"}


def get_available_themes(include_dark: bool = False) -> dict[str, ProjectTheme]:
    """获取可用的主题列表。
    
    Args:
        include_dark: 是否包含深色主题，默认为 False（深色主题不参与自动分配）
    
    Returns:
        主题字典
    """
    if include_dark:
        return THEMES.copy()
    return {name: theme for name, theme in THEMES.items() if name not in DARK_THEME_NAMES}

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

# ──────────────────────────────────────────────────────────────
# Collapsible Panel Styles — aligned with pokoclaw tool-calls.ts
# ──────────────────────────────────────────────────────────────
PANEL_STYLES = {
    "corner_radius": "5px",
    "padding": "8px 8px 8px 8px",
    "vertical_spacing": "8px",
    "border_normal": "grey",
    "border_failed": "red",
    "border_history": "blue",
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
    "stop_danger": {"text": "⏹ 停止", "type": "danger"},
}

# UI Text Constants
UI_TEXT = {
    # Core Card UI
    "log_truncated_warning": "\n> ⚠️ **日志内容过长，已被截断**\n> 🔍 完整日志请查看服务器本地文件\n> (仅显示末尾内容)...\n",
    "claude_mode_title": "🔮 Claude 编程模式",
    "gemini_mode_title": "✨ Gemini 编程模式",
    "coco_mode_title": "编程模式",
    "smart_mode_title": "智能模式",
    "project_dir_label": "📂 项目目录: `{path}`",
    "image_alt_text": "图片 {index}",
    "time_just_now": "刚刚",
    "time_secs_ago": "{seconds}秒前",
    "time_mins_ago": "{minutes}分钟前",
    "time_mins_secs_ago": "{minutes}分{seconds}秒前",
    "time_hours_ago": "{hours}小时前",
    "time_hours_mins_ago": "{hours}时{minutes}分前",
    "time_days_ago": "{days}天前",
    "duration_hours_mins_secs": "{hours}小时{minutes}分{seconds}秒",
    "duration_mins_secs": "{minutes}分{seconds}秒",
    "duration_secs": "{seconds}秒",
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
    "system_mode_aiden": "🎯 Aiden 编程模式",
    "system_mode_codex": "💻 Codex 编程模式",
    "system_mode_gemini": "✨ Gemini 编程模式",
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
    "system_ttadk_refresh_label_tool": "工具: `{tool}`",
    "system_ttadk_refresh_label_source": "来源: `{source}`",
    "system_ttadk_refresh_label_warning": "⚠️ 警告: {warnings}",
    "system_ttadk_refresh_label_diag": "诊断: attempts={attempts}",
    "system_ttadk_refresh_footer": "\n最短修复路径：若仍不可用，请确认在项目目录执行过 `ttadk init`，或切换 tool 后重试。",
    "system_switching_to": "🔄 正在切换到 {tool} / {model}...",
    "system_already_in_mode": "🧠 当前已经在智能模式中",
    "system_not_set": "未设置",
    "system_default": "默认",
    "system_no_active_project": "⚠️ 当前没有活跃项目，请先创建或切换到一个项目。\n\n发送 `/projects` 查看项目看板",
    "system_auto": "自动",
    "system_on": "开启",
    "system_off": "关闭",
    "system_no_project": "无",
    "system_no_project_display": "无项目",
    "system_unknown_error": "未知错误",
    "system_unknown_unit": "未知单元",
    "system_unknown_execution_error": "未知执行异常",
    "system_mode_label": " 模式",
    "system_theme_color_label": "主题色: ",
    "system_history_record_title": " · 历史记录",
    "system_btn_prev_page": "⬅️ 上一页",
    "system_btn_next_page": "➡️ 下一页",
    "system_btn_back_status": "📊 返回状态",
    "system_status_completed": "已完成",
    "system_status_executing": "执行中",
    "system_status_partial_failed": "部分失败",
    "system_status_ready": "就绪",
    "system_status_preparing": "准备中",
    "system_label_current_tool": "🔧 **当前工具**",
    "system_label_current_model": "🤖 **当前模型**",
    "system_status_available": "✅ 可用",
    "system_status_unavailable": "❌ 不可用",
    "system_never_used": "从未使用",
    "system_unknown": "未知",
    "acp_result_text_header": "📝 输出文本",
    "acp_result_plan_header": "📋 计划",
    "acp_result_tools_header": "🛠️ 工具调用",
    "acp_result_tool_results_header": "📦 工具结果(本地记录)",
    "acp_result_files_header": "🗂️ 改动文件",
    "system_ttadk_unavailable": "TTADK 暂不可用",
    "system_ttadk_list_load_error": "暂时无法加载 TTADK 工具列表（{error}）",
    "system_ttadk_model_load_error": "暂时无法加载 TTADK 模型列表（{error}）",
    "system_ttadk_status_banner": "🎮 **TTADK 状态** · 工具: `{tool}` · 模型: `{model}` · 自动执行: `{yolo}`",
    "system_ttadk_info_header": "**🎮 TTADK 当前状态**\n",
    "system_ttadk_info_footer": "\n使用 `/ttadk` 切换工具或模型",
    "system_acp_tool_desc_coco": "字节跳动 AI",
    "system_acp_tool_coco_display": "字节跳动 AI 编程",
    "system_acp_tool_desc_claude": "Anthropic AI",
    "system_acp_tool_desc_aiden": "Aiden CLI",
    "system_acp_tool_aiden_display": "AI 编程助手",
    "system_acp_tool_desc_codex": "OpenAI Codex",
    "system_acp_tool_desc_gemini": "Google Gemini CLI",
    "system_arg_error": "参数错误",
    "system_acp_unsupported_tool": "不支持的 ACP 工具: {tool_name}",
    "system_acp_no_available_tools": "未检测到可用 ACP 工具",
    "system_acp_select_tool_prompt": "请选择 ACP 工具",
    "system_acp_select_model_prompt": "请选择 ACP 模型",
    "system_acp_querying_models": "🔍 正在查询 {tool_name} 支持的模型...",
    "system_acp_get_models_failed": "获取 {tool_name} 模型列表失败，请稍后重试",
    "system_acp_specify_model_prompt": "请指定模型名称，例如：{example}",
    "system_model_usage_example": "\n• `/model list` — 查看可用模型\n• `/model <name>` — 切换到指定模型",
    "system_worktree_no_selection_error": "请至少选择一个工具-模型组合",
    "system_worktree_created_prompt": "Worktree 已创建，请发送任务目标开始并行执行。",
    "system_worktree_create_failed": "Worktree 创建失败: {error}",
    "system_worktree_cleanup_success": "✅ 所有 Worktree 已清理完成",
    "system_worktree_cleanup_warnings": "⚠️ 清理完成，但以下 Worktree 存在未处理的变更：\n{details}\n\n请手动检查后再次清理。",
    "system_worktree_goal_required": "⚠️ 请先在输入框中填写任务目标",
    "system_worktree_unit_running_error": "⚠️ 存在正在执行的单元，请等待完成后再重试",
    "system_worktree_retry_starting": "🔄 正在重试失败单元...",
    "system_worktree_retry_completed": "重试完成",
    "system_worktree_retry_goal": "重试执行",
    "system_worktree_confirm_title": "🌳 Worktree — 确认组合",
    "system_worktree_confirm_header": "**即将启动以下工具-模型组合：**\n",
    "system_worktree_confirm_banner": "请在下方输入您的任务目标，点击按钮一键开启执行",
    "system_worktree_btn_confirm": "确认并开始执行",
    "system_worktree_btn_reselect": "重新选择",
    "system_worktree_progress_title": "🌳 Worktree — {status}",
    "system_worktree_progress_header": "**执行进度：**\n",
    "system_worktree_progress_banner": "所有单元已就绪，请录入总任务目标并开始执行",
    "system_worktree_btn_execute": "🚀 开始并行执行",
    "system_worktree_btn_retry": "🔄 重试失败单元",
    "system_worktree_btn_merge": "🔀 合并到 {base}",
    "system_worktree_btn_cleanup": "🧹 清理 Worktree",
    "system_worktree_merge_title": "🌳 Worktree — 合并结果",
    "system_worktree_merge_header": "**合并详情：**\n",
    "system_worktree_merge_success": "✅ 已成功合并到 {base}",
    "system_worktree_merge_failed": "❌ 合并到 {base} 失败: {error}",
    "system_worktree_cleanup_title": "🌳 Worktree — 清理完成",
    "system_worktree_select_tool_error": "未选择工具",
    "system_worktree_project_not_found": "找不到关联的项目，请先通过 /projects 关联一个项目",
    "system_worktree_no_available_tools": "当前环境没有可用的编程工具",
    "system_worktree_select_tool_prompt": "请选择要使用的工具：",
    "system_worktree_select_model_prompt": "为 {tool} 选择模型：",
    "system_worktree_selection_finished_banner": "已选择工具: {tool}",
    "system_dir_changed_title": "目录已切换",
    "system_error_title": "操作失败",
    "system_ttadk_ai_tool_label": "AI Tool",
    "system_coco_status_title": "**🤖 Coco 状态**\n",
    "system_coco_current_model": "当前模型: `{model}`",
    "system_coco_available_models": "\n**可用模型:**",
    "system_error_prompt_title": "⚠️ 错误提示",
    "system_ref_note_prefix": "🔗 关联：",
    "system_rate_limit_title": "⏸️ 限速等待",
    "system_rate_limit_content": "🔄 API 限速触发，自动等待 {wait_seconds} 秒后恢复...\n\n无需操作，任务将自动继续。",
    "system_shell_success_title": "✅ 命令执行成功",
    "system_shell_failed_title": "❌ 命令执行失败",
    "system_shell_no_output": "✅ 命令执行成功（无输出）",
    "system_shell_return_code": "返回码: `{code}`",
    "system_shell_stderr_label": "⚠️ **错误输出**:",
    "system_ttadk_yolo_on": "⚡ 自动执行：开启（点击关闭）",
    "system_ttadk_yolo_off": "⚡ 自动执行：关闭（点击开启）",
    "system_ttadk_select_tool_prompt": "请选择要使用的 TTADK 工具：",
    "system_ttadk_select_tool_placeholder": "选择工具",
    "system_ttadk_tool_select_title": "🔧 TTADK 工具选择",
    "system_ttadk_select_model_prompt": "请为 {tool} 选择模型：",
    "system_ttadk_select_model_hint": "（若列表为空/不全，可点击下方『🔄 刷新模型列表』强制拉取）",
    "system_ttadk_select_model_placeholder": "选择模型",
    "system_ttadk_refresh_btn": "🔄 刷新模型列表",
    "system_ttadk_model_select_title": "🤖 {tool} 模型选择",
    "system_ttadk_combined_select_prompt": "请选择要使用的 TTADK 工具和模型：",
    "system_ttadk_label_tool": "**🔧 工具**",
    "system_ttadk_label_model": "**🤖 模型** (工具: {tool})",
    "system_ttadk_combined_title": "🔧 TTADK 工具与模型选择",
    "system_ttadk_unavailable_title": "⚠️ TTADK 暂不可用",
    "system_ttadk_soft_failure_msg": "⚠️ {reason}\n\n已为你保留选择，可点击继续或稍后重试。",
    "system_ttadk_btn_continue": "继续进入TTADK",
    "system_ttadk_btn_reenter": "🔄 重新进入TTADK",
    "system_acp_tool_select_title": "🧩 ACP 工具选择",
    "system_acp_model_select_title": "🧠 {tool} 模型选择",
    "system_menu_title": "📱 快捷菜单",
    "system_menu_header": "**📱 常用指令菜单**",
    "system_menu_btn_new_project": "➕ 新建项目",
    "system_menu_btn_switch_project": "🔄 切换项目",
    "system_menu_btn_deep_task": "🧠 Deep 任务",
    "system_menu_btn_status": "📊 状态概览",
    "system_menu_btn_ttadk": "🎮 TTADK",
    "system_menu_btn_acp": "🧩 ACP",
    "system_menu_btn_help": "📖 帮助",
    "system_help_title": "📖 GhostAP 使用帮助",
    "system_help_status_header": "**当前状态**  •  {mode}  •  `{cwd}`  •  项目: {project}",
    "system_help_quick_entry": "**⚡ 快捷入口**（点按执行，手机优先）",
    "system_help_section_modes": "🔄 编程模式切换",
    "system_help_section_modes_body": (
        "`/coco` · 进入 Coco 编程模式（字节跳动 AI）\n"
        "`/claude` · 进入 Claude 编程模式（Anthropic AI）\n"
        "`/aiden` · 进入 Aiden 编程模式\n"
        "`/codex` · 进入 Codex 编程模式\n"
        "`/gemini` · 进入 Gemini 编程模式\n"
        "`/ttadk` · 进入 TTADK 多工具编程模式\n"
        "`/exit` · 退出当前编程模式\n"
        "`/coco_info` · `/claude_info` · `/aiden_info` · `/codex_info` · `/ttadk_info` · 查看会话/模型信息"
    ),
    "system_help_section_deep": "🧠 Deep Engine · 复杂任务一次交付",
    "system_help_section_deep_body": (
        "`/deep <需求>` · 启动 Deep Engine\n"
        "`/deep_status` · 查看任务进度\n"
        "`/stop_deep` · 停止任务"
    ),
    "system_help_section_loop": "🔁 Loop Engine · 迭代闭环",
    "system_help_section_loop_body": (
        "`/loop <需求>` · 启动 Loop 模式\n"
        "`/loop_status` · 查看迭代进度\n"
        "`/loop_guide <引导>` · 注入引导信息\n"
        "`/loop_pause` · 暂停迭代  ·  `/loop_resume` · 恢复迭代\n"
        "`/stop_loop` · 停止 Loop"
    ),
    "system_help_section_spec": "📋 Spec Engine · 结构化开发闭环",
    "system_help_section_spec_body": (
        "`/spec <需求>` · 启动  ·  `/spec_status` · 查看进度\n"
        "`/spec_pause` · 暂停  ·  `/spec_resume` · 继续\n"
        "`/spec_guide <引导>` · 补充约束/偏好\n"
        "`/spec_history` · 历史  ·  `/spec_metrics` · 目标达成度\n"
        "`/spec_config` · 配置  ·  `/spec_save` · 立即保存\n"
        "`/spec_export` · 导出报告  ·  `/spec_recover [任务ID]` · 恢复失败任务\n"
        "`/stop_spec` · 停止"
    ),
    "system_help_section_project": "📂 项目管理",
    "system_help_section_project_body": (
        "`/projects` · 查看所有项目\n"
        "`/new <名称> [路径]` · 创建新项目\n"
        "`/switch <名称>` · 切换项目  ·  `/close <名称>` · 关闭项目\n"
        "`/status` · 查看所有引擎任务状态\n"
        "`/diff` · 查看最近两次版本变更"
    ),
    "system_help_section_ttadk": "🤖 TTADK 管理",
    "system_help_section_ttadk_body": (
        "`/ttadk_refresh` · 强制刷新模型列表\n"
        "`/ttadk_info` · 查看当前工具和模型"
    ),
    "system_help_section_worktree": "🌳 Worktree · 多工具并行执行",
    "system_help_section_worktree_body": (
        "`/wt <目标>` · 启动 Worktree 并行执行\n"
        "`/wt` · 进入工具选择流程\n"
        "支持多工具组合并行：选择工具 → 设定目标 → 自动分配 → 并行执行 → 合并结果"
    ),
    "system_help_tips": (
        "**💡 使用提示**\n"
        "1. 手机端优先点按上方 **⚡ 快捷入口**，无需输入指令\n"
        "2. 发送 `/menu` 打开完整快捷菜单；`/tools` 查看可用工具\n"
        "3. 智能模式下可直接输入 Shell 命令或自然语言\n"
        "4. 复杂任务用 `/deep`，迭代任务用 `/loop`，规范交付用 `/spec`，多工具并行用 `/wt`"
    ),
    "system_tools_list_title": "🛠️ 工具选择",
    "system_tools_list_header": "**🔧 可用工具列表**",
    "system_tools_list_footer": "可用工具: {available}/{total} • 点击按钮进入对应模式 • 灰色按钮表示工具不可用",
    "system_tools_status_title": "📋 工具状态",
    "system_tools_status_header": "**📊 工具状态详情**",
    "system_tools_status_item": "{emoji} **{name}**\n   状态: {status}\n   最后使用: {last_used}{active_info}",
    "system_tools_status_active_session": "\n   🔴 活跃会话: {chat_id}",
    "system_tools_status_btn_enter": "进入 {name}",
    "worktree_goal_label": "**任务目标：** {goal}",
    "worktree_goal_placeholder": "输入任务目标（选完工具后自动执行）",
    "worktree_selected_header": "**已选组合：**\n",
    "worktree_btn_finish": "完成选择",
    "worktree_select_tool_title": "🌳 Worktree — 选择工具",
    "worktree_select_tool_prompt": "**请选择一个工具加入 Worktree 组合：**\n",
    "worktree_skip_model_btn": "跳过（使用默认模型）",
    "worktree_select_model_title": "🌳 Worktree — 选择模型",
    "worktree_confirm_header": "**即将启动以下工具-模型组合：**\n",
    "worktree_confirm_banner": "请在下方输入您的任务目标，点击按钮一键开启执行",
    "worktree_input_placeholder": "输入任务目标，点击确认后开始执行",
    "worktree_btn_confirm": "确认并开始执行",
    "worktree_btn_reselect": "重新选择",
    "worktree_confirm_title": "🌳 Worktree — 确认组合",
    "worktree_progress_header": "**执行进度：**\n",
    "worktree_fail_reason": "> 🔍 **失败原因**：{error}",
    "worktree_ready_banner": "所有单元已就绪，请录入总任务目标并开始执行",
    "worktree_btn_execute": "🚀 开始并行执行",
    "worktree_btn_retry": "🔄 重试失败单元",
    "worktree_progress_title": "🌳 Worktree — {status}",
    "worktree_result_header": "**🔀 工作单元结果**\n\n{message}\n\n",
    "worktree_btn_view_merge": "查看集成项",
    "worktree_result_title": "工作单元结果",
    "worktree_merge_entry_header": "**🔀 待集成项**\n\n目标分支: `{base}`\n\n",
    "worktree_merge_entry_title": "待集成项",
    "worktree_cleanup_header": "**目标分支：** `{base}`\n",
    "worktree_merge_result_header": "**合并结果：**",
    "worktree_merge_item": "{icon} {name} — {detail}",
    "worktree_pending_merge_header": "**待集成项：**",
    "worktree_failed_units_header": "**失败单元：**",
    "worktree_failed_unit_item": "❌ **{name}** · {title} — {error}",
    "worktree_failed_unit_item_no_title": "❌ **{name}** — {error}",
    "worktree_failed_overflow": "...及 {count} 个其他失败单元",
    "worktree_btn_merge_partial": "✅ 先合并已完成",
    "worktree_btn_merge_all": "合并所有分支",
    "worktree_btn_cleanup": "清理 Worktree",
    "worktree_cleanup_card_title": "🌳 Worktree — 集成与清理",
    "system_ttadk_switch_tool_error": "暂时无法切换 TTADK 工具到 {tool}",
    "system_ttadk_switch_model_error": "暂时无法切换 TTADK 模型到 {model}",
    # Project Commands
    "project_create_success": "✅ 项目 **{name}** 创建成功\n\n📁 路径: `{path}`",
    "project_create_error": "❌ 创建项目失败: {error}",
    "project_not_found_title": "未找到项目",
    "project_switch_success": "✅ 已切换到项目: **{name}**\n\n📁 路径: `{path}`",
    "project_switch_error": "❌ 切换项目失败: {error}",
    "project_not_found": "❌ 未找到项目: {name}",
    "project_not_found_hint": "❌ 未找到项目: {name}\n\n发送 `/projects` 查看所有项目",
    "project_close_success": "✅ 项目 **{name}** 已关闭",
    "project_close_error": "❌ 关闭项目失败: {error}",
    "project_dir_info_title": "目录信息",
    "project_dir_info": "📂 **项目目录**: `{root}`\n📁 **工作目录**: `{cwd}`",
    "project_dir_info_cwd": "• 📁 工作目录: `{cwd}`",
    "project_dir_switched": "✅ 已切换到: `{path}`",
    "project_dir_switched_banner": "目录已切换到: {path}",
    "project_dir_switched_detail": "当前工作目录已成功更新为 `{path}`。",
    "project_dir_switch_failed_banner": "切换目录失败: {path}",
    "project_dir_switch_failed_detail": "无法切换到目标目录: `{path}`",
    "project_dir_not_exist": "目录不存在: {path}",
    "project_current_header": "📁 **当前项目: {name}**",
    "project_id_label": "• 项目 ID: `{id}`",
    "project_status_label": "• 状态: {emoji} {status}",
    "project_coco_status": "• Coco 模式: {status}",
    "project_claude_status": "• Claude 模式: {status}",
    "project_last_active_label": "• 最后活跃: {time_ago}",
    "project_coco_session_header": "\n\n🤖 **Coco 会话**",
    "project_claude_session_header": "\n\n🔮 **Claude 会话**",
    "project_session_id_label": "• 会话 ID: `{id}`",
    "project_session_count_label": "• 对话数: {count}",
    "project_similar_header": "\n\n**相似项目：**",
    "project_restore_info": "\n\n📋 已恢复上下文: {count} 条记录",
    "project_restore_last_mode": ", 上次模式: {mode}",
    "project_switched_content": "已切换到项目 **{name}**\n\n📂 项目目录: `{root}`{context_info}",
    "project_board_title": "📋 项目看板",
    "project_board_empty": "当前没有活动项目\n\n发送 `/new 项目名 [路径]` 创建新项目",
    "project_board_active_section": "**🟢 当前活动项目**",
    "project_board_other_section": "**⚪ 其他项目**",
    "project_info_card_title": "当前项目",
    "project_status_card_title": "项目状态",
    "project_switch_title": "🔄 项目已切换",
    "diag_current_mode": "**当前模式**: {mode}",
    "diag_no_active_tasks": "暂无正在进行的任务",
    "diag_no_tasks": "暂无任务",
    "diag_project_header": "### {name} {id_info}",
    "diag_task_line": "- {emoji} `{run_id}` {name} ({task_type}){pct}{msg}",
    "diag_unified_status_header": "**引擎任务 ({count})**\n",
    "diag_engine_line": "- {emoji} **{mode}** · {name}{tid} · {info}",
    "diag_status_all_hint": "\n_发送 `/status all` 查看包括已完成的任务_",
    "diag_no_engine_tasks": "当前没有 Deep/Loop/Spec 引擎任务\n\n",
    "diag_engine_launch_hints": "启动任务:\n• `/deep <需求>` — 单次深度执行\n• `/loop <需求>` — 迭代闭环\n• `/spec <需求>` — 结构化开发",
    "diag_task_board_title": "📋 任务看板",
    "diag_no_active_project_tasks": "当前没有活跃项目，无法按项目查看任务。\n\n发送 /projects 查看项目看板",
    "diag_status_iteration": "迭代{iteration}",
    "diag_status_criteria": "标准{criteria}",
    "diag_status_cycle": "循环{cycle}",
    "diag_unified_status_title": "📊 统一状态",
    "diag_task_detail_deep_title": "📊 Deep 任务详情",
    "diag_task_detail_loop_title": "📊 Loop 任务详情",
    "diag_task_detail_spec_title": "📊 Spec 任务详情",
    "diag_diff_no_active_project": "当前没有活跃项目，无法生成 Diff 报告。\n\n发送 /projects 选择项目",
    "diag_diff_report_title": "🧾 Diff 报告",
    "diag_diff_usage_footer": "用法：/diff（最近两版） • /diff current（到当前） • /diff N • /diff A..B",
    "diag_trace_not_found": "未找到关联信息：{key}",
    "diag_trace_replies_header": "### 📨 回复消息 ({count})",
    "diag_trace_runs_header": "### 🧵 任务 run_id ({count})",
    "diag_trace_usage_footer": "提示：/trace <id> 支持 origin/reply/run_id/request_id",
    "diag_trace_title": "🔎 关联查询",
    "diag_step_parsing": "解析参数",
    "diag_step_generating": "生成报告",
    "diag_step_completed": "完成",
    "diag_engine_deep": "Deep",
    "diag_engine_loop": "Loop",
    "diag_engine_spec": "Spec",
    "diag_label_origin_id": "**Origin ID**",
    "diag_label_request_id": "**Request ID**",
    "diag_label_project_id": "**Project ID**",
    "diag_project_id_fmt": "(`{id}`)",
    "diag_task_pct_fmt": " {pct:.0f}%",
    "diag_task_msg_fmt": " — {msg}",
    "diag_task_detail_title": "📊 任务详情",
    "diag_task_name_label": "**任务**: {name}",
    "diag_task_type_label": "**类型**: {type}",
    "diag_task_status_label": "**状态**: {emoji} {status}",
    "diag_task_run_id_label": "**run_id**: `{id}`",
    "diag_task_id_label": "**task_id**: `{id}`",
    "diag_task_progress_label": "**进度**: {msg}",
    "diag_task_not_found": "未找到 task_id: `{id}`\n\n发送 `/status` 查看所有任务",
    "diag_diff_title": "## 🧾 Diff 报告",
    "diag_diff_project_label": "**项目**: {name} (`{id}`)",
    "diag_diff_range_label": "**范围**: v{from_v} → v{to_v}",
    "diag_diff_range_current_label": "**范围**: v{from_v} → 当前",
    "diag_diff_start_reason_label": "**起点原因**: {reason}",
    "diag_diff_end_reason_label": "**终点原因**: {reason}",
    "diag_diff_added_entries_label": "**新增条目**: {count}",
    "diag_diff_no_entries": "✅ 本范围内没有新增上下文条目",
    "diag_diff_file_changes_header": "### 📝 文件变更 ({count})",
    "diag_diff_deep_results_header": "### 🧠 Deep 结果 ({count})",
    "diag_diff_deep_item": "- `{name}`：已完成 {done}/{total} 个任务",
    "diag_diff_mode_changes_header": "### 🔄 模式切换 ({count})",
    "diag_diff_summaries_header": "### 📌 AI 摘要 ({count})",
    "diag_diff_conversations_header": "### 💬 对话片段 ({count})",
    "diag_diff_others_header": "### 📎 其他 ({count})",
    "diag_diff_truncated": "\n…（内容过长已截断）",
    "diag_diff_generating_banner": "🧾 正在生成 Diff 报告...",
    "diag_diff_started_banner": "🧾 已开始生成 Diff 报告...",
    "diag_diff_generating_progress": "🧾 {step}中（{pct}%）...",
    "diag_diff_failed": "生成 Diff 报告失败",
    "diag_diff_exception": "Diff 报告生成异常: {error}",
    "diag_diff_no_record": "该项目暂无上下文记录，无法生成 Diff 报告。",
    "diag_diff_no_bookmarks": "该项目尚无版本书签。\n\n提示：版本书签会在模式切换、项目切换、Deep 完成等关键节点自动创建。",
    "diag_diff_no_current": "该项目尚无版本书签，无法计算 `current` diff。",
    "diag_diff_usage_error": "用法错误：`/diff <A>..<B>`，例如 `/diff 3..5`。",
    "diag_diff_usage_hint": "用法：`/diff [last|current|N|A..B]`，例如 `/diff current` 或 `/diff 2..3`。",
    "diag_diff_version_not_found": "找不到版本 v{vnum}，当前共有 {total} 个版本。",
    "project_board_empty_content": "暂无项目\n\n发送 `/new 项目名 路径` 创建新项目",
    "project_board_total_projects": "共 **{total}** 个项目",
    "project_board_current_marker": " (当前)",
    "project_board_claude_info": " | 🔮 Claude 模式中 (消息数: {count})",
    "project_board_coco_info": " | 🤖 Coco 模式中 (消息数: {count})",
    "project_board_busy_info": " | ⏳ {task_type}",
    "project_board_page_info": "第 {page}/{total} 页",
    "project_board_btn_new": "➕ 新建项目",
    "project_board_btn_switch": "切换到此项目",
    "project_board_btn_continue": "继续开发",
    "project_board_btn_detail": "查看详情",
    "project_board_btn_prev": "⬅️ 上一页",
    "project_board_btn_next": "下一页 ➡️",
    "project_board_btn_refresh": "🔄 刷新",
    "project_created_title": "🎉 新项目已创建",
    "project_btn_start_coco": "🤖 开始 Coco",
    "project_btn_start_claude": "🔮 开始 Claude",
    "project_resume_detected": "🔄 检测到未完成的 {mode} 会话",
    "project_resume_session_id": "• 会话 ID: `{id}`",
    "project_resume_query_count": "• 对话数: {count} 条",
    "project_resume_last_query": "• 最后对话: {query}",
    "project_resume_btn_resume": "🔄 恢复会话",
    "project_resume_btn_new": "➕ 新建会话",
    "project_resume_no_session": "没有可恢复的会话",
    "project_notif_suggestion_header": "💡 **建议下一步:**",
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
    # ── Worktree Engine Card Prompts ──
    "worktree_ready_intercept_hint": "💡 提示：直接发送消息即可作为任务目标开始执行",
    "worktree_start_silent": "🚀 已开始执行: {goal}\n完成后将自动通知，请稍候…",
    "worktree_executing": "⏳ 正在并行执行: {goal}",
    "worktree_still_running": "⏳ 仍在执行中 ({elapsed}min)，请耐心等待…",
    "worktree_executing_live": "🔄 执行中: {goal}",
    "worktree_completed_no_change": "执行完成（无可合并的变更）\n\n可能原因：目标描述不够具体，或工具未产生文件修改。建议检查目标后重试。",
    "worktree_auto_executing_banner": "🚀 正在启动并执行任务...",
    # ── Deep Engine Card Prompts ──
    "deep_error_no_detail": "发生错误 (无详细信息)",
    "deep_error_expand_hint": '\n> (展开日志查看更多)...',
    "deep_executing_placeholder": "正在执行...",
    "deep_content_expand_hint": '\n> (展开日志查看更多)',
    "deep_folded_lines_hint": "...(已折叠 {count} 行)...\n",
    "deep_ac_expand_hint": '\n> (展开验收标准查看更多)...',
    "deep_status_failed_zh": "失败",
    "deep_status_completed_zh": "完成",
    "deep_status_finished_zh": "结束",
    "deep_status_planning_zh": "规划",
    "deep_status_analyzing_zh": "分析",
    # ── Shell Truncation ──
    "shell_truncated": "\n...(已截断)...",
    # ── Streaming Card Collapsible / Continuation ──
    "streaming_continuation_footer": "\n\n---\n⬇️ 后续内容见下方",
    "streaming_continuation_title_suffix": " (续 #{n})",
    "streaming_continuation_initial": "🔄 继续输出...",
    "continuation_stale_stub": "ℹ️ **此页已收起，请查看下方更新后的卡片。**",
    # ── ws_client Prompts ──
    "ws_session_fail_msg": "⚠️ {name} 会话启动失败，已退回智能模式，请重新发送 /{cmd} 重试",
    "ws_thread_pending_msg": "📝 当前已开启{name}编程模式\n\n请发送你的编程需求，将自动创建编程话题",
    "ws_topic_hint_msg": "💡 当前话题已在编程模式中，直接发送你的需求即可\n\n如需切换工具，请在主对话中发送对应命令创建新话题",
    "ws_exit_deferred_msg": "✅ 已收到 /exit，将在当前任务完成后退出（不中断执行）",
    "ws_active_topic_msg": "💡 你有一个活跃的 {name} 编程话题正在进行中\n\n请在话题中回复继续对话\n如需新建编程环境，请先发送对应的编程模式命令（如 /coco）",
    "ws_backpressure_spec": "⚠️ 系统繁忙，请稍后再试",
    "ws_backpressure_generic": "⚠️ 系统繁忙，请稍后再试",
    "ws_message_timeout": "⏳ 处理消息超时，请稍后重试",
    "ws_message_internal_error": "❌ 处理消息时发生内部错误，请稍后重试",
    "ws_unsupported_msg_type": "⚠️ 目前仅支持文本、图片和富文本消息",
    "ws_image_only_prefix": "请查看并理解以下图片",
    "ws_thread_create_failed": "⚠️ 创建编程话题失败，请重试",
    "ws_system_cmd_gate_blocked": "⏳ 系统指令处理中，按钮暂不可用，请稍后重试",
    "ws_card_action_ack": "已收到操作，正在处理…",
    "ws_project_eviction_notify": "该项目「{name}」暂时与当前群断开连接——因为同时使用的群聊数量已满。你可以随时发送 /project 重新连接",
    "ws_fallback_admin_name": "Bot 管理员",
    # Engine error prefixes
    "engine_error_timeout": "超时",
    "engine_error_exception": "异常",
    # ── Retry / Signature ──
    "retry_command_sig_mismatch": "⚠️ 此按钮已失效，请重新发送命令",
    "retry_command_sig_upgrade_expired": "此按钮已过期，请重新发送命令",
    "retry_project_unavailable": "⚠️ 原项目不可用，请重新操作。",
    # ── LRU Eviction Notification ──
    "eviction_notify_title": "📤 项目已自动解绑",
    "eviction_notify_body": "该项目「{name}」暂时与当前群断开连接——因为同时使用的群聊数量已达 {max} 个上限。\n你可以随时发送 /project 重新连接。",
    "eviction_notify_btn_rebind": "🔗 重新绑定",
}

# Merge spec-engine UI text from the single source of truth
from ..spec_engine.constants import SPEC_UI_TEXT  # noqa: E402

_spec_overlap = UI_TEXT.keys() & SPEC_UI_TEXT.keys()
if _spec_overlap:
    raise RuntimeError(f"UI_TEXT key conflict with SPEC_UI_TEXT: {_spec_overlap}")
UI_TEXT.update(SPEC_UI_TEXT)

# Merge lock-related UI text from dedicated module
from .styles_lock import LOCK_UI_TEXT  # noqa: E402

_lock_overlap = UI_TEXT.keys() & LOCK_UI_TEXT.keys()
if _lock_overlap:
    raise RuntimeError(f"UI_TEXT key conflict with LOCK_UI_TEXT: {_lock_overlap}")
UI_TEXT.update(LOCK_UI_TEXT)

# ──────────────────────────────────────────────────────────────
# Terminal State Markers — emoji-prefixed markdown for card endings
# ──────────────────────────────────────────────────────────────
TERMINAL_MARKERS: dict[str, str] = {
    "completed": "✅ **已完成**",
    "failed": "❌ **执行失败**",
    "blocked": "⏸ **任务已阻塞**",
    "cancelled": "⏹ **已停止**",
    "awaiting_approval": "🔐 **等待授权**",
    "denied": "❌ **授权已拒绝**",
    "continued": "✅ **已获得授权**",
}

# ──────────────────────────────────────────────────────────────
# Footer Status — real-time phase indicator shown at card bottom
# ──────────────────────────────────────────────────────────────
FOOTER_STATUS: dict[str, str] = {
    "thinking": "🧠 正在思考",
    "tool_running": "🧰 正在调用工具",
    "waiting_approval": "🔐 等待批复",
}

# ──────────────────────────────────────────────────────────────
# Truncation Limits — aligned with pokoclaw card-truncation.ts
# ──────────────────────────────────────────────────────────────
TRUNCATION_LIMITS: dict[str, int] = {
    "card_string_max_chars": 220,
    "card_string_max_lines": 6,
    "bash_max_chars": 240,
    "bash_max_lines": 8,
    "reasoning_tail_max": 500,
    "terminal_message_max": 1600,
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
    # Max continuation cards before giving up
    "CONTINUATION_MAX_CARDS": 10,
    # Min content length to enable collapsible panels (avoid overhead for short content)
    "COLLAPSIBLE_MIN_CHARS": 2000,
    # Max collapsible elements before falling back to flat markdown
    "COLLAPSIBLE_MAX_ELEMENTS": 20,
    # Card payload byte budget (aligned with pokoclaw: 27 * 1024)
    "CARD_BYTE_BUDGET": 27 * 1024,
    # Card node budget (max tagged nodes per card)
    "CARD_NODE_BUDGET": 180,
}

# ──────────────────────────────────────────────────────────────
# Worktree display constants
# ──────────────────────────────────────────────────────────────

STATUS_DISPLAY_MAP: dict[str, str] = {
    "completed": "已完成",
    "failed": "失败",
    "running": "执行中",
    "planned": "已规划",
    "ready": "就绪",
    "pending": "等待中",
}
