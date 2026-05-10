import asyncio
import logging
import threading
import time as _time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from acp.stdio import spawn_agent_process

from .client import GhostAPClient
from .providers import tool_registry
from ..config import get_settings
from ..ttadk.models import ACPModelOption, ACPToolOption
from ..utils.async_helpers import safe_wait_for
from ..utils.text import get_acp_result_header_text

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Generic ACP model probe cache for non-coco tools (Aiden, Codex, Gemini…).
# Key: tool_name, Value: (timestamp, model_list).  TTL aligned with CocoModelManager.
# ---------------------------------------------------------------------------
_acp_probe_cache: dict[str, tuple[float, list[ACPModelOption]]] = {}
_acp_probe_cache_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
_ACP_PROBE_CACHE_TTL = 300  # 5 minutes


def _get_cached_probe(tool_name: str) -> list[ACPModelOption]:
    """Return cached probe result if within TTL, else empty list."""
    with _acp_probe_cache_lock:
        entry = _acp_probe_cache.get(tool_name)
    if not entry:
        return []
    ts, models = entry
    if (_time.time() - ts) > _ACP_PROBE_CACHE_TTL:
        return []
    return list(models)


def _set_cached_probe(tool_name: str, models: list[ACPModelOption]) -> None:
    """Store a successful probe result in cache."""
    if not models:
        return
    with _acp_probe_cache_lock:
        _acp_probe_cache[tool_name] = (_time.time(), list(models))


def list_acp_tools() -> list[ACPToolOption]:
    """List available ACP tools.

    使用共享文案层提供的工具描述文案，避免直接依赖 card.styles.UI_TEXT。
    """

    names = ["coco", "claude", "aiden", "codex", "gemini"]
    out: list[ACPToolOption] = []
    headers = get_acp_result_header_text()

    for name in names:
        provider = tool_registry.get_provider(name)
        if not provider:
            continue
        try:
            available = bool(provider.check_availability())
        except Exception:
            logger.debug("[ACP] availability check failed for %s", name, exc_info=True)
            available = False
        if available:
            desc = headers.get(f"tool_desc_{name}") or name
            out.append(
                ACPToolOption(name=name, description=desc, is_default=(name == "coco"))
            )
    return out


def fetch_acp_models(
    tool_name: str,
    cwd: Optional[str],
    current_model: Optional[str] = None,
    probe_timeout: Optional[float] = None,
) -> list[ACPModelOption]:
    """Synchronous wrapper to probe available models from an ACP provider.

    For Coco, prefer the cached list maintained by ``CocoModelManager`` (the
    same source ``show_coco_status`` and the agent session bootstrap rely on).
    A successful probe there is cached for 5 minutes, so subsequent /wt and
    /model clicks reuse the real ACP model list instead of degrading to the
    6-entry static ``DEFAULT_MODELS`` fallback.
    """
    if tool_name == "coco":
        cached = _coco_models_from_manager(current_model)
        if cached:
            return cached

    try:
        timeout_s = _resolve_acp_model_probe_timeout(probe_timeout)
        models = asyncio.run(
            safe_wait_for(
                probe_acp_models(tool_name, cwd, current_model),
                timeout=timeout_s,
                action=f"ACP {tool_name} 模型探测",
            )
        )
    except Exception:
        logger.warning("[ACP] probe models failed for %s, will fallback", tool_name, exc_info=True)
        models = []

    if models:
        # Cache successful probe for non-coco tools
        if tool_name != "coco":
            _set_cached_probe(tool_name, models)
        return models

    # Fallback for coco — try CocoModelManager again (probe inside it may have
    # populated cache concurrently) before degrading to DEFAULT_MODELS.
    if tool_name == "coco":
        cached = _coco_models_from_manager(current_model)
        if cached:
            return cached
        try:
            from ..coco_model.manager import DEFAULT_MODELS

            logger.warning(
                "[ACP] coco ACP probe returned no models, falling back to %d static DEFAULT_MODELS",
                len(DEFAULT_MODELS),
            )
            target_default = _coco_target_default(current_model)
            return [
                ACPModelOption(
                    name=m.name,
                    description=m.description,
                    is_default=bool(
                        (target_default and m.name == target_default)
                        or getattr(m, "is_default", False)
                    ),
                )
                for m in DEFAULT_MODELS
                if getattr(m, "name", "")
            ]
        except Exception:
            logger.warning("[ACP] coco model fallback failed", exc_info=True)

    # Generic cache fallback for non-coco tools
    if tool_name != "coco":
        cached = _get_cached_probe(tool_name)
        if cached:
            logger.debug("[ACP] using cached probe for %s (%d models)", tool_name, len(cached))
            if current_model:
                for m in cached:
                    m.is_default = (m.name == current_model)
            return cached

    if current_model:
        return [
            ACPModelOption(
                name=str(current_model), description=str(current_model), is_default=True
            )
        ]

    return []


