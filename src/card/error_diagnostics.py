"""Card-bound diagnostic context for safe error detail disclosure."""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass


EXPIRED_DIAGNOSTIC_MESSAGE = "⚠️ 诊断详情已过期或不存在，请重新触发操作获取最新摘要。"
UNAUTHORIZED_DIAGNOSTIC_MESSAGE = "⚠️ 无法查看该诊断详情：当前会话与原始错误卡不匹配，请重新触发操作获取最新摘要。"


@dataclass(frozen=True)
class DiagnosticBinding:
    chat_id: str | None = None
    origin_message_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None

    def matches(
        self,
        *,
        chat_id: str | None = None,
        origin_message_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> bool:
        checks = (
            (self.chat_id, chat_id),
            (self.origin_message_id, origin_message_id),
            (self.request_id, request_id),
            (self.trace_id, trace_id),
        )
        bound = [(expected, actual) for expected, actual in checks if expected]
        if not bound:
            return False
        return all(str(actual or "") == str(expected) for expected, actual in bound)


@dataclass(frozen=True)
class ErrorDiagnosticRecord:
    title: str
    summary: str
    details: str
    created_at: float
    expires_at: float
    binding: DiagnosticBinding


class ErrorDiagnosticStore:
    """In-memory TTL store for card diagnostics bound to card action context."""

    _PATH_RE = re.compile(r"(?<![\w])(?:/[\w.\-]+){2,}")
    _SECRET_ASSIGNMENT_RE = re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL)[A-Z0-9_]*)\s*=\s*[^\s\n]+"
    )
    _BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")

    def __init__(self, *, ttl_seconds: int = 900, max_details_chars: int = 1200, max_records: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_details_chars = max_details_chars
        self.max_records = max_records
        self._records: dict[str, ErrorDiagnosticRecord] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def register(
        self,
        *,
        title: str,
        summary: str,
        details: str,
        chat_id: str | None = None,
        origin_message_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        now = time.time()
        token = secrets.token_urlsafe(18)
        record = ErrorDiagnosticRecord(
            title=str(title or "错误详情"),
            summary=str(summary or "暂无错误摘要"),
            details=str(details or "本次错误暂无更多诊断上下文。"),
            created_at=now,
            expires_at=now + self.ttl_seconds,
            binding=DiagnosticBinding(
                chat_id=str(chat_id) if chat_id else None,
                origin_message_id=str(origin_message_id) if origin_message_id else None,
                request_id=str(request_id) if request_id else None,
                trace_id=str(trace_id) if trace_id else None,
            ),
        )
        with self._lock:
            self._purge_locked(now)
            if len(self._records) >= self.max_records:
                oldest = min(self._records, key=lambda key: self._records[key].created_at)
                self._records.pop(oldest, None)
            self._records[token] = record
        return token

    def render(
        self,
        token: str | None,
        *,
        chat_id: str | None = None,
        origin_message_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        record = self.get(token)
        if record is None:
            return EXPIRED_DIAGNOSTIC_MESSAGE
        if not record.binding.matches(
            chat_id=chat_id,
            origin_message_id=origin_message_id,
            request_id=request_id,
            trace_id=trace_id,
        ):
            return UNAUTHORIZED_DIAGNOSTIC_MESSAGE
        summary = self.sanitize(record.summary, limit=600)
        details = self.sanitize(record.details, limit=self.max_details_chars)
        return f"🔎 {record.title}\n\n**摘要**\n{summary}\n\n**详情（已脱敏）**\n{details}"

    def get(self, token: str | None) -> ErrorDiagnosticRecord | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            return self._records.get(str(token))

    def sanitize(self, text: object, *, limit: int | None = None) -> str:
        value = str(text or "")
        value = self._SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=<redacted>", value)
        value = self._BEARER_RE.sub("Bearer <redacted>", value)
        value = self._PATH_RE.sub("<path>", value)
        max_chars = self.max_details_chars if limit is None else limit
        if len(value) > max_chars:
            value = value[:max_chars].rstrip() + "…\n（诊断详情已截断）"
        return value or "本次错误暂无更多诊断上下文。"

    def _purge_locked(self, now: float) -> None:
        expired = [token for token, record in self._records.items() if record.expires_at <= now]
        for token in expired:
            self._records.pop(token, None)


error_diagnostic_store = ErrorDiagnosticStore()


def register_error_diagnostic(
    *,
    title: str,
    summary: str,
    details: str,
    chat_id: str | None = None,
    origin_message_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    return error_diagnostic_store.register(
        title=title,
        summary=summary,
        details=details,
        chat_id=chat_id,
        origin_message_id=origin_message_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def render_error_diagnostic(
    token: str | None,
    *,
    chat_id: str | None = None,
    origin_message_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    return error_diagnostic_store.render(
        token,
        chat_id=chat_id,
        origin_message_id=origin_message_id,
        request_id=request_id,
        trace_id=trace_id,
    )
