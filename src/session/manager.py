import logging
import time
from typing import Optional, TypeVar, Generic, Type

from .base import BaseSession

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseSession)


class BaseSessionManager(Generic[T]):
    def __init__(self, session_cls: Type[T], session_timeout: int = 86400):
        self._session_cls = session_cls
        self._sessions: dict[str, T] = {}
        self._session_timeout = session_timeout

    def start_session(self, chat_id: str, session_id: Optional[str] = None) -> T:
        if session_id:
            session = self._session_cls(chat_id=chat_id, session_id=session_id)
        else:
            session = self._session_cls(chat_id=chat_id)
        self._sessions[chat_id] = session
        cli_name = session._get_cli_name().upper()
        logger.info("[%s] 会话启动: chat=%s, session=%s", cli_name, chat_id[-8:], session.session_id[:8])
        return session

    def resume_session(self, chat_id: str, session_id: str) -> T:
        session = self._session_cls(
            chat_id=chat_id,
            session_id=session_id,
            is_resumed=True,
        )
        self._sessions[chat_id] = session
        cli_name = session._get_cli_name().upper()
        logger.info("[%s] 会话恢复: chat=%s, session=%s", cli_name, chat_id[-8:], session_id[:8])
        return session

    def get_session(self, chat_id: str) -> Optional[T]:
        session = self._sessions.get(chat_id)
        if session:
            if time.time() - session.last_active > self._session_timeout:
                cli_name = session._get_cli_name().upper()
                logger.info("[%s] 会话超时: chat=%s, session=%s", cli_name, chat_id[-8:], session.session_id[:8])
                self.end_session(chat_id)
                return None
        return session

    def end_session(self, chat_id: str) -> Optional[dict]:
        if chat_id in self._sessions:
            session = self._sessions[chat_id]
            cli_name = session._get_cli_name().upper()
            logger.info("[%s] 会话结束: chat=%s, session=%s, 消息数=%d",
                       cli_name, chat_id[-8:], session.session_id[:8], session.message_count)
            snapshot = session.to_snapshot()
            del self._sessions[chat_id]
            return snapshot
        return None

    def has_active_session(self, chat_id: str) -> bool:
        return self.get_session(chat_id) is not None

    def get_session_info(self, chat_id: str) -> Optional[str]:
        session = self.get_session(chat_id)
        if not session:
            return None

        cli_name = session._get_cli_name().capitalize()
        duration = int(time.time() - session.created_at)
        minutes = duration // 60
        seconds = duration % 60

        resumed_info = " (已恢复)" if session.is_resumed else ""

        return (
            f"📊 {cli_name} 会话信息{resumed_info}:\n"
            f"- 会话ID: {session.session_id}\n"
            f"- 消息数: {session.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )
