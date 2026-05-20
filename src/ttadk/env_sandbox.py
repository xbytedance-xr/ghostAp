"""TTADK 子进程环境隔离（env_sandbox）。

目标：统一隔离 ttadk 相关子进程的外部副作用，避免运行/测试写入真实 `~/.ttadk`。

设计原则：
- 只覆盖必要的环境变量（默认：HOME/XDG_CONFIG_HOME；可选：XDG_CACHE_HOME）
- 其余环境变量继承父进程，避免破坏代理/证书/locale 等运行依赖
- 统一清理 `CLAUDECODE`（避免嵌套会话 guard 导致探测/启动误判）
- 保留真实 HOME 下的鉴权目录（Library/Preferences）到 sandbox，避免重复 OAuth 鉴权
- best-effort：即使目录创建失败，也不抛异常影响主流程（回退为原 env）
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from ..config import get_settings
from ..utils.errors import get_error_detail

logger = logging.getLogger(__name__)

_SANDBOX_LOGGED = False

_AUTH_PRESERVE_DIRS: tuple[str, ...] = (
    os.path.join("Library", "Preferences", "bytesso-nodejs"),
) if sys.platform == "darwin" else ()


def _ttadk_candidate_bin_dirs() -> list[str]:
    """Return common user-level TTADK install locations missing from GUI app PATH."""
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".local", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]


def resolve_ttadk_executable(*, path: str | None = None) -> str:
    """Resolve ttadk from PATH plus common npm-global locations."""
    try:
        found = shutil.which("ttadk", path=path)
        if found:
            return found
    except Exception:
        logger.debug("resolve_ttadk_executable: shutil.which failed", exc_info=True)

    for directory in _ttadk_candidate_bin_dirs():
        candidate = os.path.join(directory, "ttadk")
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except Exception:
            logger.debug("resolve_ttadk_executable: candidate check failed", exc_info=True)
    return ""


def ensure_ttadk_path(env: dict[str, str]) -> dict[str, str]:
    """Prepend the resolved ttadk directory to PATH when GUI shells omit it."""
    try:
        resolved = resolve_ttadk_executable(path=env.get("PATH"))
        if not resolved:
            return env
        bin_dir = os.path.dirname(resolved)
        path_parts = [part for part in (env.get("PATH") or "").split(os.pathsep) if part]
        if bin_dir and bin_dir not in path_parts:
            env["PATH"] = os.pathsep.join([bin_dir, *path_parts])
    except Exception:
        logger.debug("ensure_ttadk_path: failed", exc_info=True)
    return env


def _symlink_auth_dirs(real_home: str, sandbox_root: Path) -> None:
    for rel in _AUTH_PRESERVE_DIRS:
        src = Path(real_home) / rel
        if not src.is_dir():
            continue
        dst = sandbox_root / rel
        if dst.is_symlink():
            if dst.resolve() == src.resolve():
                continue
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            dst.unlink(missing_ok=True)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
        except Exception as exc:
            logger.debug("[env_sandbox] failed to symlink auth dir %s → %s: %s", dst, src, get_error_detail(exc))


def _safe_str(x: object) -> str:
    try:
        return str(x)
    except Exception:
        logger.debug("_safe_str: return str(x)", exc_info=True)
        return ""


def _resolve_sandbox_root(*, cwd: str, configured_root: str) -> Path:
    """解析 sandbox 根目录。

    规则：
    - configured_root 为空：默认 `<cwd>/.ttadk_sandbox`
    - configured_root 为相对路径：相对 `<cwd>`
    - configured_root 支持 `{cwd}` 占位符
    """
    base = Path(cwd or ".").resolve()
    raw = (_safe_str(configured_root) or "").strip()
    if not raw:
        return base / ".ttadk_sandbox"
    try:
        raw = raw.format(cwd=str(base))
    except Exception:
        logger.debug("_resolve_sandbox_root: raw.format(cwd=str(base))", exc_info=True)
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def build_ttadk_subprocess_env(
    *,
    cwd: str,
    agent_type: str = "",
    tool_name: str = "",
    base_env: Optional[dict[str, str]] = None,
    get_settings_fn: Optional[Callable[[], object]] = None,
) -> tuple[dict[str, str], str]:
    """构造用于启动 ttadk 相关子进程的隔离环境。

    返回 (env, sandbox_root)。其中 sandbox_root 为空字符串表示未启用/未生效。

    Contract:
    - 永不抛异常
    - 当隔离开启时，覆盖 HOME/XDG_CONFIG_HOME（可选 XDG_CACHE_HOME）
    - 始终移除 CLAUDECODE
    """
    try:
        env = dict(base_env) if isinstance(base_env, dict) else os.environ.copy()
    except Exception:
        env = {}
    ensure_ttadk_path(env)

    # Always drop nested-session guard to keep behavior consistent across ttadk paths.
    try:
        env.pop("CLAUDECODE", None)
    except Exception:
        logger.debug("build_ttadk_subprocess_env: env.pop('CLAUDECODE', None)", exc_info=True)

    sandbox_root = ""
    if get_settings_fn is None:
        get_settings_fn = get_settings

    try:
        s = get_settings_fn()
    except Exception:
        s = None

    enabled = True
    cover_cache = False
    configured_root = ""
    try:
        enabled = bool(getattr(s, "ttadk_sandbox_home_enabled", True)) if s is not None else True
        cover_cache = bool(getattr(s, "ttadk_sandbox_cover_cache_home", False)) if s is not None else False
        configured_root = _safe_str(getattr(s, "ttadk_sandbox_home_root", "") if s is not None else "")
    except Exception:
        enabled = True
        cover_cache = False
        configured_root = ""

    if not enabled:
        return env, ""

    # Normalize cwd
    try:
        cwd_norm = str(Path(cwd or ".").resolve())
    except Exception:
        cwd_norm = cwd or "."

    try:
        root = _resolve_sandbox_root(cwd=cwd_norm, configured_root=configured_root)
        sandbox_root = str(root)
        # Create directories best-effort.
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            # If we cannot create sandbox dir, fall back to non-sandbox env
            return env, ""

        xdg_config = root / "xdg_config"
        try:
            xdg_config.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Still proceed: XDG_CONFIG_HOME will point to a (possibly non-existing) dir.
            pass

        env["HOME"] = sandbox_root
        env["XDG_CONFIG_HOME"] = str(xdg_config)

        real_home = os.environ.get("HOME", "")
        if real_home and real_home != sandbox_root:
            try:
                _symlink_auth_dirs(real_home, root)
            except Exception:
                logger.debug("_symlink_auth_dirs(real_home, root)", exc_info=True)

        if cover_cache:
            xdg_cache = root / "xdg_cache"
            try:
                xdg_cache.mkdir(parents=True, exist_ok=True)
            except Exception:
                logger.debug("True, exist_ok=True)", exc_info=True)
            env["XDG_CACHE_HOME"] = str(xdg_cache)

        # Provide lightweight context hints (non-standard, best-effort; safe to ignore).
        # Do NOT override any existing TTADK_* env unless explicitly absent.
        try:
            if agent_type:
                env.setdefault("TTADK_AGENT_TYPE", _safe_str(agent_type))
            if tool_name:
                env.setdefault("TTADK_TOOL_NAME", _safe_str(tool_name))
        except Exception:
            logger.debug("evaluate condition", exc_info=True)

        # One-time observability log (do not print sensitive values)
        global _SANDBOX_LOGGED
        if not _SANDBOX_LOGGED:
            _SANDBOX_LOGGED = True
            try:
                keys = ["HOME", "XDG_CONFIG_HOME"] + (["XDG_CACHE_HOME"] if cover_cache else [])
                logger.info(
                    "[TTADK:Sandbox] enabled=%s sandbox_root=%s overridden_keys=%s",
                    True,
                    sandbox_root,
                    keys,
                )
            except Exception:
                logger.debug("['HOME', 'XDG_CONFIG_HOME'] + (['XDG_CACHE_HOME'] ", exc_info=True)

        return env, sandbox_root
    except Exception:
        return env, ""
