import re


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
