"""TTADK 兼容层（compat）。

本模块用于承载历史兼容符号（monkeypatch/旧导入路径），将其从 `src.ttadk.manager` 的业务主体中隔离。

约束：
- 不得承载任何新的业务逻辑；仅允许 alias/薄转调。
- stub 冷却（runtime invalid-model cooldown）的 SSOT 仍在 `src.ttadk.startup_common`。

Provider 注入契约（显式，避免隐式 import 耦合）：
- `install_compat_providers()` 负责将 provider 注入 `src.ttadk.startup_common.install_stub_cooldown_providers()`。
- provider 来源默认由 `src.ttadk.manager.get_ttadk_manager()` 组装并显式传入，compat 本身不应通过
  `sys.modules` 反射读取/回写 `src.ttadk.manager` 的变量。

legacy_store_provider 返回约定：
- dict: 使用该 legacy store（用于历史 monkeypatch 注入）
- None: 未指定 legacy store（不覆盖 SSOT 内部默认 store）
- `startup_common.LEGACY_STORE_CLEARED`: 显式清空（解绑旧 dict 引用，避免 monkeypatch 清空无效）
"""

from __future__ import annotations

import time
import threading
from typing import Callable, Optional

from . import startup_common as _startup_common
from ..config import get_settings as _get_settings


# ---------------------------------------------------------------------------
# deprecated_* Runtime invalid-model cooldown (compat only)
# ---------------------------------------------------------------------------


# DEPRECATED compat hook:
# - 历史测试/外部脚本可能 monkeypatch `src.ttadk.manager._LEGACY_STUB_COOLDOWN_STORE`。
# - SSOT 位于 `src.ttadk.startup_common._LEGACY_STUB_COOLDOWN_STORE`，该模块会 best-effort 同步读取 manager 侧变量。
_LEGACY_STUB_COOLDOWN_STORE: dict[tuple[str, str, str], float] | None = None


# DEPRECATED compat symbol:
# - 历史测试会用 `src.ttadk.manager._StubCooldownStore()` 创建隔离实例。
# - 这里直接 alias 到 SSOT 模块中的实现。
_StubCooldownStore = _startup_common._StubCooldownStore


# DEPRECATED compat symbol:
# - 保留 `_STUB_COOLDOWN` 名称供历史测试/外部脚本 monkeypatch。
# - 默认指向 SSOT 对象（避免“双 SSOT”）。
_STUB_COOLDOWN = _startup_common._STUB_COOLDOWN


# ---------------------------------------------------------------------------
# Explicit provider injection
# ---------------------------------------------------------------------------


_PROVIDER_TIME_FN: Callable[[], float] = time.time
_PROVIDER_GET_SETTINGS_FN: Callable[[], object] = _get_settings


def _default_legacy_store_provider() -> object:
    # 默认只使用 startup_common 的模块级 legacy hook（由其内部处理 detach/merge）。
    return getattr(_startup_common, "_LEGACY_STUB_COOLDOWN_STORE", None)


_PROVIDER_LEGACY_STORE_PROVIDER: Callable[[], object] = _default_legacy_store_provider


_providers_lock = threading.Lock()
_providers_installed: bool = False


def migrate_legacy_store_from_fn_attr(
    fn: object,
    *,
    attr_name: str = "_runtime_invalid_model_last_ts_by_stub",
) -> dict[tuple[str, str, str], float] | None:
    """显式迁移：从历史函数属性挂载点提取 legacy store。

    说明：历史实现可能把 store 挂在 `coordinate_ttadk_startup._runtime_invalid_model_last_ts_by_stub`。
    为避免 compat 层通过 `sys.modules` 隐式扫描，本函数仅做“显式读取”，由调用方（通常是
    `src.ttadk.manager.get_ttadk_manager()`）在初始化路径中按需调用一次。
    """
    try:
        cand = getattr(fn, attr_name, None)
    except Exception:
        cand = None
    return cand if isinstance(cand, dict) else None


def install_compat_providers(
    *,
    force: bool = False,
    time_fn: Optional[Callable[[], float]] = None,
    get_settings_fn: Optional[Callable[[], object]] = None,
    legacy_store_provider: Optional[Callable[[], object]] = None,
) -> bool:
    """把 compat 侧 provider 注入 startup_common（SSOT）。

    约束：
    - 该函数是显式初始化入口；`compat.py` 不应在 import-time 自动调用，以避免隐式副作用。
    - 默认幂等：重复调用不会重复安装；如需强制重装（例如单测恢复环境），传 `force=True`。

    返回：本次是否发生安装。
    """
    global _providers_installed
    with _providers_lock:
        if _providers_installed and not force:
            return False
        global _PROVIDER_TIME_FN, _PROVIDER_GET_SETTINGS_FN, _PROVIDER_LEGACY_STORE_PROVIDER
        if callable(time_fn):
            _PROVIDER_TIME_FN = time_fn
        if callable(get_settings_fn):
            _PROVIDER_GET_SETTINGS_FN = get_settings_fn
        if callable(legacy_store_provider):
            _PROVIDER_LEGACY_STORE_PROVIDER = legacy_store_provider
        _startup_common.install_stub_cooldown_providers(
            time_fn=_PROVIDER_TIME_FN,
            get_settings_fn=_PROVIDER_GET_SETTINGS_FN,
            legacy_store_provider=_PROVIDER_LEGACY_STORE_PROVIDER,
        )
        _providers_installed = True
        return True


def _runtime_invalid_model_stub_limits() -> tuple[float, int, float]:
    return _STUB_COOLDOWN.limits()


def _runtime_invalid_model_stub_key(mgr, tool_name: str) -> tuple[str, str, str]:
    return _STUB_COOLDOWN.key(mgr, tool_name)


def _runtime_invalid_model_stub_store_unlocked() -> dict[tuple[str, str, str], float]:
    # 兼容历史调用点：该函数语义为“调用方已持锁”。
    return _STUB_COOLDOWN._store_unlocked()


def _runtime_invalid_model_stub_store() -> dict[tuple[str, str, str], float]:
    return _STUB_COOLDOWN.store()


def _runtime_invalid_model_stub_get_last_ts(mgr, tool_name: str) -> float:
    return _STUB_COOLDOWN.get_last_ts(mgr, tool_name)


def _runtime_invalid_model_stub_set_last_ts(mgr, tool_name: str, ts: float) -> None:
    _STUB_COOLDOWN.set_last_ts(mgr, tool_name, ts)
