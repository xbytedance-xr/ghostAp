"""Button configuration for card builders."""

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
