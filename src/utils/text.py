import re


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