def _coco_target_default(current_model: Optional[str]) -> str:
    """Resolve the model name to mark as default for Coco rendering."""
    try:
        from ..coco_model import get_coco_model_manager

        configured_current = None
        try:
            configured_current = get_coco_model_manager().get_current_model()
        except Exception:
            logger.debug("[ACP] coco current model lookup failed", exc_info=True)
        return str(current_model or configured_current or "").strip()
    except Exception:
        return str(current_model or "").strip()


def _coco_models_from_manager(current_model: Optional[str]) -> list[ACPModelOption]:
    """Read Coco models from ``CocoModelManager`` (cache + ACP probe + static).

    Returns the same list ``/coco_status`` and the agent bootstrap rely on,
    so the worktree model card stays in sync with the rest of the system.
    Returns an empty list when CocoModelManager has not yet populated and
    the caller should still attempt a fresh probe.
    """
    try:
        from ..coco_model import get_coco_model_manager
        from ..coco_model.manager import DEFAULT_MODELS

        manager = get_coco_model_manager()
        result = manager.get_models()
        models = list(result.models or [])
        if not models:
            return []
        # If manager only had time to return the static defaults (probe failed
        # or never ran), let the caller try a fresh ACP probe; we can come back
        # to manager later if probe also fails.
        default_names = {m.name for m in DEFAULT_MODELS}
        unique_names = {m.name for m in models}
        if unique_names == default_names:
            return []
        target_default = _coco_target_default(current_model)
        out: list[ACPModelOption] = []
        for m in models:
            name = str(getattr(m, "name", "") or "").strip()
            if not name:
                continue
            description = str(getattr(m, "description", "") or name)
            is_default = bool(
                (target_default and name == target_default)
                or getattr(m, "is_default", False)
            )
            out.append(
                ACPModelOption(name=name, description=description, is_default=is_default)
            )
        return out
    except Exception:
        logger.debug("[ACP] coco manager lookup failed", exc_info=True)
        return []


def _resolve_acp_model_probe_timeout(probe_timeout: Optional[float] = None) -> float:
    """Resolve the probe timeout for fetch_acp_models.

    Prefer ``acp_model_probe_timeout`` (designed for full model-list probing),
    fall back to the legacy ``acp_healthcheck_timeout`` for backwards-compat
    when the dedicated setting is unset.
    """
    if probe_timeout is not None:
        return max(0.1, float(probe_timeout))
    try:
        settings = get_settings()
        configured = float(
            getattr(settings, "acp_model_probe_timeout", None)
            or getattr(settings, "acp_healthcheck_timeout", 2.0)
            or 2.0
        )
    except Exception:
        configured = 6.0
    return max(0.1, configured)


