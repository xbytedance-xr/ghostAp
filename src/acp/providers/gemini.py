"""Gemini Provider（ACP 模式）。"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from typing import Optional

from ..provider import ACPProvider


@functools.lru_cache(maxsize=1)
def _get_gemini_acp_serve_help_blob() -> str:
    """Best-effort 获取 `gemini --help` 输出摘要（缓存）。"""
    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        p = subprocess.run(
            ["gemini", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        return ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    except Exception:
        return ""


def _load_gemini_acp_help_blob() -> str:
    blob = _get_gemini_acp_serve_help_blob()
    if blob:
        return blob
    _get_gemini_acp_serve_help_blob.cache_clear()
    return _get_gemini_acp_serve_help_blob()


def _detect_model_arg_style(help_blob: str) -> str:
    blob = (help_blob or "").lower()
    if not blob:
        return "unknown"
    if "--model" in blob:
        return "model_long"
    if re.search(r"(^|\s)-m(\s|$)", blob):
        return "model_short"
    if "model.name" in blob or re.search(r"(^|\s)-c(\s|$)", blob):
        return "config_c"
    return "unknown"


class GeminiProvider(ACPProvider):
    @property
    def name(self) -> str:
        return "gemini"

    def check_availability(self) -> bool:
        blob = _load_gemini_acp_help_blob().lower()
        return bool(blob and "usage:" in blob and "--acp" in blob)

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        args: list[str] = ["--acp"]

        m = (model_name or "").strip()
        if m:
            style = _detect_model_arg_style(_load_gemini_acp_help_blob())
            if style == "model_long":
                args.extend(["--model", m])
            elif style == "model_short":
                args.extend(["-m", m])
            elif style == "config_c":
                args.extend(["-c", f"model.name={m}"])

        return "gemini", args
