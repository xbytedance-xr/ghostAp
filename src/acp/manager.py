"""ACP Session Manager — manages per-chat ACP sessions.

Replaces the old BaseSessionManager with ACP-native session lifecycle
management. Supports both Coco and Claude agent types.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .sync_adapter import SyncACPSession
from ..config import get_settings

logger = logging.getLogger(__name__)


class ACPSessionManager:
    """Manages per-chat ACP sessions for a specific agent type."""

    def __init__(self, agent_type: str, session_timeout: int = 86400):
        self._agent_type = agent_type  # "coco" / "claude"
        self._sessions: dict[str, SyncACPSession] = {}
        self._session_timeout = session_timeout

    def start_session(
        self,
        chat_id: str,
        cwd: str = "",
        session_id: Optional[str] = None,
        startup_timeout: float = 60,
    ) -> SyncACPSession:
        """Start a new ACP session for a chat.

        startup_timeout controls how long we wait for the ACP agent process
        (`<agent> acp serve`) to spawn + complete protocol handshake.
        """
        # Close existing session if any
        if chat_id in self._sessions:
            self.end_session(chat_id)

        settings = get_settings()
        retries = int(getattr(settings, "acp_startup_retries", 2) or 2)
        retries = max(1, retries)

        last_err: Exception | None = None
        session: SyncACPSession | None = None
        actual_id = ""
        last_spec = ""

        # Retry spawning agent process + handshake, since ACP CLI may be temporarily unavailable.
        for attempt in range(1, retries + 1):
            try:
                session = SyncACPSession(agent_type=self._agent_type, cwd=cwd or ".")
                try:
                    last_spec = session.describe_agent()
                except Exception:
                    last_spec = ""

                # Progressive timeout: allow more time on later attempts.
                effective_timeout = float(startup_timeout) * (1.0 + 0.5 * (attempt - 1))
                actual_id = session.start(startup_timeout=effective_timeout)
                logger.info(
                    "[ACP:%s] Session started: chat=%s, session=%s (attempt=%d/%d)",
                    self._agent_type.upper(), chat_id[-8:], actual_id[:8], attempt, retries,
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
            raise RuntimeError(
                f"启动 {self._agent_type} ACP Server 失败{spec}（已重试 {retries} 次）: {detail}"
            )

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

        self._sessions[chat_id] = session
        return session

    def ensure_session(
        self,
        chat_id: str,
        cwd: str = "",
        session_id: Optional[str] = None,
        startup_timeout: float = 60,
    ) -> SyncACPSession:
        """Ensure an ACP session exists and its underlying server is running.

        1) Detect whether current ACP server process is alive.
        2) If not alive / missing / timed out, auto-start a new ACP session.
        3) Optionally load a given session_id (resume) after startup.
        """
        existing = self._sessions.get(chat_id)
        if existing:
            # Timeout check (reuse get_session semantics)
            if time.time() - existing.last_active > self._session_timeout:
                logger.info("[ACP:%s] Session timeout before ensure: chat=%s",
                            self._agent_type.upper(), chat_id[-8:])
                self.end_session(chat_id)
                existing = None

        if existing:
            # Health check (process + lightweight RPC)
            health_to = float(getattr(get_settings(), "acp_healthcheck_timeout", 2.0) or 2.0)
            if not existing.is_server_healthy(healthcheck_timeout=health_to):
                logger.warning(
                    "[ACP:%s] Detected unhealthy ACP server, restarting: chat=%s session=%s",
                    self._agent_type.upper(), chat_id[-8:], (existing.session_id or "none")[:8],
                )
                self.end_session(chat_id)
                existing = None

        if existing and session_id and existing.session_id != session_id:
            # Different target session requested; restart to load requested session.
            self.end_session(chat_id)
            existing = None

        if existing:
            return existing

        return self.start_session(chat_id, cwd=cwd, session_id=session_id, startup_timeout=startup_timeout)

    def resume_session(self, chat_id: str, session_id: str, cwd: str = "") -> SyncACPSession:
        """Resume an existing session by session_id."""
        return self.start_session(chat_id, cwd=cwd, session_id=session_id)

    def get_session(self, chat_id: str) -> Optional[SyncACPSession]:
        """Get active session for a chat (with timeout check)."""
        session = self._sessions.get(chat_id)
        if session:
            if time.time() - session.last_active > self._session_timeout:
                logger.info("[ACP:%s] Session timeout: chat=%s",
                             self._agent_type.upper(), chat_id[-8:])
                self.end_session(chat_id)
                return None
            # Detect whether the underlying ACP server is unhealthy.
            health_to = float(getattr(get_settings(), "acp_healthcheck_timeout", 2.0) or 2.0)
            if not session.is_server_healthy(healthcheck_timeout=health_to):
                logger.warning(
                    "[ACP:%s] Session server unhealthy: chat=%s session=%s",
                    self._agent_type.upper(), chat_id[-8:], (session.session_id or "none")[:8],
                )
                self.end_session(chat_id)
                return None
        return session

    def end_session(self, chat_id: str) -> Optional[dict]:
        """End a session and return its snapshot."""
        if chat_id in self._sessions:
            session = self._sessions[chat_id]
            logger.info("[ACP:%s] Session ended: chat=%s, session=%s, msgs=%d",
                         self._agent_type.upper(), chat_id[-8:],
                         session.session_id[:8] if session.session_id else "none",
                         session.message_count)
            snapshot = session.to_snapshot()
            try:
                session.close()
            except Exception as e:
                logger.debug("Error closing ACP session: %s", e)
            del self._sessions[chat_id]
            return snapshot
        return None

    def has_active_session(self, chat_id: str) -> bool:
        return self.get_session(chat_id) is not None

    def get_session_info(self, chat_id: str) -> Optional[str]:
        """Return human-readable session info."""
        session = self.get_session(chat_id)
        if not session:
            return None
        return session.get_session_info()

    def cleanup_all(self) -> None:
        """Close all sessions."""
        for chat_id in list(self._sessions.keys()):
            try:
                self.end_session(chat_id)
            except Exception as e:
                logger.debug("Error cleaning up session for %s: %s", chat_id[-8:], e)
