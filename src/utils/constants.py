"""Shared constants used across engine and card layers."""

# ──────────────────────────────────────────────────────────────
# Worktree unit status → display text mapping
# ──────────────────────────────────────────────────────────────
STATUS_DISPLAY_MAP: dict[str, str] = {
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "running": "执行中",
    "planned": "已规划",
    "ready": "就绪",
    "pending": "等待中",
}
