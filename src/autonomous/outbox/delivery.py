"""Employee-owned delivery coordinator for Durable Outbox snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .models import (
    DeliveryEffectState,
    EmployeeOutboxBinding,
    employee_outbox_uuid,
)
from .projection import OutboxRecord
from .service import EmployeeOutboxService


@dataclass(frozen=True, slots=True)
class EmployeeDeliveryAuthority:
    app_id: str
    generation: int
    connection_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id:
            raise ValueError("delivery authority app_id is required")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("delivery authority generation is invalid")
        if not isinstance(self.connection_id, str) or not self.connection_id:
            raise ValueError("delivery authority connection_id is required")


class EmployeeCardChannels(Protocol):
    def send(
        self,
        agent_id: str,
        *,
        generation: int,
        target: str,
        message: Any,
        options: Any = None,
    ) -> Any: ...

    def update_card(
        self,
        agent_id: str,
        *,
        generation: int,
        message_id: str,
        card: dict[str, Any],
    ) -> Any: ...


class EmployeeOutboxDeliveryCoordinator:
    """Anchor each delivery effect before invoking the employee child."""

    def __init__(
        self,
        *,
        outbox: EmployeeOutboxService,
        channels: EmployeeCardChannels,
        authority_resolver: Callable[[OutboxRecord], EmployeeDeliveryAuthority],
    ) -> None:
        if not isinstance(outbox, EmployeeOutboxService):
            raise TypeError("outbox must be EmployeeOutboxService")
        if not callable(authority_resolver):
            raise TypeError("authority_resolver must be callable")
        self._outbox = outbox
        self._channels = channels
        self._authority_resolver = authority_resolver

    def deliver(
        self,
        outbox_id: str,
        snapshot_version: int | None = None,
    ) -> EmployeeOutboxBinding | None:
        record = self._outbox.get_record(outbox_id)
        version = record.latest_version if snapshot_version is None else snapshot_version
        if record.binding is not None and record.binding.bound_snapshot_version >= version:
            return record.binding
        effect = self._outbox.prepare_delivery(outbox_id, version)
        if effect.state is DeliveryEffectState.COMMITTED:
            return self._outbox.get_record(outbox_id).binding
        if effect.state is DeliveryEffectState.PREPARED:
            effect = self._outbox.mark_effect_executing(effect.effect_id)
        if effect.state is not DeliveryEffectState.EXECUTING:
            raise RuntimeError("Outbox delivery effect is not executable")
        version = effect.snapshot_version
        snapshot = self._outbox.get_snapshot(outbox_id, version)

        record = self._outbox.get_record(outbox_id)
        authority = self._authority_resolver(record)
        if not isinstance(authority, EmployeeDeliveryAuthority):
            raise RuntimeError("employee delivery authority is unavailable")
        if record.binding is None:
            options: dict[str, Any] = {"uuid": employee_outbox_uuid(outbox_id)}
            if snapshot.thread_root_message_id:
                options.update(
                    {
                        "reply_to": snapshot.thread_root_message_id,
                        "reply_in_thread": True,
                    }
                )
            receipt = self._channels.send(
                record.agent_id,
                generation=authority.generation,
                target=snapshot.chat_id,
                message={"card": snapshot.to_dict()["card_json"]},
                options=options,
            )
        else:
            receipt = self._channels.update_card(
                record.agent_id,
                generation=authority.generation,
                message_id=record.binding.message_id,
                card=snapshot.to_dict()["card_json"],
            )
        self._validate_receipt(receipt, authority, record.binding)
        return self._outbox.commit_delivery(
            effect.effect_id,
            app_id=receipt.app_id,
            generation=receipt.generation,
            connection_id=receipt.connection_id,
            message_id=receipt.message_id,
        )

    @staticmethod
    def _validate_receipt(
        receipt: Any,
        authority: EmployeeDeliveryAuthority,
        current_binding: EmployeeOutboxBinding | None,
    ) -> None:
        valid = (
            getattr(receipt, "success", None) is True
            and getattr(receipt, "app_id", None) == authority.app_id
            and getattr(receipt, "generation", None) == authority.generation
            and getattr(receipt, "connection_id", None) == authority.connection_id
            and isinstance(getattr(receipt, "message_id", None), str)
            and bool(receipt.message_id)
            and (current_binding is None or receipt.message_id == current_binding.message_id)
        )
        if not valid:
            raise RuntimeError("employee delivery receipt does not match authority")


__all__ = [
    "EmployeeDeliveryAuthority",
    "EmployeeOutboxDeliveryCoordinator",
]
