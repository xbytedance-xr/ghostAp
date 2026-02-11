"""ACP Session Manager — manages per-chat, per-project ACP sessions.

Sessions are keyed by (chat_id, project_id) to ensure full isolation between
projects within the same chat.  When project_id is not provided, a default
suffix is used for backward compatibility.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .sync_adapter import SyncACPSession
from ..agent_session import SyncClaudeCLISession, SyncSession
from ..config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_PROJECT = "_default_"


class ACPSessionManager:
    """Manages per-chat, per-project sessions for a specific agent type.

    - Coco: ACP backend (SyncACPSession)
    - Claude: CLI backend (SyncClaudeCLISession)
    """

    def __init__(self, agent_type: str, session_timeout: int = 86400):
        self._agent_type = agent_type  # "coco" / "claude"
        self._sessions: dict[str, SyncSession] = {}  # key = _session_key(...)
        self._session_timeout = session_timeout
        self._lock = threading.Lock()

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
        if self._agent_type.lower() == "claude":
            # CLI backend doesn't need handshake retries.
            retries = 1

        last_err: Exception | None = None
        session: SyncSession | None = None
        actual_id = ""
        last_spec = ""

        # Retry spawning agent process + handshake, since ACP CLI may be temporarily unavailable.
        for attempt in range(1, retries + 1):
            try:
                if self._agent_type.lower() == "claude":
                    session = SyncClaudeCLISession(cwd=cwd or ".")
                else:
                    session = SyncACPSession(agent_type=self._agent_type, cwd=cwd or ".")
                try:
                    last_spec = session.describe_agent()
                except Exception:
                    last_spec = ""

                # Progressive timeout: allow more time on later attempts.
                effective_timeout = float(startup_timeout) * (1.0 + 0.5 * (attempt - 1))
                actual_id = session.start(startup_timeout=effective_timeout)
                logger.info(
                    "[ACP:%s] Session started: key=%s, session=%s (attempt=%d/%d)",
                    self._agent_type.upper(), key[-16:], actual_id[:8], attempt, retries,
                )
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    "[ACP:%s] Session start failed (attempt=%d/%d): %s",
                    self._agent_type.upper(), attempt, retries, e,
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
            kind = "会话" if self._agent_type.lower() == "claude" else "ACP Server"
            raise RuntimeError(f"启动 {self._agent_type} {kind} 失败{spec}（已重试 {retries} 次）: {detail}")

        # If caller wants a specific session_id (resume), load it
        if session_id:
            try:
                session.load_session(session_id)
                session.session_id = session_id
                session.is_resumed = True
            except Exception as e:
                logger.warning("[ACP:%s] Failed to load session %s, using new: %s",
                               self._agent_type.upper(), session_id[:8], e)

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

        return self.start_session(chat_id, cwd=cwd, session_id=session_id, startup_timeout=startup_timeout, project_id=project_id)

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
