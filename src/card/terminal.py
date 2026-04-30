"""Terminal state markers, footer status, and status display map."""

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
