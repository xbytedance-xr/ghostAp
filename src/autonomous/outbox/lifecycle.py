"""Gateway lifecycle adapter that appends employee status snapshots."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..gateway.models import DispatchBinding, GatewayExecutionResult, GatewayExecutionStatus
from .cards import build_employee_status_card
from .models import EmployeeCardState, EmployeeOutboxSnapshot, employee_outbox_id
from .service import EmployeeOutboxService


class EmployeeOutboxLifecycle:
    """Map durable execution facts to one monotonic employee card."""

    def __init__(self, outbox: EmployeeOutboxService) -> None:
        if not isinstance(outbox, EmployeeOutboxService):
            raise TypeError("outbox must be EmployeeOutboxService")
        self._outbox = outbox

    def queued(self, binding: DispatchBinding) -> EmployeeOutboxSnapshot:
        outbox_id = employee_outbox_id(
            binding.tenant_key,
            binding.agent_id,
            binding.attempt_id,
        )
        try:
            return self._outbox.get_snapshot(outbox_id)
        except KeyError:
            pass
        return self._append(
            binding,
            version=1,
            state=EmployeeCardState.QUEUED,
            summary="任务已进入员工执行队列。",
            progress=0,
            created_at=_canonical_utc(binding.dispatch_committed_at),
        )

    def running(self, binding: DispatchBinding) -> EmployeeOutboxSnapshot:
        current = self.queued(binding)
        if current.state is EmployeeCardState.RUNNING or current.state.terminal:
            return current
        return self._append(
            binding,
            version=current.version + 1,
            state=EmployeeCardState.RUNNING,
            summary="员工正在执行任务。",
            progress=max(current.progress_percent, 10),
            created_at=current.created_at,
        )

    def terminal(
        self,
        binding: DispatchBinding,
        result: GatewayExecutionResult,
    ) -> EmployeeOutboxSnapshot:
        current = self.queued(binding)
        state, summary, progress = _terminal_view(result)
        if current.state.terminal:
            if current.state is not state:
                raise RuntimeError("employee terminal card conflicts with execution fact")
            return current
        if (
            current.state is EmployeeCardState.QUEUED
            and result.status is not GatewayExecutionStatus.ACTION_REQUIRED
        ):
            current = self.running(binding)
        return self._append(
            binding,
            version=current.version + 1,
            state=state,
            summary=summary,
            progress=max(current.progress_percent, progress),
            created_at=current.created_at,
        )

    def _append(
        self,
        binding: DispatchBinding,
        *,
        version: int,
        state: EmployeeCardState,
        summary: str,
        progress: int,
        created_at: str,
    ) -> EmployeeOutboxSnapshot:
        title = f"员工任务 · {binding.task_id}"
        snapshot = EmployeeOutboxSnapshot(
            schema_version=1,
            outbox_id=employee_outbox_id(
                binding.tenant_key,
                binding.agent_id,
                binding.attempt_id,
            ),
            tenant_key=binding.tenant_key,
            agent_id=binding.agent_id,
            attempt_id=binding.attempt_id,
            chat_id=binding.chat_id,
            thread_root_message_id=binding.thread_root_id,
            version=version,
            state=state,
            title=title,
            summary=_safe_single_line(summary),
            progress_percent=progress,
            card_json=build_employee_status_card(
                title=title,
                state=state,
                summary=summary,
                progress_percent=progress,
                attempt_id=binding.attempt_id,
            ),
            created_at=created_at,
            terminal_version=version if state.terminal else 0,
        )
        self._outbox.append_snapshot(snapshot)
        return snapshot


def _terminal_view(
    result: GatewayExecutionResult,
) -> tuple[EmployeeCardState, str, int]:
    if result.status is GatewayExecutionStatus.COMPLETED:
        return EmployeeCardState.COMPLETED, result.output, 100
    if result.status is GatewayExecutionStatus.CANCELED:
        return EmployeeCardState.CANCELED, "任务已取消。", 100
    if result.status is GatewayExecutionStatus.ACTION_REQUIRED:
        return (
            EmployeeCardState.ACTION_REQUIRED,
            (f"任务需要人工处理：{result.safe_error_code or 'action_required'}"),
            100,
        )
    if result.status is GatewayExecutionStatus.TIMEOUT:
        return EmployeeCardState.FAILED, "任务执行超时。", 100
    return EmployeeCardState.FAILED, (f"任务执行失败：{result.safe_error_code or 'execution_failed'}"), 100


def _safe_single_line(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:100_000]


def _canonical_utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("dispatch timestamp is not UTC")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["EmployeeOutboxLifecycle"]
