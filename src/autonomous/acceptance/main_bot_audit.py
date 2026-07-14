"""Tamper-evident audit of main-Bot outbound message attempts."""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..journal.anchor import AnchorProvider, FileAnchor
from ..journal.frame import JournalEvent
from ..journal.writer import CommitState, JournalWriter

_AGGREGATE_ID = "main-bot-send-audit"
_EVENT_TYPE = "main_bot.send_attempted"
_OPERATIONS = frozenset({"create", "reply", "patch"})


class MainBotSendAuditLog:
    """Record every logical outbound mutation before network dispatch."""

    def __init__(self, writer: JournalWriter, *, external_audit: Any = None) -> None:
        if not isinstance(writer, JournalWriter):
            raise TypeError("writer must be JournalWriter")
        self.writer = writer
        self.external_audit = external_audit
        self._lock = threading.Lock()
        self._complete = True

    @classmethod
    def open(
        cls,
        directory: str | Path,
        *,
        anchor_path: str | Path,
        hmac_key: bytes,
        writer_epoch: int | None = None,
        anchor: AnchorProvider | None = None,
        external_audit: Any = None,
    ) -> MainBotSendAuditLog:
        return cls(
            JournalWriter.open(
                Path(directory).expanduser(),
                anchor=anchor if anchor is not None else FileAnchor(anchor_path),
                hmac_key=hmac_key,
                writer_epoch=writer_epoch if writer_epoch is not None else time.time_ns(),
            ),
            external_audit=external_audit,
        )

    def record_attempt(
        self,
        tenant_key: str,
        operation: str,
        target: str,
        *,
        attempted_at: float | None = None,
    ) -> None:
        if not isinstance(tenant_key, str) or len(tenant_key) > 512:
            raise ValueError("invalid tenant key")
        if operation not in _OPERATIONS:
            raise ValueError("invalid main Bot operation")
        if not isinstance(target, str) or not target or len(target) > 2048:
            raise ValueError("invalid main Bot target")
        timestamp = time.time() if attempted_at is None else attempted_at
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or timestamp <= 0
        ):
            raise ValueError("invalid main Bot audit timestamp")
        attempt_id = uuid.uuid4().hex
        tenant_hash = (
            hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
            if tenant_key
            else ""
        )
        target_hash = hashlib.sha256(target.encode("utf-8")).hexdigest()
        event = JournalEvent(
            event_type=_EVENT_TYPE,
            aggregate_id=_AGGREGATE_ID,
            payload={
                "attempt_id": attempt_id,
                "tenant_hash": tenant_hash,
                "operation": operation,
                "target_hash": target_hash,
                "attempted_at": float(timestamp),
            },
            timestamp=float(timestamp),
        )
        with self._lock:
            try:
                if self.external_audit is not None:
                    self.external_audit.record_main_bot_send_attempt(
                        attempt_id=attempt_id,
                        tenant_hash=tenant_hash,
                        operation=operation,
                        target_hash=target_hash,
                        attempted_at=float(timestamp),
                    )
                expected = self.writer.get_aggregate_versions((_AGGREGATE_ID,))
                result = self.writer.commit((event,), expected)
                if result.state is not CommitState.ANCHORED:
                    raise RuntimeError("main Bot send audit was not anchored")
            except BaseException:
                self._complete = False
                raise

    def mark_incomplete(self, _error: Exception | None = None) -> None:
        with self._lock:
            self._complete = False

    def count_attempts(self, tenant_key: str, start: float, end: float) -> int:
        if not isinstance(tenant_key, str) or not tenant_key:
            raise ValueError("tenant key is required")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (start, end)
        ) or start > end:
            raise ValueError("invalid main Bot audit window")
        with self._lock:
            if not self._complete:
                raise RuntimeError("main Bot send audit is incomplete")
            local_count = 0
            tenant_hash = hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
            for frame in self.writer.replay():
                for event in frame.events:
                    if event.event_type != _EVENT_TYPE:
                        continue
                    payload = event.payload
                    attempted_at = payload.get("attempted_at")
                    event_tenant = payload.get("tenant_hash")
                    if (
                        isinstance(attempted_at, (int, float))
                        and not isinstance(attempted_at, bool)
                        and float(start) <= float(attempted_at) <= float(end)
                        and event_tenant in {"", tenant_hash}
                    ):
                        local_count += 1
            if self.external_audit is None:
                return local_count
            external_count = self.external_audit.count_main_bot_send_attempts(
                tenant_key,
                float(start),
                float(end),
            )
            if (
                isinstance(external_count, bool)
                or not isinstance(external_count, int)
                or external_count < local_count
            ):
                self._complete = False
                raise RuntimeError("external main Bot audit is behind local audit")
            return external_count

    def close(self) -> None:
        self.writer.close()


__all__ = ["MainBotSendAuditLog"]
