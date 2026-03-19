"""Codex Provider（ACP 模式）。"""

from __future__ import annotations

import functools
import re
from typing import Optional

from ..provider import ACPProvider


@functools.lru_cache(maxsize=1)
def _get_codex_acp_serve_help_blob() -> str:
    """Best-effort 获取 `codex acp serve --help` 输出摘要（缓存）。"""
    try:
        from ..sync_adapter import _probe_acp_serve_help

        ok, _rc, out_snip, err_snip = _probe_acp_serve_help("codex")
        blob = (out_snip or "") + "\n" + (err_snip or "")
        return blob
    except Exception:
        return ""


def _detect_model_arg_style(help_blob: str) -> str:
    blob = (help_blob or "").lower()
    if not blob:
        return "unknown"
    if "--model" in blob:
        return "model_long"
    if re.search(r"(^|\s)-m(\s|$)", blob):
        return "model_short"
    return "unknown"


class CodexProvider(ACPProvider):
    @property
    def name(self) -> str:
        return "codex"

    def check_availability(self) -> bool:
        try:
            from ..sync_adapter import _probe_acp_serve_help

            ok, _rc, _out, _err = _probe_acp_serve_help("codex")
            return bool(ok)
        except Exception:
            return False

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        args: list[str] = ["acp", "serve"]

        m = (model_name or "").strip()
        if m:
            style = _detect_model_arg_style(_get_codex_acp_serve_help_blob())
            if style == "model_long":
                args.extend(["--model", m])
            elif style == "model_short":
                args.extend(["-m", m])
            else:
                # unknown: 不强行透传，避免参数不兼容
                pass

        return "codex", args
