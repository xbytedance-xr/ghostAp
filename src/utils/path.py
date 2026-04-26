"""Path utilities (pure stdlib helpers).

本模块必须保持“纯库”属性：只依赖标准库（pathlib/typing 等），
避免引入 TTADK/ACP/Feishu 等业务依赖，以杜绝循环依赖。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def normalize_ttadk_cwd(cwd: Optional[str]) -> Optional[str]:
    """将 cwd 归一化为绝对路径（用于 TTADK 相关调用链）。

    约束：best-effort，不抛异常。
    - None/空串 -> None
    - 其他 -> `expanduser` + `absolute`（不做 realpath/symlink 展开）

    说明：
    - TTADKModelCache 仅对“绝对路径 cwd”启用项目级落盘，上层入口统一归一化可避免传入
      "." 导致只走内存不落盘。
    - 这里刻意不做 `resolve()`，避免把 `/tmp` 折叠为 `/private/tmp`，导致上层日志/断言
      与传入值语义不一致。
    """
    raw = (cwd or "").strip()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        return str(p.absolute())
    except Exception:
        return None


def normalize_repo_path(path: Optional[str]) -> Optional[str]:
    """将仓库路径归一化为唯一的绝对路径（用于仓库锁 key）。

    - None/空串 -> None
    - 其他 -> ``os.path.realpath(os.path.expanduser(path))``

    与 :func:`normalize_ttadk_cwd` 的区别：这里**会**展开符号链接（realpath），
    确保 ``~/repo``、``/home/user/repo``、``/home/user/./repo`` 等价路径
    归一化后产出相同字符串，用于仓库级互斥锁的 key 比较。
    """
    raw = (path or "").strip()
    if not raw:
        return None
    try:
        return os.path.realpath(os.path.expanduser(raw))
    except Exception:
        return None
