"""TTADK 启动编排公共 helper（startup_common）。

目标：为 `src/ttadk/startup.py` 提供无状态/低依赖 helper，使其不再依赖 `src/ttadk/manager.py`，
从而降低循环依赖风险。同时承载 compat 兼容层（provider 注入、legacy store 迁移等）。

本模块只依赖：标准库 + `src/config.py` + `src/ttadk/*` 低层模块。
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Callable, Optional

from ..config import get_settings
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider injection (explicit deps; no sys.modules coupling)
# ---------------------------------------------------------------------------


# Sentinel: returned by legacy_store_provider to indicate an *explicit* clear.
# This is needed to distinguish "no legacy specified" vs "legacy cleared".
LEGACY_STORE_CLEARED = object()


_STUB_TIME_FN: Callable[[], float] = time.time
_STUB_GET_SETTINGS_FN: Callable[[], object] = get_settings


def _default_legacy_store_provider() -> object:
    # Default behavior: only consult this module's explicit legacy hook.
    return _LEGACY_STUB_COOLDOWN_STORE


_STUB_LEGACY_STORE_PROVIDER: Callable[[], object] = _default_legacy_store_provider


def install_stub_cooldown_providers(
    *,
    time_fn: Optional[Callable[[], float]] = None,
    get_settings_fn: Optional[Callable[[], object]] = None,
    legacy_store_provider: Optional[Callable[[], object]] = None,
) -> None:
    global _STUB_TIME_FN, _STUB_GET_SETTINGS_FN, _STUB_LEGACY_STORE_PROVIDER
    if callable(time_fn):
        _STUB_TIME_FN = time_fn
    if callable(get_settings_fn):
        _STUB_GET_SETTINGS_FN = get_settings_fn
    if callable(legacy_store_provider):
        _STUB_LEGACY_STORE_PROVIDER = legacy_store_provider


_compat_providers_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
_compat_providers_installed: bool = False


def migrate_legacy_store_from_fn_attr(
    fn: object,
    *,
    attr_name: str = "_runtime_invalid_model_last_ts_by_stub",
) -> dict[tuple[str, str, str], float] | None:
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
    global _compat_providers_installed
    with _compat_providers_lock:
        if _compat_providers_installed and not force:
            return False
        _time_fn = time_fn if callable(time_fn) else _STUB_TIME_FN
        _gsf = get_settings_fn if callable(get_settings_fn) else _STUB_GET_SETTINGS_FN
        _lsp = legacy_store_provider if callable(legacy_store_provider) else _STUB_LEGACY_STORE_PROVIDER
        install_stub_cooldown_providers(
            time_fn=_time_fn,
            get_settings_fn=_gsf,
            legacy_store_provider=_lsp,
        )
        _compat_providers_installed = True
        return True


# ---------------------------------------------------------------------------
# Runtime invalid-model cooldown (stub path helpers)
# ---------------------------------------------------------------------------


# DEPRECATED:
# - 历史实现可能把 store 挂在 `coordinate_ttadk_startup._runtime_invalid_model_last_ts_by_stub` 上。
# - 现在该函数属性仅作为“一次性只读迁移来源”（仅当本模块变量为空时读取）。
_LEGACY_STUB_COOLDOWN_STORE: dict[tuple[str, str, str], float] | None = None


class _StubCooldownStore:
    """非 TTADKManager（测试桩/旧 manager）路径的运行期 invalid-model 冷却状态。

    设计目标：
    - 收敛模块级散落状态，减少测试耦合与并发风险
    - 保留 legacy 兼容：允许 store 挂在 `coordinate_ttadk_startup` 函数对象上
    """

    def __init__(
        self,
        *,
        ttl_default_s: float = 3600.0,
        max_keys_default: int = 1024,
        gc_interval_default_s: float = 60.0,
    ) -> None:
        self._lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        # key: (module, qualname, tool)
        self._store: dict[tuple[str, str, str], float] = {}
        self._last_gc_ts: float = 0.0

        # 容量控制默认值（配置缺失/非法时回退）
        self._ttl_default = float(ttl_default_s)
        self._max_keys_default = int(max_keys_default)
        self._gc_interval_default = float(gc_interval_default_s)

        # limits 缓存：减少高频路径中重复读取 settings 的开销
        self._limits_cache: tuple[float, int, float] | None = None
        self._limits_cache_ts: float = 0.0

    def limits(self) -> tuple[float, int, float]:
        """读取 stub 冷却 store 的限额配置，并做边界收敛。

        返回 (ttl_s, max_keys, gc_interval_s)。
        - ttl_s <= 0 表示禁用 TTL 清理
        - max_keys <= 0 表示禁用数量上限
        - gc_interval_s <= 0 表示每次写入都可触发 GC
        """
        # Use injected time provider (compat layer may route to manager.time.time).
        try:
            now = float(_STUB_TIME_FN())
        except Exception:
            now = time.time()
        with self._lock:
            cached = self._limits_cache
            if cached is not None:
                try:
                    cached_interval = float(cached[2] or 0.0)
                except Exception:
                    cached_interval = 0.0
                # interval<=0 语义为“每次写入都可触发 GC”，此时不做缓存
                if cached_interval > 0.0:
                    try:
                        last_ts = float(self._limits_cache_ts or 0.0)
                    except Exception:
                        last_ts = 0.0
                    if last_ts and (now - last_ts) < cached_interval:
                        return cached

            ttl_default = float(self._ttl_default)
            max_default = int(self._max_keys_default)
            interval_default = float(self._gc_interval_default)

            # Use injected settings provider (compat layer may route to manager.get_settings).
            try:
                s = _STUB_GET_SETTINGS_FN()
            except Exception:
                s = None

            # ttl
            try:
                ttl = float(
                    getattr(s, "ttadk_runtime_stub_cooldown_ttl_s", ttl_default) if s is not None else ttl_default
                )
            except Exception:
                ttl = ttl_default
            ttl = max(0.0, ttl)

            # max_keys
            try:
                max_keys = int(
                    getattr(s, "ttadk_runtime_stub_cooldown_max_keys", max_default) if s is not None else max_default
                )
            except Exception:
                max_keys = max_default
            max_keys = max(0, max_keys)

            # gc interval
            try:
                interval = float(
                    getattr(s, "ttadk_runtime_stub_cooldown_gc_interval_s", interval_default)
                    if s is not None
                    else interval_default
                )
            except Exception:
                interval = interval_default
            interval = max(0.0, interval)

            out = (ttl, max_keys, interval)
            if interval > 0.0:
                self._limits_cache = out
                self._limits_cache_ts = now
            else:
                self._limits_cache = None
                self._limits_cache_ts = 0.0
            return out

    def key(self, mgr, tool_name: str) -> tuple[str, str, str]:
        """为非 TTADKManager 的 manager 生成冷却隔离 key（避免跨测试污染）。"""
        tool = str((tool_name or "").strip().lower())
        try:
            cls = mgr.__class__
            return (
                str(getattr(cls, "__module__", "")),
                str(getattr(cls, "__qualname__", "")),
                tool,
            )
        except Exception:
            return ("", "", tool)

    def _store_unlocked(self) -> dict[tuple[str, str, str], float]:
        """获取 store 并与 legacy store 保持兼容。

        约束：调用方必须持有 self._lock；本函数内部不加锁。
        """
        # NOTE: startup_common must not read sys.modules for manager-level compatibility.
        # Any legacy hook / monkeypatch compatibility is handled via injected providers.
        global _LEGACY_STUB_COOLDOWN_STORE

        legacy_ref = _LEGACY_STUB_COOLDOWN_STORE

        directive = None
        try:
            directive = _STUB_LEGACY_STORE_PROVIDER()
        except Exception:
            directive = None

        # Explicit clear: detach from any previously bound legacy dict.
        if directive is LEGACY_STORE_CLEARED:
            _LEGACY_STUB_COOLDOWN_STORE = None
            try:
                if isinstance(legacy_ref, dict) and self._store is legacy_ref:
                    self._store = {}
                    self._last_gc_ts = 0.0
            except Exception:
                logger.debug("_store_unlocked: evaluate condition", exc_info=True)
            return self._store

        legacy = directive if isinstance(directive, dict) else _LEGACY_STUB_COOLDOWN_STORE
        if isinstance(directive, dict):
            _LEGACY_STUB_COOLDOWN_STORE = directive

        if isinstance(legacy, dict):
            # 若 legacy 与当前 store 不是同一个对象，做一次合并并将引用指向 legacy。
            try:
                if legacy is not self._store:
                    legacy.update(self._store)
                    self._store = legacy
            except Exception:
                logger.debug("_store_unlocked: evaluate condition", exc_info=True)
            return legacy  # type: ignore[return-value]
        return self._store

    def store(self) -> dict[tuple[str, str, str], float]:
        """线程安全地获取 stub 冷却 store（含 legacy 同步）。"""
        with self._lock:
            return self._store_unlocked()

    def _maybe_gc_unlocked(self, *, now_ts: float) -> None:
        """对 store 做摊还式清理。

        约束：调用方必须持有 self._lock。
        """
        try:
            now = float(now_ts)
        except Exception:
            try:
                now = float(_STUB_TIME_FN())
            except Exception:
                now = time.time()

        store = self._store_unlocked()
        ttl, max_keys, interval = self.limits()

        if interval:
            try:
                last = float(self._last_gc_ts or 0.0)
            except Exception:
                last = 0.0
            if last and (now - last) < interval:
                return

        self._last_gc_ts = now

        # 1) TTL 清理（默认开启；TTL=0 表示禁用）
        if ttl:
            expired: list[tuple[str, str, str]] = []
            for k, ts in list(store.items()):
                try:
                    tsv = float(ts or 0.0)
                except Exception:
                    tsv = 0.0
                if tsv and (now - tsv) > ttl:
                    expired.append(k)
            for k in expired:
                try:
                    store.pop(k, None)
                except Exception:
                    continue

        # 2) max_keys 兜底
        if max_keys > 0 and len(store) > max_keys:
            items: list[tuple[tuple[str, str, str], float]] = []
            for k, ts in list(store.items()):
                try:
                    tsv = float(ts or 0.0)
                except Exception:
                    tsv = 0.0
                items.append((k, tsv))
            # newest first
            items.sort(key=lambda x: x[1], reverse=True)
            keep = {k for k, _ in items[:max_keys]}
            for k in list(store.keys()):
                if k not in keep:
                    try:
                        store.pop(k, None)
                    except Exception:
                        continue

    def get_last_ts(self, mgr, tool_name: str) -> float:
        k = self.key(mgr, tool_name)
        with self._lock:
            store = self._store_unlocked()
            try:
                return float(store.get(k, 0.0) or 0.0)
            except Exception:
                logger.debug("get_last_ts: return float(store.get(k, 0.0) or 0.0)", exc_info=True)
                return 0.0

    def set_last_ts(self, mgr, tool_name: str, ts: float) -> None:
        k = self.key(mgr, tool_name)
        try:
            now = float(_STUB_TIME_FN())
        except Exception:
            now = time.time()
        with self._lock:
            store = self._store_unlocked()
            try:
                store[k] = float(ts)
            except Exception:
                logger.debug("set_last_ts: convert to float", exc_info=True)
                return
            try:
                self._maybe_gc_unlocked(now_ts=now)
            except Exception:
                logger.debug("set_last_ts: now)", exc_info=True)
                return


_STUB_COOLDOWN = _StubCooldownStore()


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


# ---------------------------------------------------------------------------
# TTADK startup precheck contract
# ---------------------------------------------------------------------------


def precheck_ttadk_startup_model(
    *,
    agent_type: str,
    cwd: str,
    model_intent: Optional[str],
    manager=None,
    startup_probe_timeout_s: Optional[float] = None,
) -> dict:
    """统一 TTADK 启动阶段预校验入口。

    约束：
    - 仅当 validated=True 才返回 `model`（真实模型名）用于透传 -m
    - validated=False 时返回 model=None，表示让 ttadk 走 (auto)

    重要约束（SSOT 收敛）：
    - 启动期“是否透传 -m”的决策应优先通过 `TTADKManager.resolve_startup_model_with_diagnostics()` 完成，
      并将其 diagnostics 原样透出，供上层日志/排障/验收使用。
    - 仅当 validated=True 且 real_name 非空时才允许透传 -m；否则必须返回 model=None。
    """
    agent_type = (agent_type or "").strip().lower()
    if not agent_type.startswith("ttadk_"):
        return {
            "tool": "",
            "input_model": (model_intent or "").strip(),
            "model": None,
            "validated": False,
            "source": "non_ttadk",
            "decision": "non_ttadk",
            "fail_phase": "",
            "warnings": [],
        }

    tool = agent_type.replace("ttadk_", "")
    try:
        if manager is None:
            # 注意：测试里经常 monkeypatch `src.ttadk.get_ttadk_manager`。
            # 这里通过 importlib 从 package 侧取函数，确保 patch 生效。
            pkg = importlib.import_module(__package__ or "src.ttadk")
            manager = pkg.get_ttadk_manager()

        input_model = (model_intent or getattr(manager, "get_current_model", lambda: "")() or "").strip()

        diag: dict = {}
        # SSOT：优先使用“模型列表获取+真名解析+校验/降级”的单一入口
        if hasattr(manager, "resolve_model_intent_ssot"):
            resolved, mr = manager.resolve_model_intent_ssot(
                tool_name=tool,
                model_intent=input_model,
                cwd=cwd,
                force_refresh=False,
                require_valid=False,
            )
            try:
                diag = dict(getattr(mr, "diagnostics", {}) or {})
            except Exception:
                diag = {}
        elif hasattr(manager, "resolve_startup_model_with_diagnostics"):
            # 旧 SSOT：返回 (ResolvedModelResult, diagnostics)
            try:
                resolved, diag = manager.resolve_startup_model_with_diagnostics(
                    input_model,
                    tool_name=tool,
                    cwd=cwd,
                    timeout_s=startup_probe_timeout_s,
                )
            except TypeError:
                resolved, diag = manager.resolve_startup_model_with_diagnostics(
                    input_model,
                    tool_name=tool,
                    cwd=cwd,
                )
        elif hasattr(manager, "resolve_startup_model"):
            # 兼容不同版本/测试桩的签名：timeout_s 可能不存在
            try:
                resolved = manager.resolve_startup_model(
                    input_model,
                    tool_name=tool,
                    cwd=cwd,
                    timeout_s=startup_probe_timeout_s,
                )
            except TypeError:
                resolved = manager.resolve_startup_model(
                    input_model,
                    tool_name=tool,
                    cwd=cwd,
                )
        else:
            # 兼容旧 manager/test stub
            resolved = manager.resolve_and_ensure_valid_model(
                input_model,
                tool_name=tool,
                cwd=cwd,
            )

        real_name = str(getattr(resolved, "real_name", "") or "")
        resolved_validated = bool(getattr(resolved, "validated", False))

        # Safety: validated=True 但 real_name 为空时，禁止透传 -m。
        validated = bool(resolved_validated and real_name)
        passthrough = real_name if validated else None

        warnings = list(getattr(resolved, "warnings", []) or [])
        if resolved_validated and not validated:
            # 与 validated 语义不一致：强制走 auto，并显式提示不要透传 -m。
            if "no_m_passthrough" not in warnings:
                warnings.append("no_m_passthrough")

        # Guardrail: 若 warnings 明确标记模型列表不可信/为空/错误/禁止透传，则必须强制走 auto。
        # 说明：validated 的最终裁决应来自 manager.resolve_startup_model_with_diagnostics，但为了兼容
        # 旧 manager/test stub，这里做一次兜底收敛，避免上层误透传 -m。
        try:
            if any(
                w in ("models_untrusted", "models_empty", "models_error", "no_m_passthrough")
                or str(w).startswith("models_error")
                for w in (warnings or [])
            ):
                validated = False
                passthrough = None
        except Exception:
            logger.debug("evaluate condition", exc_info=True)
        # 兜底：确保 diagnostics 一定是 dict（避免上层 consumer 做复杂判空）
        try:
            diag_out = dict(diag or {})
        except Exception:
            diag_out = {}

        return {
            "tool": tool,
            "input_model": input_model,
            # resolved_real_name: 用于诊断/日志，不代表一定可透传
            "resolved_real_name": real_name or input_model,
            # passthrough_model: 稳定字段，表示“最终要透传给 ttadk 的 model（validated 才有值）"
            "passthrough_model": passthrough,
            # compat: resolved_model 语义与 passthrough_model 一致（validated 才有真实名，否则为 None）
            "resolved_model": passthrough,
            "model": passthrough,
            "validated": validated,
            "real_name": getattr(resolved, "real_name", "") or input_model,
            "source": getattr(resolved, "source", "") or "unknown",
            "decision": "precheck_validated" if passthrough else "precheck_auto",
            "fail_phase": "",
            "warnings": warnings,
            "diagnostics": diag_out,
        }
    except Exception as e:
        return {
            "tool": tool,
            "input_model": (model_intent or "").strip(),
            "resolved_real_name": "",
            "model": None,
            "validated": False,
            "source": "error",
            "decision": "precheck_error",
            "fail_phase": "precheck_error",
            "warnings": [f"precheck_error:{type(e).__name__}"],
            "diagnostics": {},
        }
