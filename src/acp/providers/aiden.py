"""Aiden Provider（ACP 模式）。"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from typing import Optional

from ..provider import ACPProvider


@functools.lru_cache(maxsize=1)
def _get_aiden_acp_serve_help_blob() -> str:
    """Best-effort 获取 `aiden acp --help` 输出摘要。

    Aiden 当前 CLI 以 `aiden acp` 进入 ACP 模式，而不是 `aiden acp serve`。
    """
    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        p = subprocess.run(
            ["aiden", "acp", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        return ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    except Exception:
        return ""


def _load_aiden_acp_help_blob() -> str:
    blob = _get_aiden_acp_serve_help_blob()
    if blob:
        return blob
    _get_aiden_acp_serve_help_blob.cache_clear()
    return _get_aiden_acp_serve_help_blob()


def _detect_model_arg_style(help_blob: str) -> str:
    """从 help 输出中推断 model 参数风格。"""
    blob = (help_blob or "").lower()
    if not blob:
        return "unknown"

    if "model.name" in blob or re.search(r"(^|\s)-c(\s|$)", blob):
        return "config_c"
    if "--model" in blob:
        return "model_long"
    if re.search(r"(^|\s)-m(\s|$)", blob):
        return "model_short"
    return "unknown"


class AidenProvider(ACPProvider):
    @property
    def name(self) -> str:
        return "aiden"

    def check_availability(self) -> bool:
        """判断 aiden 是否可用且支持 ACP 模式。"""
        blob = _load_aiden_acp_help_blob().lower()
        return bool(blob and "usage:" in blob and "aiden acp" in blob and "acp agent" in blob)

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        """生成 aiden ACP Server 启动命令。"""
        args: list[str] = ["acp"]

        m = (model_name or "").strip()
        if m:
            style = _detect_model_arg_style(_load_aiden_acp_help_blob())
            if style == "config_c":
                args.extend(["-c", f"model.name={m}"])
            elif style == "model_long":
                args.extend(["--model", m])
            elif style == "model_short":
                args.extend(["-m", m])

        return "aiden", args
