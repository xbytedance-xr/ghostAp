"""TTADK 模型列表缓存（cache）。

目标：把 `src/ttadk/manager.py` 中与“模型列表缓存/落盘/TTL/refresh/预热/并发安全”相关的状态与实现集中到单独模块，
以便 `TTADKManager` 退化为轻量 façade。

设计约束：
- 不 import `src.ttadk.startup*`，避免循环依赖
- 仅依赖标准库 + `src.ttadk.models`/`src.ttadk.model_fetcher`
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from .model_fetcher import TTADKModelFetcher
from .models import ModelListResult, TTADKModel, build_model_list_diagnostics
from ..utils.errors import get_error_detail

logger = logging.getLogger(__name__)

__all__ = [
    "TTADKModelCache",
]


class TTADKModelCache:
    """TTADK 模型列表缓存/落盘/预热的单一实现体。"""

    def __init__(
        self,
        *,
        default_models: list[TTADKModel],
        # NOTE: 旧实现默认写入 ~/.ttadk/models_cache.json（服务侧会污染真实 HOME）。
        # 新实现允许 cache_file_path 为空，落盘路径由 cwd + Settings 统一决定。
        cache_file_path: Optional[Path] = None,
        cache_ttl_s: float = 300.0,
        model_fetcher: Optional[TTADKModelFetcher] = None,
        # NOTE: 为兼容历史测试的 monkeypatch（例如 monkeypatch `src.ttadk.manager.get_settings`），
        # cache 侧支持注入 get_settings_fn，避免硬编码依赖 `src.config.get_settings`。
        get_settings_fn=None,
    ) -> None:
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        self._default_models = list(default_models)
        # legacy fallback path (only used when caller does not pass cwd)
        self._cache_file_path = cache_file_path
        self._cache_ttl_s = float(cache_ttl_s or 0.0)

        self._model_fetcher = model_fetcher or TTADKModelFetcher()

        # settings 访问（可注入，便于测试）
        self._get_settings_fn = get_settings_fn

        # 允许由上层注入“持久化 hook”（便于测试中禁用 HOME 写入）
        self._persist_hook = None

        # 记录已尝试 load 的项目（按 cache 文件路径去重），避免每次 get_models 都触发磁盘读。
        self._loaded_cache_paths: set[str] = set()

        # tool -> models
        self._tool_models_cache: dict[str, list[TTADKModel]] = {}
        # tool -> {source, warnings}
        self._tool_models_meta: dict[str, dict] = {}
        # tool -> ts
        self._cache_time: dict[str, float] = {}

        # 预热状态（best-effort）
        self._preheat_once = threading.Event()
        self._preheated_tools: set[str] = set()
        self._preheat_inflight_tools: set[str] = set()
        self._preheat_last_attempt: dict[str, float] = {}
        self._preheat_cooldown_s: float = 60.0

        # 已知模型集合（用于 UI/校验）
        self._known_models: set[str] = set()

        try:
            self._model_fetcher.set_cache_sink(self._on_fetcher_cache_update)
        except Exception:
            logger.debug("call set_cache_sink", exc_info=True)

    def set_model_fetcher(self, fetcher: TTADKModelFetcher) -> None:
        """替换内部 model_fetcher，并重新绑定 cache_sink（best-effort）。"""
        if fetcher is None:
            return
        self._model_fetcher = fetcher
        try:
            self._model_fetcher.set_cache_sink(self._on_fetcher_cache_update)
        except Exception:
            logger.debug("set_model_fetcher: call set_cache_sink", exc_info=True)

    def _get_settings(self):
        """获取 Settings（兼容测试 monkeypatch，best-effort）。"""
        fn = None
        try:
            fn = self._get_settings_fn
        except Exception:
            fn = None
        if callable(fn):
            try:
                return fn()
            except Exception:
                logger.debug("_get_settings: return fn()", exc_info=True)
        from ..config import get_settings

        return get_settings()

    def set_persist_hook(self, hook) -> None:
        """设置持久化回调（无参 callable）。

        注意：hook 应尽量不抛异常；cache 内部按 best-effort 调用。
        """
        self._persist_hook = hook

    def _persist(self, *, cwd: Optional[str] = None) -> None:
        try:
            hook = self._persist_hook
        except Exception:
            hook = None
        if hook is None:
            self.save_to_file(cwd=cwd)
            return
        try:
            try:
                hook(cwd=cwd)
            except TypeError:
                # backward compat
                hook()
        except Exception:
            # best-effort
            return

    # ------------------------------
    # 路径 SSOT
    # ------------------------------

    def _resolve_project_cache_path(self, *, cwd: Optional[str]) -> Optional[Path]:
        """解析“项目级”缓存落盘路径。

        规则：
        - cwd 为空：禁用项目落盘（避免写入真实 HOME）
        - settings.ttadk_models_cache_path 为空：默认 "{cwd}/.ghostap/ttadk/models_cache.json"
        - 支持 "{cwd}" 占位符
        """
        base = (cwd or "").strip()
        if not base:
            return None

        # 约束：仅当调用方提供“明确的绝对路径”时才启用项目级落盘。
        # 目的：避免测试/错误调用方传入相对路径（例如 "."）导致写入仓库根目录并引入跨用例污染。
        try:
            if not Path(base).is_absolute():
                return None
        except Exception:
            logger.debug("_resolve_project_cache_path: evaluate condition", exc_info=True)
            return None

        # Guardrail: avoid reading/writing project cache directly under temp roots.
        # This prevents cross-test / cross-process pollution when callers accidentally
        # pass generic temp dirs like `/tmp` as cwd.
        try:
            base_path = Path(base)
            if base_path == Path("/tmp") or base_path == Path("/var/tmp"):
                return None
        except Exception:
            logger.debug("_resolve_project_cache_path: resolve path", exc_info=True)
        s = self._get_settings()
        raw = ""
        try:
            raw = str(getattr(s, "ttadk_models_cache_path", "") or "")
        except Exception:
            raw = ""

        # Default path guardrail:
        # If the caller didn't explicitly configure `ttadk_models_cache_path`, only enable
        # project-level persistence for directories that *look like a project*.
        # This avoids accidentally reading/writing shared locations like `/tmp`.
        if not raw.strip():
            try:
                base_path = Path(base)
                markers = [
                    base_path / ".git",
                    base_path / "pyproject.toml",
                    base_path / "package.json",
                    base_path / "go.mod",
                    base_path / "Cargo.toml",
                    base_path / ".ghostap",
                ]
                if not any(p.exists() for p in markers):
                    return None
            except Exception:
                logger.debug("resolve path", exc_info=True)
                return None
            raw = "{cwd}/.ghostap/ttadk/models_cache.json"
        try:
            raw = raw.format(cwd=base)
        except Exception:
            raw = raw.replace("{cwd}", base)
        try:
            p = Path(raw)
        except Exception:
            logger.debug("resolve path", exc_info=True)
            return None
        if not p.is_absolute():
            # 相对路径按 cwd 解析
            try:
                p = Path(base) / p
            except Exception:
                logger.debug("resolve path", exc_info=True)
                return None
        return p

    def _legacy_home_cache_path(self) -> Path:
        return Path.home() / ".ttadk" / "models_cache.json"

    def _resolve_cache_file_path(self, *, cwd: Optional[str]) -> Optional[Path]:
        """选择实际读写的 cache 文件路径。

        优先：项目级 cache（cwd + Settings）。
        回退：cache_file_path（仅用于历史测试/兼容调用方）。
        """
        p = self._resolve_project_cache_path(cwd=cwd)
        if p is not None:
            return p
        try:
            return self._cache_file_path
        except Exception:
            logger.debug("_resolve_cache_file_path: return self._cache_file_path", exc_info=True)
            return None

    # ------------------------------
    # 预热（best-effort）
    # ------------------------------

    def _parse_preheat_tools(self, raw: str) -> list[str]:
        raw = (raw or "").strip()
        if not raw:
            return []
        parts: list[str] = []
        for chunk in raw.replace(",", " ").split():
            name = (chunk or "").strip().lower()
            if name:
                parts.append(name)
        seen: set[str] = set()
        out: list[str] = []
        for x in parts:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _preheat_probe_and_cache(self, tool_name: str, *, cwd: Optional[str], timeout: float, reason: str) -> bool:
        tool = (tool_name or "").strip().lower()
        if not tool:
            return False
        now = time.time()
        with self._lock:
            if self._is_cache_valid(tool) and self._tool_models_cache.get(tool):
                self._preheated_tools.add(tool)
                return True
            if tool in self._preheated_tools:
                return True
            last = float(self._preheat_last_attempt.get(tool, 0.0) or 0.0)
            if last and (now - last) < float(self._preheat_cooldown_s or 0.0):
                return False
            if tool in self._preheat_inflight_tools:
                return False
            self._preheat_inflight_tools.add(tool)
            self._preheat_last_attempt[tool] = now

        try:
            models = self._model_fetcher.probe_tool_models(tool_name=tool, cwd=cwd, timeout=timeout)
        except Exception:
            logger.debug("_preheat_probe_and_cache: self._model_fetcher.probe_tool_...", exc_info=True)
            return False
        finally:
            with self._lock:
                self._preheat_inflight_tools.discard(tool)

        if not models:
            return False
        with self._lock:
            self._tool_models_cache[tool] = list(models)
            self._cache_time[tool] = time.time()
            self._known_models.update(m.name for m in models if getattr(m, "name", None))
            self._tool_models_meta[tool] = {"source": "probe", "warnings": []}
            self._preheated_tools.add(tool)
        self.save_to_file(cwd=cwd)
        logger.info("TTADK preheat ok: tool=%s count=%d timeout=%.2fs reason=%s", tool, len(models), timeout, reason)
        return True

    def maybe_preheat_common_models(self, cwd: Optional[str] = None) -> None:
        """常用工具预热（只执行一次，best-effort）。"""
        s = self._get_settings()
        if not getattr(s, "ttadk_preheat_enabled", True):
            return
        if not getattr(s, "ttadk_preheat_on_startup", True):
            return
        if not shutil.which("ttadk"):
            return

        tools = self._parse_preheat_tools(getattr(s, "ttadk_preheat_tools", ""))
        if not tools:
            return
        timeout = float(getattr(s, "ttadk_preheat_timeout", 2.5) or 0)
        if timeout <= 0:
            return

        # 重要：只有在确定会执行预热时才 set once。
        # 否则会导致开关关闭/工具列表为空时“错误地标记已预热”，影响后续逻辑与测试。
        if self._preheat_once.is_set():
            return
        with self._lock:
            if self._preheat_once.is_set():
                return
            self._preheat_once.set()

        for tool in tools:
            try:
                self._preheat_probe_and_cache(tool, cwd=cwd, timeout=timeout, reason="startup_common")
            except Exception:
                continue

    def maybe_preheat_tool_models(self, tool_name: str, cwd: Optional[str] = None) -> None:
        """首次使用某 tool 时触发预热（best-effort）。"""
        s = self._get_settings()
        if not getattr(s, "ttadk_preheat_enabled", True):
            return
        if not getattr(s, "ttadk_preheat_on_first_use", True):
            return
        if not shutil.which("ttadk"):
            return

        timeout = float(getattr(s, "ttadk_preheat_timeout", 2.5) or 0)
        if timeout <= 0:
            return
        self._preheat_probe_and_cache(tool_name, cwd=cwd, timeout=timeout, reason="first_use")

    def kickoff_preheat_common_models(self, cwd: Optional[str] = None) -> None:
        # 快速判断，避免在开关关闭时创建线程
        try:
            s = self._get_settings()
            if not getattr(s, "ttadk_preheat_enabled", True):
                return
            if not getattr(s, "ttadk_preheat_on_startup", True):
                return
            if not shutil.which("ttadk"):
                return
            timeout = float(getattr(s, "ttadk_preheat_timeout", 2.5) or 0)
            if timeout <= 0:
                return
        except Exception:
            # 保守：无法读取配置时不启动后台线程
            return
        try:
            if self._preheat_once.is_set():
                return
        except Exception:
            logger.debug("kickoff_preheat_common_models: evaluate condition", exc_info=True)
        try:
            t = threading.Thread(
                target=self.maybe_preheat_common_models,
                kwargs={"cwd": cwd},
                name="ttadk-preheat-common",
                daemon=True,
            )
            t.start()
        except Exception:
            logger.debug("kickoff_preheat_common_models: threading.Thread(", exc_info=True)
            return

    def kickoff_preheat_tool_models(self, tool_name: str, cwd: Optional[str] = None) -> None:
        # 快速判断，避免频繁创建线程
        try:
            s = self._get_settings()
            if not getattr(s, "ttadk_preheat_enabled", True):
                return
            if not getattr(s, "ttadk_preheat_on_first_use", True):
                return
            if not shutil.which("ttadk"):
                return
            timeout = float(getattr(s, "ttadk_preheat_timeout", 2.5) or 0)
            if timeout <= 0:
                return
            tool = (tool_name or "").strip().lower()
            if not tool:
                return
            now = time.time()
            with self._lock:
                if self._is_cache_valid(tool) and self._tool_models_cache.get(tool):
                    self._preheated_tools.add(tool)
                    return
                if tool in self._preheated_tools:
                    return
                last = float(self._preheat_last_attempt.get(tool, 0.0) or 0.0)
                if last and (now - last) < float(self._preheat_cooldown_s or 0.0):
                    return
                if tool in self._preheat_inflight_tools:
                    return
        except Exception:
            logger.debug("kickoff_preheat_tool_models: unexpected error", exc_info=True)
        try:
            t = threading.Thread(
                target=self.maybe_preheat_tool_models,
                args=(tool_name,),
                kwargs={"cwd": cwd},
                name=f"ttadk-preheat-{tool_name}",
                daemon=True,
            )
            t.start()
        except Exception:
            logger.debug("kickoff_preheat_tool_models: threading.Thread(", exc_info=True)
            return

    # ------------------------------
    # 基础能力
    # ------------------------------

    def _is_cache_valid(self, tool_name: str) -> bool:
        tool = (tool_name or "").strip().lower()
        if not tool:
            return False
        if self._cache_ttl_s <= 0:
            return False
        try:
            cache_time = float(self._cache_time.get(tool, 0.0) or 0.0)
        except Exception:
            cache_time = 0.0
        if not cache_time:
            return False
        return (time.time() - cache_time) < float(self._cache_ttl_s)

    def invalidate(self, tool_name: Optional[str] = None) -> None:
        tool = (tool_name or "").strip().lower()
        with self._lock:
            if tool:
                self._tool_models_cache.pop(tool, None)
                self._tool_models_meta.pop(tool, None)
                self._cache_time.pop(tool, None)
            else:
                self._tool_models_cache.clear()
                self._tool_models_meta.clear()
                self._cache_time.clear()

    def known_models(self) -> set[str]:
        with self._lock:
            return set(self._known_models)

    # ------------------------------
    # 文件缓存
    # ------------------------------

    def load_from_file(self) -> None:
        # Backward compat: keep old signature but default to "no cwd".
        return self.load_from_file_for_project(cwd=None)

    def load_from_file_for_project(self, *, cwd: Optional[str]) -> None:
        """按项目加载模型缓存。

        - 若项目 cache 存在：读取项目 cache（可信度更高）
        - 若项目 cache 不存在：可选读取 legacy HOME cache（低置信），并可选迁移到项目 cache
        """
        path = self._resolve_cache_file_path(cwd=cwd)
        if path is None:
            return
        key = str(path)
        with self._lock:
            if key in self._loaded_cache_paths:
                return
            self._loaded_cache_paths.add(key)

        def _apply_loaded(data: dict, *, low_confidence: bool) -> None:
            # 支持两种格式：
            # v1(legacy): {"tool": ["m1"|{"name":..., "friendly_name":...}, ...]}
            # v2: {"version": 2, "tools": {"tool": {"models": [{"model_id":..., "display_name":..., "aliases": [...]}, ...], "meta": {...}}}}

            tools_payload: dict[str, object]
            try:
                if isinstance(data, dict) and isinstance(data.get("tools"), dict):
                    tools_payload = dict(data.get("tools") or {})
                else:
                    tools_payload = dict(data or {})
            except Exception:
                tools_payload = {}

            with self._lock:
                for tool, tool_blob in (tools_payload or {}).items():
                    models_data: object
                    meta: dict = {}

                    if isinstance(tool_blob, dict) and isinstance(tool_blob.get("models"), list):
                        models_data = tool_blob.get("models")
                        try:
                            meta = dict(tool_blob.get("meta") or {})
                        except Exception:
                            meta = {}
                    else:
                        models_data = tool_blob

                    if not isinstance(models_data, list):
                        continue
                    models: list[TTADKModel] = []
                    for m in models_data:
                        if isinstance(m, dict):
                            try:
                                # v2 preferred keys
                                mid = m.get("model_id") or m.get("name")
                                if not isinstance(mid, str) or not mid.strip():
                                    continue
                                mid = mid.strip()
                                friendly = m.get("display_name") or m.get("friendly_name") or ""
                                if not isinstance(friendly, str):
                                    friendly = ""
                                desc = m.get("description") or ""
                                if not isinstance(desc, str):
                                    desc = ""
                                is_default = bool(m.get("is_default", False))

                                mm = TTADKModel(
                                    name=mid, description=desc, is_default=is_default, friendly_name=str(friendly or "")
                                )
                                # best-effort：保留 aliases（供 resolver 使用，TTADKModel 不声明该字段）
                                try:
                                    als = m.get("aliases")
                                    if isinstance(als, list):
                                        mm.aliases = [str(x) for x in als if str(x).strip()]
                                except Exception:
                                    logger.debug("get value", exc_info=True)
                                models.append(mm)
                            except Exception:
                                continue
                        elif isinstance(m, str):
                            name = (m or "").strip()
                            if name:
                                models.append(TTADKModel(name=name, description=name, friendly_name=name))
                    if not models:
                        continue
                    t = str(tool or "").strip().lower()
                    if not t:
                        continue
                    self._tool_models_cache[t] = models
                    self._cache_time[t] = time.time()
                    self._known_models.update(mm.name for mm in models if getattr(mm, "name", None))
                    ws: list[str] = []
                    if low_confidence:
                        ws = ["source_cross_project", "low_confidence"]
                    # meta 合并（文件内 meta 优先），并叠加 low_confidence 标记
                    src = "file_cache"
                    try:
                        src = str(meta.get("source") or src)
                    except Exception:
                        src = "file_cache"
                    try:
                        ws_file = [str(w) for w in (meta.get("warnings") or []) if w]
                    except Exception:
                        ws_file = []
                    ws2 = list(ws_file)
                    for w in ws:
                        if w not in ws2:
                            ws2.append(w)
                    self._tool_models_meta[t] = {
                        "source": src,
                        "warnings": ws2,
                    }

        # 1) project cache
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _apply_loaded(data, low_confidence=False)
                logger.info(
                    "Loaded TTADK models from project cache: path=%s tools=%d", str(path), len(self._tool_models_cache)
                )
                return
            except Exception as e:
                logger.warning("Failed to load TTADK models cache from project file: path=%s err=%s", str(path), get_error_detail(e))
                # best-effort: remove corrupted project cache
                try:
                    if path.exists():
                        path.unlink()
                except Exception:
                    logger.debug("evaluate condition", exc_info=True)

        # 2) legacy home cache (read-only unless migrating)
        # 约束：legacy HOME 仅在“明确项目 cwd”场景下才允许读取，避免无 cwd 时意外依赖/污染真实 HOME。
        if not (cwd or "").strip():
            return
        s = self._get_settings()
        allow_legacy = True
        migrate = True
        try:
            allow_legacy = bool(getattr(s, "ttadk_models_cache_read_legacy_home", True))
            migrate = bool(getattr(s, "ttadk_models_cache_migrate_from_legacy_home", True))
        except Exception:
            allow_legacy = True
            migrate = True
        if not allow_legacy:
            return

        legacy = self._legacy_home_cache_path()
        if not legacy.exists():
            return
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            _apply_loaded(data, low_confidence=True)
            logger.info("Loaded TTADK models from legacy HOME cache: path=%s", str(legacy))
        except Exception as e:
            logger.warning("Failed to load TTADK models cache from legacy HOME file: path=%s err=%s", str(legacy), get_error_detail(e))
            return

        # Optional migrate: write into project cache only (never write back to HOME).
        if migrate and cwd:
            try:
                self.save_to_file(cwd=cwd)
            except Exception:
                logger.debug("cwd)", exc_info=True)

    def save_to_file(self, *, cwd: Optional[str] = None) -> None:
        try:
            with self._lock:
                tools: dict[str, dict] = {}
                for tool, models in self._tool_models_cache.items():
                    xs: list[dict] = []
                    for m in models or []:
                        try:
                            aliases = list(getattr(m, "aliases", []) or [])
                        except Exception:
                            aliases = []
                        xs.append(
                            {
                                # v2
                                "model_id": str(getattr(m, "name", "") or ""),
                                "display_name": str(getattr(m, "friendly_name", "") or ""),
                                "aliases": [str(x) for x in aliases if str(x).strip()],
                                # extras (backward compatible)
                                "name": str(getattr(m, "name", "") or ""),
                                "description": str(getattr(m, "description", "") or ""),
                                "is_default": bool(getattr(m, "is_default", False)),
                                "friendly_name": str(getattr(m, "friendly_name", "") or ""),
                            }
                        )
                    try:
                        meta = dict(self._tool_models_meta.get(tool, {}) or {})
                    except Exception:
                        meta = {}
                    tools[str(tool)] = {"models": xs, "meta": meta}

                data: dict = {
                    "version": 2,
                    "tools": tools,
                }

            path = self._resolve_cache_file_path(cwd=cwd)
            if path is None:
                return

            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(path)
        except Exception as e:
            logger.warning("Failed to save models cache to file: %s", get_error_detail(e))

    # ------------------------------
    # fetcher 回灌
    # ------------------------------

    def _on_fetcher_cache_update(self, tool_name: str, models: list[TTADKModel], source: str, diagnostics) -> None:
        tool = (tool_name or "").strip().lower()
        if not tool or not models:
            return
        with self._lock:
            self._tool_models_cache[tool] = list(models)
            self._cache_time[tool] = time.time()
            self._known_models.update(m.name for m in models if getattr(m, "name", None))
            try:
                ws = list(getattr(diagnostics, "warnings", []) or [])
            except Exception:
                ws = []
            self._tool_models_meta[tool] = {
                "source": str(source or ""),
                "warnings": [str(w) for w in ws if w],
            }
        # 注意：fetcher 回灌不携带 cwd；为避免写入真实 HOME，这里不做落盘。
        # 真实落盘由上层（带 cwd 的调用链）在 get_models()/seed_*() 等路径中触发。
        self._persist(cwd=None)

    # ------------------------------
    # 业务接口：模型列表
    # ------------------------------

    def seed_models_from_error(self, tool_name: str, error_text: str) -> list[str]:
        from .models import extract_available_models

        tool = (tool_name or "").strip().lower()
        if not tool:
            return []
        names = extract_available_models(error_text or "")
        if not names:
            return []
        with self._lock:
            self._tool_models_cache[tool] = [TTADKModel(name=n, description=n, friendly_name=n) for n in names]
            self._cache_time[tool] = time.time()
            self._known_models.update(names)
            self._tool_models_meta[tool] = {"source": "probe_seed", "warnings": []}
        self._persist(cwd=None)
        return names

    def seed_models_from_invalid_model_runtime(
        self,
        *,
        tool_name: str,
        available_models: list[str],
        source: str = "runtime_invalid_model_seed",
    ) -> list[str]:
        tool = (tool_name or "").strip().lower()
        if not tool:
            return []
        names = [str(x).strip() for x in (available_models or []) if str(x).strip()]
        if not names:
            return []
        with self._lock:
            self._tool_models_cache[tool] = [TTADKModel(name=n, description=n, friendly_name=n) for n in names]
            self._cache_time[tool] = time.time()
            self._known_models.update(names)
            self._tool_models_meta[tool] = {"source": str(source or "runtime_invalid_model_seed"), "warnings": []}
        self._persist(cwd=None)
        return names

    def get_models(self, *, tool_name: str, cwd: Optional[str] = None, force_refresh: bool = False) -> ModelListResult:
        tool = (tool_name or "").strip().lower()
        if not tool:
            diag = build_model_list_diagnostics(
                source="defaults",
                cached=False,
                cache_ts=None,
                ttl_s=float(getattr(self, "_cache_ttl_s", 0.0) or 0.0),
                chosen_strategy="",
                attempts=[],
            )
            return ModelListResult(
                models=list(self._default_models),
                source="defaults",
                warnings=["models_untrusted", "missing_tool"],
                diagnostics=diag,
            )

        # best-effort：按项目加载一次磁盘缓存
        try:
            self.load_from_file_for_project(cwd=cwd)
        except Exception:
            logger.debug("get_models: cwd)", exc_info=True)

        # best-effort：首次使用触发后台预热
        try:
            self.kickoff_preheat_tool_models(tool, cwd=cwd)
        except Exception:
            logger.debug("get_models: cwd)", exc_info=True)

        if not force_refresh:
            cache_models: list[TTADKModel] | None = None
            cache_warnings: list[str] = []
            with self._lock:
                if self._is_cache_valid(tool):
                    ms = list(self._tool_models_cache.get(tool, []) or [])
                    if ms:
                        cache_models = ms
                        meta = dict(self._tool_models_meta.get(tool, {}) or {})
                        cache_warnings = [str(w) for w in (meta.get("warnings") or []) if w]

            if cache_models is not None:
                cache_ts = None
                try:
                    cache_ts = float(self._cache_time.get(tool, 0.0) or 0.0)
                except Exception:
                    cache_ts = None
                diag = build_model_list_diagnostics(
                    source="cache",
                    cached=True,
                    cache_ts=cache_ts,
                    ttl_s=float(getattr(self, "_cache_ttl_s", 0.0) or 0.0),
                    chosen_strategy="memory_cache",
                    attempts=[],
                )
                return ModelListResult(
                    models=list(cache_models),
                    cached=True,
                    source="cache",
                    warnings=list(cache_warnings),
                    diagnostics=diag,
                )

        # 慢路径：fetcher
        try:
            try:
                fetch_result = self._model_fetcher.fetch_tool_models_with_diagnostics(
                    tool,
                    cwd=cwd,
                    force_refresh=force_refresh,
                    prefer_probe=bool(force_refresh),
                )
            except TypeError:
                fetch_result = self._model_fetcher.fetch_tool_models_with_diagnostics(
                    tool, cwd=cwd, force_refresh=force_refresh
                )
        except Exception as e:
            diag = build_model_list_diagnostics(
                source="defaults",
                cached=False,
                cache_ts=None,
                ttl_s=float(getattr(self, "_cache_ttl_s", 0.0) or 0.0),
                chosen_strategy="fetch_exception",
                attempts=[],
                error_snippet=(get_error_detail(e))[:200],
            )
            return ModelListResult(
                models=list(self._default_models),
                cached=False,
                error=get_error_detail(e),
                source="defaults",
                warnings=["models_error"],
                diagnostics=diag,
            )

        fetched = list(fetch_result.models or [])
        if fetched:
            with self._lock:
                self._tool_models_cache[tool] = list(fetched)
                self._cache_time[tool] = time.time()
                self._known_models.update(m.name for m in fetched if getattr(m, "name", None))
                self._tool_models_meta[tool] = {
                    "source": str(fetch_result.source or ""),
                    "warnings": list(fetch_result.diagnostics.warnings or []),
                }
            self._persist(cwd=cwd)
            cache_ts = None
            try:
                cache_ts = float(self._cache_time.get(tool, 0.0) or 0.0)
            except Exception:
                cache_ts = None
            diag = build_model_list_diagnostics(
                source=str(fetch_result.source or "unknown"),
                cached=False,
                cache_ts=cache_ts,
                ttl_s=float(getattr(self, "_cache_ttl_s", 0.0) or 0.0),
                chosen_strategy=str(fetch_result.diagnostics.chosen_strategy or ""),
                attempts=list(fetch_result.diagnostics.attempts or []),
            )
            return ModelListResult(
                models=list(fetched),
                cached=False,
                source=fetch_result.source or "unknown",
                warnings=list(fetch_result.diagnostics.warnings or []),
                diagnostics=diag,
            )

        diag = build_model_list_diagnostics(
            source="defaults",
            cached=False,
            cache_ts=None,
            ttl_s=float(getattr(self, "_cache_ttl_s", 0.0) or 0.0),
            chosen_strategy=str(fetch_result.diagnostics.chosen_strategy or ""),
            attempts=list(fetch_result.diagnostics.attempts or []),
        )
        return ModelListResult(
            models=list(self._default_models),
            cached=False,
            source="defaults",
            warnings=["models_untrusted"],
            diagnostics=diag,
        )
