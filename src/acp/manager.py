"""ACP Session Manager — manages per-chat, per-project ACP sessions.

Sessions are keyed by (chat_id, project_id) to ensure full isolation between
projects within the same chat.  When project_id is not provided, a default
suffix is used for backward compatibility.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .sync_adapter import SyncACPSession
from .sync_adapter import build_startup_diagnostics, format_startup_diagnostics
from .diagnostics import (
    format_startup_failure_log_line,
    get_diagnostics_config,
    redact_text,
    truncate_text,
)
from ..agent_session import SyncClaudeCLISession, SyncSession
from ..config import get_settings

logger = logging.getLogger(__name__)


def _format_error_type_and_repr(err: object) -> tuple[str, str]:
    """兼容入口：历史调用点需要 err_type/err_repr。

    新 SSOT 在 `src.acp.diagnostics.format_startup_failure_log_line`。
    这里保留函数名以降低回归风险。
    """
    try:
        err_type = type(err).__name__
    except Exception:
        err_type = "Exception"
    try:
        err_repr = repr(err)
    except Exception:
        err_repr = ""
    if not (err_repr or "").strip():
        err_repr = f"<{err_type}>"
    return (err_type or "Exception", err_repr)


def _format_ttadk_startup_attempts(diagnostics: object, *, per_item_limit: int = 300, total_limit: int = 1600) -> str:
    """TTADK 启动 attempts 摘要（compat wrapper）。

    说明：脱敏/截断/配置读取的 SSOT 在 `src.acp.diagnostics`，本函数仅做 best-effort 薄封装，
    以保持现有日志调用点与函数名稳定。
    """
    try:
        from .diagnostics import format_attempts_summary

        diag = diagnostics if isinstance(diagnostics, dict) else {}
        attempts = (diag.get("attempts") or []) if isinstance(diag, dict) else []
        return format_attempts_summary(attempts, per_item_limit=per_item_limit, total_limit=total_limit, get_settings_fn=get_settings)
    except Exception:
        return ""


def _sanitize_startup_detail(text: str) -> str:
    """Redact and truncate startup detail for safe logging/user-facing errors."""
    s = str(text or "")
    if not s:
        return ""
    try:
        cfg = get_diagnostics_config(get_settings_fn=get_settings)
        if bool(getattr(cfg, "redact_enabled", True)):
            s = redact_text(
                s,
                list(getattr(cfg, "redact_patterns", []) or []),
                str(getattr(cfg, "redact_replacement", "***REDACTED***") or "***REDACTED***"),
            )
        lim = int(getattr(cfg, "snippet_limit", 240) or 240)
        s = truncate_text(s, max(1, lim))
    except Exception:
        pass
    return s


def _coco_acp_args(model_name: Optional[str]) -> list[str]:
    args: list[str] = ["acp", "serve"]
    if model_name:
        args.extend(["-c", f"model.name={model_name}"])
    return args


def _degrade_ttadk_to_coco_acp(
    *,
    agent_type: str,
    cwd: str,
    startup_timeout: float,
    reason: Exception,
) -> tuple[SyncSession, str]:
    """TTADK 启动失败时的确定性降级：使用 coco ACP 作为 agent_cmd/args 覆盖。

    注意：保留 session 的 _agent_type=ttadk_*，避免 ensure_session 因 agent_type 变化而反复重启。
    """
    from ..coco_model import get_coco_model_manager

    fallback_model = get_coco_model_manager().get_current_model()
    s = SyncACPSession(
        agent_type=agent_type,
        cwd=cwd or ".",
        agent_cmd="coco",
        agent_args=_coco_acp_args(fallback_model),
    )
    sid = s.start(startup_timeout=float(startup_timeout or 60))
    setattr(s, "_degraded_to", "coco")
    # Best-effort: keep a non-empty, user-facing reason summary.
    # Prefer structured diagnostics (fail_reason + error_text/stderr_snippet), fall back to repr.
    try:
        d = build_startup_diagnostics(
            agent_type=agent_type,
            cwd=cwd or ".",
            model_name=None,
            session=None,
            error=reason,
            timeout_s=float(startup_timeout or 0),
        )
        fr = str((d or {}).get("fail_reason") or (d or {}).get("fail_phase") or "start_failed")
        et = str((d or {}).get("error_text") or (d or {}).get("stderr_snippet") or (d or {}).get("error") or "")
        fr = (fr or "").strip() or "start_failed"
        et = (et or "").strip() or (repr(reason) if reason is not None else "<Exception> (empty)")
        setattr(s, "_degraded_reason", f"{fr}: {et}")
    except Exception:
        setattr(s, "_degraded_reason", str(reason) or (repr(reason) if reason is not None else ""))
    return (s, sid)


def _build_startup_diagnostics(
    session: Optional[SyncSession],
    agent_type: str,
    cwd: str,
    model_name: Optional[str],
    timeout: float,
    error: Exception,
) -> dict:
    """兼容入口：收敛到 SSOT（src.acp.sync_adapter.build_startup_diagnostics）。"""
    try:
        return build_startup_diagnostics(
            agent_type=agent_type,
            cwd=cwd,
            model_name=model_name,
            session=session,
            error=error,
            timeout_s=float(timeout or 0),
        )
    except Exception:
        # 极端兜底：保证返回可序列化 dict
        return {
            "agent_type": agent_type or "",
            "cwd": cwd or "",
            "model": model_name or "",
            "timeout_s": float(timeout or 0),
            "error_type": type(error).__name__,
            "error": str(error) if error is not None else "(empty)",
            "cmd": "",
            "args": [],
            "rc": None,
            "stdout_snippet": "",
            "stderr_snippet": "",
        }

_DEFAULT_PROJECT = "_default_"


class ACPSessionManager:
    """Manages per-chat, per-project sessions for a specific agent type.

    - Coco: ACP backend (SyncACPSession)
    - Claude: CLI backend (SyncClaudeCLISession)
    """

    def __init__(
        self,
        agent_type: str,
        session_timeout: int = 86400,
        session_starter: Optional[Callable[..., tuple[SyncSession, str, dict]]] = None,
    ):
        self._agent_type = agent_type  # "coco" / "claude"
        self._sessions: dict[str, SyncSession] = {}  # key = _session_key(...)
        self._session_timeout = session_timeout
        self._lock = threading.Lock()
        # 可注入启动器：用于将 TTADK 启动编排与 ACPSessionManager 解耦。
        # 约定：返回 (session, session_id, diagnostics_dict)。
        self._session_starter = session_starter

    @staticmethod
    def _session_key(chat_id: str, project_id: Optional[str] = None) -> str:
        """Compute the session dict key."""
        return f"{chat_id}:{project_id}" if project_id else f"{chat_id}:{_DEFAULT_PROJECT}"

    def start_session(
        self,
        chat_id: str,
        cwd: str = "",
        session_id: Optional[str] = None,
        startup_timeout: float = 60,
        project_id: Optional[str] = None,
        agent_type_override: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> SyncSession:
        """Start a new session for a chat/project."""
        key = self._session_key(chat_id, project_id)
        # Close existing session if any (under lock to prevent concurrent create)
        with self._lock:
            if key in self._sessions:
                self._end_session_unlocked(key)

        settings = get_settings()
        retries = int(getattr(settings, "acp_startup_retries", 2) or 2)
        retries = max(1, retries)
        effective_agent_type = (agent_type_override or self._agent_type).lower()

        # 可注入启动器（优先）：允许上层把启动编排从 manager 中抽离。
        # 重要：该注入点仅负责“启动并返回 (session, session_id, diagnostics)”，
        # 失败诊断的日志格式仍由本模块与 `format_startup_failure_log_line` 统一控制。
        if callable(self._session_starter):
            try:
                session, actual_id, _diag = self._session_starter(
                    agent_type=effective_agent_type,
                    cwd=cwd or ".",
                    startup_timeout=float(startup_timeout or 60),
                    model_name=model_name,
                    session_id=session_id,
                    project_id=project_id,
                )
                if session and actual_id:
                    # best-effort：保留可读 agent spec
                    try:
                        last_spec = session.describe_agent()
                    except Exception:
                        last_spec = ""
            except Exception:
                # 注入启动器出错时，回退到内置逻辑（保持兼容/不引入回归）。
                session = None
                actual_id = ""

        if effective_agent_type == "claude":
            # CLI backend doesn't need handshake retries.
            retries = 1

        last_err: Exception | None = None
        session: SyncSession | None = None
        actual_id = ""
        last_spec = ""

        # TTADK/ACP: 统一归一化 cwd，避免传入 "." 导致项目级缓存不落盘。
        try:
            from ..utils.path import normalize_ttadk_cwd

            raw_cwd = cwd
            norm_cwd = normalize_ttadk_cwd(raw_cwd)
            cwd = norm_cwd or raw_cwd
            try:
                if bool(getattr(get_settings(), "ttadk_cwd_debug_enabled", False)):
                    logger.debug("[TTADK:CWD] where=%s raw_cwd=%r normalized_cwd=%r", "acp.manager.ensure_session", raw_cwd, norm_cwd)
            except Exception:
                pass
        except Exception:
            pass

        # TTADK: Use SyncTTADKCLISession directly (CLI backend), avoiding ACP complexity.
        if effective_agent_type.startswith("ttadk_") and (not session or not actual_id):
            try:
                from ..ttadk.startup_common import precheck_ttadk_startup_model
                from ..ttadk import get_ttadk_manager
                from ..agent_session import SyncTTADKCLISession

                ttadk_manager = get_ttadk_manager()
                
                # Precheck model intent
                info = precheck_ttadk_startup_model(
                    agent_type=effective_agent_type,
                    cwd=cwd or ".",
                    model_intent=model_name,
                    manager=ttadk_manager,
                )
                
                resolved_model = info.get("model")
                
                session = SyncTTADKCLISession(
                    agent_type=effective_agent_type,
                    cwd=cwd or ".",
                    model_name=resolved_model
                )
                actual_id = session.start()
                
                logger.info(
                    "[ACP:%s] TTADK CLI Session started: key=%s, session=%s, model=%s",
                    effective_agent_type.upper(), key[-16:], actual_id[:8], resolved_model
                )
                
            except Exception as e:
                last_err = e
                detail = str(last_err or "").strip() if last_err else ""
                if not detail and last_err is not None:
                    for k in ("stderr_snippet", "stdout_snippet", "stderr", "stdout"):
                        try:
                            v = str(getattr(last_err, k, "") or "").strip()
                            if v:
                                detail = v
                                break
                        except Exception:
                            continue
                if not detail:
                    _, err_repr = _format_error_type_and_repr(last_err)
                    detail = err_repr or "unknown"
                safe_detail = _sanitize_startup_detail(detail) or "start_failed"
                logger.warning("TTADK CLI startup failed: %s", safe_detail)
                raise RuntimeError(f"启动 {effective_agent_type} CLI 失败: {safe_detail}")

        # Retry spawning agent process + handshake, since ACP CLI may be temporarily unavailable.
        if not session or not actual_id:
            effective_timeout = float(startup_timeout or 60)
            for attempt in range(1, retries + 1):
                try:
                    if effective_agent_type == "claude":
                        session = SyncClaudeCLISession(cwd=cwd or ".")
                    else:
                        # Backward-compatible construction: older tests/fakes may not accept model_name kw.
                        if model_name:
                            try:
                                session = SyncACPSession(
                                    agent_type=effective_agent_type,
                                    cwd=cwd or ".",
                                    model_name=model_name,
                                )
                            except TypeError:
                                session = SyncACPSession(agent_type=effective_agent_type, cwd=cwd or ".")
                        else:
                            session = SyncACPSession(agent_type=effective_agent_type, cwd=cwd or ".")

                    try:
                        last_spec = session.describe_agent()
                    except Exception:
                        last_spec = ""

                    # Progressive timeout: allow more time on later attempts.
                    effective_timeout = float(startup_timeout) * (1.0 + 0.5 * (attempt - 1))
                    actual_id = session.start(startup_timeout=effective_timeout)
                    logger.info(
                        "[ACP:%s] Session started: key=%s, session=%s (attempt=%d/%d)",
                        effective_agent_type.upper(), key[-16:], actual_id[:8], attempt, retries,
                    )
                    break
                except Exception as e:
                    last_err = e
                    # SSOT: 统一诊断构造入口（确保稳定字段存在）
                    diag = build_startup_diagnostics(
                        agent_type=effective_agent_type,
                        cwd=cwd or ".",
                        model_name=model_name,
                        session=session,
                        error=e,
                        attempt=int(attempt),
                        retries=int(retries),
                        timeout_s=float(effective_timeout or 0),
                    )

                    # 兼容运行期老日志格式：保证 error_text 非空（避免出现 `...: ` 空原因）
                    # 注意：真正的 SSOT 是 format_startup_failure_log_line，但历史日志仍依赖
                    # `logger.warning("...: %s", str(e))` 风格；这里确保 `str(e)` 可读。
                    try:
                        if isinstance(diag, dict):
                            et = str(diag.get("error_text") or "").strip()
                            if et:
                                # Best-effort: make `str(e)` informative even when __str__ is empty.
                                # RuntimeError/Exception are mutable enough for this pattern.
                                try:
                                    if not (str(e) or "").strip() or (str(e) or "").strip() in ("(empty)", "None"):
                                        e.args = (et,)
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    # 统一失败日志（SSOT=src.acp.diagnostics.format_startup_failure_log_line）
                    logger.warning(
                        format_startup_failure_log_line(
                            agent_type=effective_agent_type,
                            event="Session start failed",
                            attempt=int(attempt),
                            retries=int(retries),
                            error=e,
                            diag=diag if isinstance(diag, dict) else None,
                            attempts=(diag.get("attempts") if isinstance(diag, dict) else None),
                            get_settings_fn=get_settings,
                        )
                    )

                    try:
                        if session:
                            session.close()
                    except Exception:
                        pass
                    session = None
                    if attempt < retries:
                        # small backoff
                        time.sleep(min(2.0, 0.3 * attempt))

        if not session or not actual_id:
            detail = str(last_err) if last_err else "unknown"
            spec = f" ({last_spec})" if last_spec else ""
            kind = "会话" if effective_agent_type == "claude" else "ACP Server"
            raise RuntimeError(f"启动 {effective_agent_type} {kind} 失败{spec}（已重试 {retries} 次）: {detail}")

        # If caller wants a specific session_id (resume), load it
        if session_id:
            try:
                session.load_session(session_id)
                session.session_id = session_id
                session.is_resumed = True
            except Exception as e:
                logger.warning("[ACP:%s] Failed to load session %s, using new: %s",
                               effective_agent_type.upper(), session_id[:8], e)

        # Load local persisted history (best-effort)
        try:
            session.load_local_history(session.session_id)
        except Exception:
            pass

        with self._lock:
            self._sessions[key] = session
        return session

    def ensure_session(
        self,
        chat_id: str,
        cwd: str = "",
        session_id: Optional[str] = None,
        startup_timeout: float = 60,
        project_id: Optional[str] = None,
        agent_type_override: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> SyncSession:
        """Ensure a session exists and it is ready.

        1) Detect whether current backend is alive/healthy (if applicable).
        2) If not alive / missing / timed out, auto-start a new session.
        3) Optionally load a given session_id (resume) after startup.
        """
        key = self._session_key(chat_id, project_id)
        existing = self._sessions.get(key)
        if existing:
            # Timeout check (reuse get_session semantics)
            if time.time() - existing.last_active > self._session_timeout:
                logger.info("[ACP:%s] Session timeout before ensure: key=%s",
                            self._agent_type.upper(), key[-16:])
                self.end_session(chat_id, project_id=project_id)
                existing = None

        # Agent type / model mismatch for dynamic backends (e.g., TTADK)
        if existing and agent_type_override:
            existing_agent = getattr(existing, "_agent_type", "")
            if existing_agent and existing_agent.lower() != agent_type_override.lower():
                logger.info(
                    "[ACP:%s] Agent type changed (%s -> %s), restarting: key=%s",
                    self._agent_type.upper(),
                    existing_agent,
                    agent_type_override,
                    key[-16:],
                )
                self.end_session(chat_id, project_id=project_id)
                existing = None
            elif model_name:
                # TTADK: model_name 可能是“意图/友好名”，未必会透传 -m；仅当能解析出 validated 的真实模型名时才做一致性重启。
                if agent_type_override.lower().startswith("ttadk_"):
                    # 若该 session 已因 TTADK 启动失败降级（例如降级到 coco ACP），则不要再因 model mismatch 触发重启，
                    # 否则在 TTADK 不可用时会产生“每次 ensure 都重启→再失败→再降级”的抖动。
                    target_model: Optional[str] = None
                    if not getattr(existing, "_degraded_to", ""):
                        try:
                            from ..ttadk import get_ttadk_manager
                            from ..ttadk.startup_common import precheck_ttadk_startup_model

                            ttadk_manager = get_ttadk_manager()
                            pre = precheck_ttadk_startup_model(
                                agent_type=agent_type_override,
                                cwd=cwd or ".",
                                model_intent=model_name,
                                manager=ttadk_manager,
                            )
                            if bool(pre.get("validated")):
                                target_model = str(pre.get("model") or "").strip() or None
                        except Exception:
                            target_model = None

                    if target_model:
                        existing_args = getattr(existing, "_agent_args", None)
                        args_text = " ".join(existing_args or [])
                        if target_model not in args_text:
                            logger.info(
                                "[ACP:%s] TTADK model changed (missing %s), restarting: key=%s",
                                self._agent_type.upper(),
                                target_model,
                                key[-16:],
                            )
                            self.end_session(chat_id, project_id=project_id)
                            existing = None
                else:
                    existing_args = getattr(existing, "_agent_args", None)
                    args_text = " ".join(existing_args or [])
                    if model_name not in args_text:
                        logger.info(
                            "[ACP:%s] Model changed (missing %s), restarting: key=%s",
                            self._agent_type.upper(),
                            model_name,
                            key[-16:],
                        )
                        self.end_session(chat_id, project_id=project_id)
                        existing = None

        if existing:
            idle = time.time() - existing.last_active
            # Quick process-alive check first (no RPC); full health only after prolonged idle
            if not existing.is_server_running():
                logger.warning(
                    "[ACP:%s] Detected dead ACP server, restarting: key=%s session=%s",
                    self._agent_type.upper(), key[-16:], (existing.session_id or "none")[:8],
                )
                self.end_session(chat_id, project_id=project_id)
                existing = None
            elif idle > 30.0:
                health_to = float(getattr(get_settings(), "acp_healthcheck_timeout", 2.0) or 2.0)
                if not existing.is_server_healthy(healthcheck_timeout=health_to):
                    logger.warning(
                        "[ACP:%s] Detected unhealthy ACP server, restarting: key=%s session=%s",
                        self._agent_type.upper(), key[-16:], (existing.session_id or "none")[:8],
                    )
                    self.end_session(chat_id, project_id=project_id)
                    existing = None

        if existing and session_id and existing.session_id != session_id:
            # Different target session requested; restart to load requested session.
            self.end_session(chat_id, project_id=project_id)
            existing = None

        if existing:
            return existing

        return self.start_session(
            chat_id,
            cwd=cwd,
            session_id=session_id,
            startup_timeout=startup_timeout,
            project_id=project_id,
            agent_type_override=agent_type_override,
            model_name=model_name,
        )

    def resume_session(self, chat_id: str, session_id: str, cwd: str = "", project_id: Optional[str] = None) -> SyncSession:
        """Resume an existing session by session_id."""
        return self.start_session(chat_id, cwd=cwd, session_id=session_id, project_id=project_id)

    def get_session(self, chat_id: str, project_id: Optional[str] = None) -> Optional[SyncSession]:
        """Get active session for a chat/project (with timeout check).

        Health check is only performed when the session has been idle for a while
        (> 30s) to avoid costly RPC round-trips on every call.  For recently-active
        sessions the send_prompt watchdog already handles crash detection.
        """
        key = self._session_key(chat_id, project_id)
        session = self._sessions.get(key)
        if session:
            now = time.time()
            idle = now - session.last_active
            if idle > self._session_timeout:
                logger.info("[ACP:%s] Session timeout: key=%s",
                             self._agent_type.upper(), key[-16:])
                self.end_session(chat_id, project_id=project_id)
                return None
            # Only do expensive RPC health check after prolonged idle (>30s).
            # Recently active sessions are protected by the send_prompt watchdog.
            if idle > 30.0:
                if not session.is_server_running():
                    logger.warning(
                        "[ACP:%s] Session server dead: key=%s session=%s",
                        self._agent_type.upper(), key[-16:], (session.session_id or "none")[:8],
                    )
                    self.end_session(chat_id, project_id=project_id)
                    return None
        return session

    def _end_session_unlocked(self, key: str) -> Optional[dict]:
        """End a session without acquiring lock (caller must hold _lock)."""
        if key in self._sessions:
            session = self._sessions[key]
            logger.info("[ACP:%s] Session ended: key=%s, session=%s, msgs=%d",
                         self._agent_type.upper(), key[-16:],
                         session.session_id[:8] if session.session_id else "none",
                         session.message_count)
            snapshot = session.to_snapshot()
            try:
                session.close()
            except Exception as e:
                logger.debug("Error closing ACP session: %s", e)
            del self._sessions[key]
            return snapshot
        return None

    def end_session(self, chat_id: str, project_id: Optional[str] = None) -> Optional[dict]:
        """End a session and return its snapshot."""
        key = self._session_key(chat_id, project_id)
        with self._lock:
            return self._end_session_unlocked(key)

    def has_active_session(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.get_session(chat_id, project_id=project_id) is not None

    def get_session_info(self, chat_id: str, project_id: Optional[str] = None) -> Optional[str]:
        """Return human-readable session info."""
        session = self.get_session(chat_id, project_id=project_id)
        if not session:
            return None
        return session.get_session_info()

    def cleanup_all(self) -> None:
        """Close all sessions."""
        for key in list(self._sessions.keys()):
            try:
                with self._lock:
                    self._end_session_unlocked(key)
            except Exception as e:
                logger.debug("Error cleaning up session for %s: %s", key[-16:], e)
