"""Group naming: format and validate name/suffix for Feishu group chat."""

import re
from typing import Optional

# Max length for each part (name or suffix)
_MAX_PART_LENGTH = 50

# Allowed characters: word chars (unicode), dash, dot
_VALID_PART_RE = re.compile(r"^[\w\-.]+$", re.UNICODE)


def format_group_name(name: str, suffix: str) -> str:
    """Format group name as '{name}-{suffix}'."""
    return f"{name.strip()}-{suffix.strip()}"


def validate_name_part(part: str) -> Optional[str]:
    """Validate a name or suffix part.

    Returns None if valid, or an error message string if invalid.
    """
    part = part.strip()
    if not part:
        return "名称不能为空"
    if len(part) > _MAX_PART_LENGTH:
        return f"名称过长（最大 {_MAX_PART_LENGTH} 字符）"
    if not _VALID_PART_RE.match(part):
        return "名称包含非法字符（不能包含空格或特殊符号，允许字母/数字/中文/下划线/短横/点）"
    return None
