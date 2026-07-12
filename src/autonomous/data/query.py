"""ACL-gated history range query and authenticated request context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .projection import DataProjectionState, HistoryMetadataRecord


class QueryDeniedError(RuntimeError):
    """The caller lacks authority for this data query."""


class AuditFailedError(RuntimeError):
    """Audit emission failed; query is fail-closed."""


@dataclass(frozen=True)
class AuthenticatedDataRequest:
    """Transport-authenticated request context (never from callback payload)."""

    principal_id: str
    tenant_key: str
    receiving_bot_app_id: str
    chat_id: str
    chat_type: str
    thread_root_id: str
    requested_agent_id: str


@dataclass(frozen=True)
class ResolvedQueryContext:
    """Trusted derived context for data queries."""

    principal_id: str
    tenant_key: str
    requested_agent_id: str
    is_admin: bool
    is_main_bot_dm: bool
    is_owner: bool
    is_same_chat_member: bool


class QueryAuditPort(Protocol):
    def emit(
        self,
        *,
        principal_id: str,
        operation: str,
        resource_id: str,
        outcome: str,
        reason: str,
    ) -> bool: ...


class EmployeeDataRequestContextFactory:
    """Derives trusted query context from transport identity and projection."""

    def __init__(
        self,
        *,
        admin_principal_ids: frozenset[str],
        main_bot_app_id: str,
    ) -> None:
        self._admins = admin_principal_ids
        self._main_bot_app_id = main_bot_app_id

    def resolve(
        self,
        request: AuthenticatedDataRequest,
        state: DataProjectionState,
    ) -> ResolvedQueryContext:
        if not request.principal_id or not request.tenant_key:
            raise QueryDeniedError("missing transport identity")
        employee_meta = None
        for record in state.history_records.values():
            if record.agent_id == request.requested_agent_id:
                employee_meta = record
                break
        if employee_meta and employee_meta.tenant_key != request.tenant_key:
            raise QueryDeniedError("cross-tenant query denied")
        is_admin = request.principal_id in self._admins
        is_main_bot_dm = (
            request.receiving_bot_app_id == self._main_bot_app_id
            and request.chat_type == "p2p"
        )
        is_owner = False
        if employee_meta:
            is_owner = request.principal_id == employee_meta.owner_principal_id
        return ResolvedQueryContext(
            principal_id=request.principal_id,
            tenant_key=request.tenant_key,
            requested_agent_id=request.requested_agent_id,
            is_admin=is_admin,
            is_main_bot_dm=is_main_bot_dm,
            is_owner=is_owner,
            is_same_chat_member=request.chat_id != "",
        )


@dataclass(frozen=True)
class HistoryQuerySpec:
    """Parameters for a history range query."""

    start_day: str
    end_day: str
    page_size: int = 50
    cursor: tuple[str, int, str] | None = None


@dataclass(frozen=True)
class HistoryQueryResult:
    """Paginated query result."""

    records: tuple[HistoryMetadataRecord, ...]
    next_cursor: tuple[str, int, str] | None
    total_available: int


class HistoryRangeQuery:
    """ACL-gated paginated history query over ProjectionState indexes."""

    def __init__(
        self,
        *,
        state: DataProjectionState,
        context_factory: EmployeeDataRequestContextFactory,
        max_range_days: int = 31,
        page_size: int = 50,
        audit_port: QueryAuditPort | None = None,
    ) -> None:
        self._state = state
        self._factory = context_factory
        self._max_range = max_range_days
        self._page_size = page_size
        self._audit = audit_port

    def query(
        self,
        request: AuthenticatedDataRequest,
        spec: HistoryQuerySpec,
    ) -> HistoryQueryResult:
        context = self._factory.resolve(request, self._state)
        if not context.is_admin and not context.is_owner and not context.is_same_chat_member:
            self._emit_audit(context, "history_query", request.requested_agent_id, "denied", "no_authority")
            raise QueryDeniedError("insufficient authority for history query")
        from datetime import date
        try:
            start = date.fromisoformat(spec.start_day)
            end = date.fromisoformat(spec.end_day)
        except ValueError as exc:
            raise QueryDeniedError("invalid date range") from exc
        if end < start:
            raise QueryDeniedError("end before start")
        if (end - start).days >= self._max_range:
            raise QueryDeniedError("range exceeds maximum")
        all_records: list[HistoryMetadataRecord] = []
        current = start
        from datetime import timedelta
        while current <= end:
            day_str = current.isoformat()
            day_key = (context.tenant_key, context.requested_agent_id, day_str)
            record_ids = self._state.history_by_employee_day.get(day_key, [])
            for record_id in record_ids:
                meta = self._state.history_records.get(record_id)
                if meta is None or meta.tombstoned:
                    continue
                if not self._row_visible(meta, context):
                    continue
                all_records.append(meta)
            current += timedelta(days=1)
        all_records.sort(key=lambda r: (r.shard_day, r.publish_sequence, r.record_id))
        start_idx = 0
        if spec.cursor is not None:
            cursor_day, cursor_seq, cursor_id = spec.cursor
            for i, r in enumerate(all_records):
                if (r.shard_day, r.publish_sequence, r.record_id) > (cursor_day, cursor_seq, cursor_id):
                    start_idx = i
                    break
            else:
                start_idx = len(all_records)
        page = spec.page_size or self._page_size
        page_records = all_records[start_idx : start_idx + page]
        next_cursor = None
        if start_idx + page < len(all_records):
            last = page_records[-1]
            next_cursor = (last.shard_day, last.publish_sequence, last.record_id)
        self._emit_audit(
            context, "history_query", request.requested_agent_id,
            "granted", f"rows={len(page_records)}"
        )
        return HistoryQueryResult(
            records=tuple(page_records),
            next_cursor=next_cursor,
            total_available=len(all_records),
        )

    def _row_visible(self, meta: HistoryMetadataRecord, context: ResolvedQueryContext) -> bool:
        if context.is_admin and context.is_main_bot_dm:
            return True
        if context.is_owner:
            return True
        if context.is_same_chat_member and meta.chat_id == context.principal_id:
            return True
        if meta.requester_principal_id == context.principal_id:
            return True
        return False

    def _emit_audit(
        self,
        context: ResolvedQueryContext,
        operation: str,
        resource_id: str,
        outcome: str,
        reason: str,
    ) -> None:
        if self._audit is None:
            return
        self._audit.emit(
            principal_id=context.principal_id,
            operation=operation,
            resource_id=resource_id,
            outcome=outcome,
            reason=reason,
        )
