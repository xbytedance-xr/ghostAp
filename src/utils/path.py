"""Path utilities (pure stdlib helpers).

本模块必须保持“纯库”属性：只依赖标准库（pathlib/typing 等），
避免引入 TTADK/ACP/Feishu 等业务依赖，以杜绝循环依赖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def normalize_ttadk_cwd(cwd: Optional[str]) -> Optional[str]:
    """将 cwd 归一化为绝对路径（用于 TTADK 相关调用链）。

    约束：best-effort，不抛异常。
    - None/空串 -> None
    - 其他 -> Path(cwd).expanduser().resolve() 的字符串形式

    说明：TTADKModelCache 仅对“绝对路径 cwd”启用项目级落盘。
    上层入口统一归一化可避免传入 "." 导致只走内存不落盘。
    """
    raw = (cwd or "").strip()
    if not raw:
        return None
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return None

