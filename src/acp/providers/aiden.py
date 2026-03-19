"""Aiden Provider（ACP 模式）。"""

from __future__ import annotations

import functools
import re
from typing import Optional

from ..provider import ACPProvider


@functools.lru_cache(maxsize=1)
def _get_aiden_acp_serve_help_blob() -> str:
    """Best-effort 获取 `aiden acp serve --help` 输出摘要。

    - 该函数被缓存以避免反复触发外部进程
    - 失败时返回空字符串
    """
    try:
        from ..sync_adapter import _probe_acp_serve_help

        ok, _rc, out_snip, err_snip = _probe_acp_serve_help("aiden")
        blob = (out_snip or "") + "\n" + (err_snip or "")
        # 即使 ok=False，也保留 blob 以便后续能力判断（但为空时仍需兜底）
        return blob
    except Exception:
        return ""


def _detect_model_arg_style(help_blob: str) -> str:
    """从 help 输出中推断 model 参数风格。"""
    blob = (help_blob or "").lower()
    if not blob:
        return "unknown"

    # coco/ark 风格：-c model.name=xxx
    if "model.name" in blob or re.search(r"(^|\s)-c(\s|$)", blob):
        return "config_c"

    # 常见风格：--model 或 -m
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
        """判断 aiden 是否可用且支持 `acp serve`。"""
        try:
            from ..sync_adapter import _probe_acp_serve_help

            ok, _rc, _out, _err = _probe_acp_serve_help("aiden")
            return bool(ok)
        except Exception:
            return False

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        """生成 aiden ACP Server 启动命令。

        说明：不同环境下 aiden 的 CLI 参数可能有差异，因此这里基于 help 输出做 best-effort 适配。
        """
        args: list[str] = ["acp", "serve"]

        m = (model_name or "").strip()
        if m:
            style = _detect_model_arg_style(_get_aiden_acp_serve_help_blob())
            if style == "config_c":
                args.extend(["-c", f"model.name={m}"])
            elif style == "model_long":
                args.extend(["--model", m])
            elif style == "model_short":
                args.extend(["-m", m])
            else:
                # unknown: 不强行透传，避免因为参数不兼容导致启动失败
                pass

        return "aiden", args
