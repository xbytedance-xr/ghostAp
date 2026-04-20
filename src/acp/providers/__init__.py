from __future__ import annotations

import functools
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..provider import ACPProvider, ToolRegistry, tool_registry


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


AvailabilityChecker = Callable[[], bool]
HelpBlobLoader = Callable[[], str]


def _make_resolve_checker(tool_name: str) -> AvailabilityChecker:
    def _check() -> bool:
        from ..sync_adapter import _resolve_with_auto_update
        return _resolve_with_auto_update(tool_name)
    return _check


def _make_custom_help_checker(
    help_cmd: list[str],
    required_keywords: Sequence[str],
) -> tuple[AvailabilityChecker, HelpBlobLoader]:
    @functools.lru_cache(maxsize=1)
    def _get_help_blob() -> str:
        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            p = subprocess.run(
                help_cmd,
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
            return ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        except Exception:
            return ""

    def _load_help_blob() -> str:
        blob = _get_help_blob()
        if blob:
            return blob
        _get_help_blob.cache_clear()
        return _get_help_blob()

    def _check() -> bool:
        blob = _load_help_blob().lower()
        return bool(blob and all(kw in blob for kw in required_keywords))

    return _check, _load_help_blob


def _make_probe_checker(tool_name: str) -> tuple[AvailabilityChecker, HelpBlobLoader]:
    @functools.lru_cache(maxsize=1)
    def _get_help_blob() -> str:
        try:
            from ..sync_adapter import _probe_acp_serve_help
            ok, _rc, out_snip, err_snip = _probe_acp_serve_help(tool_name)
            return (out_snip or "") + "\n" + (err_snip or "")
        except Exception:
            return ""

    def _check() -> bool:
        try:
            from ..sync_adapter import _probe_acp_serve_help
            ok, _rc, _out, _err = _probe_acp_serve_help(tool_name)
            return bool(ok)
        except Exception:
            return False

    return _check, _get_help_blob


@dataclass(frozen=True)
class _ProviderConfig:
    tool_name: str
    serve_args: list[str]
    availability_checker: AvailabilityChecker
    model_style: Optional[str] = None
    help_blob_loader: Optional[HelpBlobLoader] = None
    is_default: bool = False
    skip_model_selection: bool = False


def _apply_model_args(
    args: list[str],
    model_name: Optional[str],
    model_style: Optional[str],
    help_blob_loader: Optional[HelpBlobLoader],
) -> list[str]:
    m = (model_name or "").strip()
    if not m:
        return args

    if model_style is None:
        return args

    style = model_style
    if style == "dynamic" and help_blob_loader:
        style = _detect_model_arg_style(help_blob_loader())

    if style == "config_c":
        return args + ["-c", f"model.name={m}"]
    if style == "model_long":
        return args + ["--model", m]
    if style == "model_short":
        return args + ["-m", m]
    return args


class GenericACPProvider:
    def __init__(self, config: _ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return self._config.tool_name

    @property
    def skip_model_selection(self) -> bool:
        return bool(self._config.skip_model_selection)

    def check_availability(self) -> bool:
        return self._config.availability_checker()

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        args = list(self._config.serve_args)
        args = _apply_model_args(
            args, model_name, self._config.model_style, self._config.help_blob_loader
        )
        return self._config.tool_name, args

    def get_fallback_command(self, model_name: Optional[str] = None) -> Optional[tuple[str, list[str]]]:
        return None


def _make_custom_help_checker_with_cache_handle(
    help_cmd: list[str],
    required_keywords: Sequence[str],
) -> tuple[AvailabilityChecker, HelpBlobLoader, Callable[[], None]]:
    checker, loader = _make_custom_help_checker(help_cmd, required_keywords)

    def _find_lru_cached(*fns: object) -> Optional[Callable[[], None]]:
        for fn in fns:
            if hasattr(fn, "cache_clear"):
                return fn.cache_clear  # type: ignore[union-attr]
            for cell in (getattr(fn, "__closure__", None) or []):
                obj = cell.cell_contents
                if callable(obj) and hasattr(obj, "cache_clear"):
                    return obj.cache_clear
        return None

    clear_fn = _find_lru_cached(checker, loader)

    def _clear() -> None:
        if clear_fn is not None:
            clear_fn()

    return checker, loader, _clear


def _make_probe_checker_with_cache_handle(
    tool_name: str,
) -> tuple[AvailabilityChecker, HelpBlobLoader, Callable[[], None]]:
    checker, loader = _make_probe_checker(tool_name)

    def _find_lru_cached(*fns: object) -> Optional[Callable[[], None]]:
        for fn in fns:
            if hasattr(fn, "cache_clear"):
                return fn.cache_clear  # type: ignore[union-attr]
            for cell in (getattr(fn, "__closure__", None) or []):
                obj = cell.cell_contents
                if callable(obj) and hasattr(obj, "cache_clear"):
                    return obj.cache_clear
        return None

    clear_fn = _find_lru_cached(loader, checker)

    def _clear() -> None:
        if clear_fn is not None:
            clear_fn()

    return checker, loader, _clear


_aiden_checker, _aiden_help_loader, _aiden_cache_clear = _make_custom_help_checker_with_cache_handle(
    ["aiden", "acp", "--help"],
    ["usage:", "aiden acp", "acp agent"],
)

_codex_checker, _codex_help_loader, _codex_cache_clear = _make_probe_checker_with_cache_handle("codex")

_gemini_checker, _gemini_help_loader, _gemini_cache_clear = _make_custom_help_checker_with_cache_handle(
    ["gemini", "--help"],
    ["usage:", "--acp"],
)

_PROVIDER_CONFIGS: list[_ProviderConfig] = [
    _ProviderConfig(
        tool_name="coco",
        serve_args=["acp", "serve"],
        availability_checker=_make_resolve_checker("coco"),
        model_style="config_c",
        is_default=True,
        skip_model_selection=True,
    ),
    _ProviderConfig(
        tool_name="claude",
        serve_args=["acp", "serve"],
        availability_checker=_make_resolve_checker("claude"),
        model_style=None,
    ),
    _ProviderConfig(
        tool_name="aiden",
        serve_args=["acp"],
        availability_checker=_aiden_checker,
        model_style="dynamic",
        help_blob_loader=_aiden_help_loader,
        skip_model_selection=True,
    ),
    _ProviderConfig(
        tool_name="codex",
        serve_args=["acp", "serve"],
        availability_checker=_codex_checker,
        model_style="dynamic",
        help_blob_loader=_codex_help_loader,
    ),
    _ProviderConfig(
        tool_name="gemini",
        serve_args=["--acp"],
        availability_checker=_gemini_checker,
        model_style="dynamic",
        help_blob_loader=_gemini_help_loader,
    ),
]


def _build_providers() -> dict[str, GenericACPProvider]:
    providers: dict[str, GenericACPProvider] = {}
    for cfg in _PROVIDER_CONFIGS:
        p = GenericACPProvider(cfg)
        providers[cfg.tool_name] = p
        tool_registry.register(p, is_default=cfg.is_default)
    return providers


_providers = _build_providers()


CocoProvider = type("CocoProvider", (), {"__new__": lambda cls: _providers["coco"]})
ClaudeProvider = type("ClaudeProvider", (), {"__new__": lambda cls: _providers["claude"]})
AidenProvider = type("AidenProvider", (), {"__new__": lambda cls: _providers["aiden"]})
CodexProvider = type("CodexProvider", (), {"__new__": lambda cls: _providers["codex"]})
GeminiProvider = type("GeminiProvider", (), {"__new__": lambda cls: _providers["gemini"]})


def _get_aiden_acp_serve_help_blob() -> str:
    return _aiden_help_loader()

_get_aiden_acp_serve_help_blob.cache_clear = _aiden_cache_clear  # type: ignore[attr-defined]


def _get_codex_acp_serve_help_blob() -> str:
    return _codex_help_loader()

_get_codex_acp_serve_help_blob.cache_clear = _codex_cache_clear  # type: ignore[attr-defined]


def _get_gemini_acp_serve_help_blob() -> str:
    return _gemini_help_loader()

_get_gemini_acp_serve_help_blob.cache_clear = _gemini_cache_clear  # type: ignore[attr-defined]


__all__ = [
    "ToolRegistry",
    "tool_registry",
    "CocoProvider",
    "ClaudeProvider",
    "AidenProvider",
    "CodexProvider",
    "GeminiProvider",
    "GenericACPProvider",
    "_get_aiden_acp_serve_help_blob",
    "_get_codex_acp_serve_help_blob",
    "_get_gemini_acp_serve_help_blob",
]