class SessionKeyCodec:
    """`session_key` 编解码协作者。

    设计目标：
    - 将会话路由使用的 `session_key` 字符串协议（chat/project/thread）集中到
      单一位置，避免在各处手写字符串拼接或拆分逻辑；
    - 保持与现有 :class:`ACPSessionManager` 中 `_session_key` /
      `_parse_session_key` 语义等价，包括默认项目占位符与旧格式兼容策略；
    - 提供面向调用方的显式类型签名，便于在测试中做 roundtrip 与异常路径
      覆盖，同时为后续迁移 Lint 提供目标入口。

    注意：
    - 本类仅负责「字符串协议 ↔ 结构化三元组(chat_id, project_id, thread_id)」
      的转换，不做持久化、日志打印或安全校验；
    - 默认项目的占位符常量应与 ACPSessionManager 中使用的值保持一致，
      后续在完成迁移后会以本类为 SSOT。
    """

    #: 默认 project 段占位符；必须与 ACPSessionManager 中的 `_DEFAULT_PROJECT`
    #: 保持一致，以确保旧 key 仍能被正确解析。
    DEFAULT_PROJECT_PLACEHOLDER = "_default_"

    @classmethod
    def encode(
        cls,
        chat_id: str,
        project_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> str:
        """根据 chat/project/thread 构造用于内部路由的 `session_key`。

        约定：
        - `chat_id` 始终位于首段且不可为空字符串；
        - 第二段为 project 占位：有显式 project 时使用其字符串形式；否则使用
          `_default_` 占位，调用方通过 `project_id is None` 语义区分；
        - 存在 `thread_id` 时，在 project 段之后追加 `":t:"` 前缀形成
          `chat:project:t:thread_id`，以支持同 chat+project 下多线程隔离；
        - 所有输入均通过 `f"{value}"` 做字符串化，不做字符合法性归一化。
        """

        # 与现有实现保持一致的字符串化策略
        base = f"{chat_id}:{project_id}" if project_id else f"{chat_id}:{cls.DEFAULT_PROJECT_PLACEHOLDER}"
        if thread_id:
            return f"{base}:t:{thread_id}"
        return base

    @classmethod
    def decode(cls, key: str) -> tuple[str, Optional[str], Optional[str]]:
        """将 `session_key` 解析为 `(chat_id, project_id, thread_id)`。

        兼容约束：
        - 对称性：与 :meth:`encode` 的编码协议保持对称；
        - 宽进严出：对于历史/异常 key 保持鲁棒而不抛异常，极端场景下返回
          `("", None, None)` 或 `(key, None, None)` 保留可追踪信息；
        - 线程维度采用 `":t:"` 前缀的标准编码格式。
        """

        try:
            s = str(key or "")
        except Exception:
            # 极端兜底：保证返回可打印 chat_id
            return "", None, None

        if not s:
            return "", None, None

        try:
            parts = s.split(":")
            if not parts:
                return "", None, None

            chat_id = parts[0] or ""

            project_id: Optional[str] = None
            if len(parts) >= 2:
                raw_project = parts[1] or ""
                if raw_project and raw_project != cls.DEFAULT_PROJECT_PLACEHOLDER:
                    project_id = raw_project

            thread_id: Optional[str] = None
            # 标准编码：chat:project:t:thread_id
            if len(parts) >= 4 and parts[2] == "t":
                thread_id = parts[3] or ""

            return chat_id, project_id, thread_id
        except Exception:
            # 解析失败时，保留原始 key 作为 chat_id，避免完全丢失上下文
            return s, None, None


async def probe_acp_models(
    tool_name: str, cwd: Optional[str], current_model: Optional[str] = None
) -> list[ACPModelOption]:
    """Asynchronously probe available models from an ACP provider."""
    provider = tool_registry.get_provider(tool_name)
    if not provider:
        return []

    cmd, args = provider.get_serve_command(None)

    from ..utils.env import build_clean_env

    env = build_clean_env()
    client = GhostAPClient(on_event=lambda _ev: None, auto_approve=True)

    try:
        async with spawn_agent_process(
            client, cmd, *args, env=env, cwd=(cwd or str(Path.cwd()))
        ) as (conn, _proc):
            await conn.initialize(protocol_version=1)
            resp = await conn.new_session(cwd=(cwd or str(Path.cwd())))
            models_state = getattr(resp, "models", None)
            available = list(getattr(models_state, "available_models", []) or [])
            current_id = str(
                getattr(models_state, "current_model_id", "")
                or getattr(models_state, "currentModelId", "")
            )
            target_default = str((current_model or current_id or "")).strip()

            items = []
            seen = set()
            for item in available:
                model_id = str(
                    getattr(item, "model_id", "")
                    or getattr(item, "modelId", "")
                    or getattr(item, "name", "")
                ).strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                description = str(
                    getattr(item, "description", "")
                    or getattr(item, "name", "")
                    or model_id
                ).strip()
                items.append(
                    ACPModelOption(
                        name=model_id,
                        description=description,
                        is_default=(model_id == target_default),
                    )
                )
            return items
    except Exception as e:
        from ..utils.errors import get_error_detail

        logger.info(
            "[ACP] fetch models failed: tool=%s err=%s", tool_name, get_error_detail(e)
        )
        return []
