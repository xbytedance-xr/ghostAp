import re
import time
import threading
import uuid

_TASK_ID_LOCK = threading.Lock()
_TASK_ID_SEQ = 0


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration string.

    Examples: "5秒", "3分12秒", "1小时30分5秒"
    """
    if seconds < 0:
        seconds = 0
    total_secs = int(seconds)
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    secs = total_secs % 60
    if hours > 0:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes > 0:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def append_duration_to_title(title: str, duration_secs: float | None) -> str:
    """Append formatted duration to title if available. E.g. '🔄 执行中 · 3分45秒'."""
    if duration_secs:
        return f"{title} · {format_duration(duration_secs)}"
    return title


def generate_task_id(project_name: str) -> str:
    """Generate a human-readable task ID: {name}_{YYYYMMDD}_{HHMMSS}_{4hex}.

    Includes 4 random hex chars to avoid collisions on rapid submission.
    """
    global _TASK_ID_SEQ
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in project_name)[:30]
    # uuid4 的 16-bit 截断在高并发/同秒内仍可能碰撞；这里改为进程内自增序号保证稳定去重。
    with _TASK_ID_LOCK:
        _TASK_ID_SEQ = (_TASK_ID_SEQ + 1) & 0xFFFF
        suffix = f"{_TASK_ID_SEQ:04x}"
    return f"{safe_name}_{ts}_{suffix}"


def make_progress_bar(completed: int, total: int) -> str:
    """Render a text progress bar: [█████░░░░░] 50% (5/10)."""
    if total == 0:
        return "[░░░░░░░░░░] 0%"

    percent = (completed / total) * 100
    filled = int(percent / 10)
    empty = 10 - filled

    return f"[{'█' * filled}{'░' * empty}] {percent:.0f}% ({completed}/{total})"


def clean_terminal_output(output: str) -> str:
    """去除 ANSI 转义序列和 OSC 序列"""
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    output = re.sub(r'\x1b\][^\x07]*\x07', '', output)
    output = re.sub(r'\x1b[\[\]\\^][^\x07\x1b]*', '', output)
    return output.strip()


def truncate_output(output: str, max_len: int, label: str = "输出被截断") -> str:
    if len(output) > max_len:
        return output[:max_len] + f"\n\n... ({label}，共 {len(output)} 字符)"
    return output
