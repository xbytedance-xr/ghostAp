from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autonomous.context.group_ledger import (
    GroupContextLedger,
    GroupEventPayload,
    GroupLedgerError,
)
from src.autonomous.context.models import (
    AuthorizedContextRequest,
    ContextQuality,
    ContextUnavailableError,
    ContextUnavailableReason,
)
from src.autonomous.journal.anchor import FileAnchor
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.writer import JournalWriter


def _ledger(tmp_path: Path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=b"group-context-ledger-test-key-32bytes",
    )
    store = BlobStore(
        tmp_path / "blobs",
        AesGcmEncryptionProvider(lambda _ref: b"k" * 32),
    )
    return writer, store, GroupContextLedger(
        writer=writer,
        blob_store=store,
        active_key_id="data-key",
    )


def _payload(text: str = "hello", *, timestamp: float = 1.0) -> GroupEventPayload:
    return GroupEventPayload(
        sender_id="ou_user",
        sender_id_type="open_id",
        sender_type="user",
        sender_tenant_key="tenant_1",
        text=text,
        timestamp=timestamp,
    )


def test_main_and_employee_bot_observations_deduplicate_without_rebinding_authority(
    tmp_path: Path,
) -> None:
    writer, store, ledger = _ledger(tmp_path)
    first = ledger.publish(
        tenant_key="tenant_1",
        chat_id="oc_team",
        thread_id="",
        message_id="om_current",
        transport_principal_id="main_bot",
        transport_event_id="evt_main",
        payload=_payload(timestamp=2.0),
    )
    duplicate = ledger.publish(
        tenant_key="tenant_1",
        chat_id="oc_team",
        thread_id="",
        message_id="om_current",
        transport_principal_id="bot_employee",
        transport_event_id="evt_employee",
        payload=_payload(),
    )

    assert duplicate == first
    assert duplicate.transport_principal_id == "main_bot"
    assert len(ledger.window(
        tenant_key="tenant_1",
        chat_id="oc_team",
        current_message_id="om_current",
    ).records) == 1
    assert "hello" not in writer.journal_path.read_text(encoding="utf-8")
    with pytest.raises(GroupLedgerError, match="idempotency conflict"):
        ledger.publish(
            tenant_key="tenant_1",
            chat_id="oc_team",
            thread_id="",
            message_id="om_current",
            transport_principal_id="bot_attacker",
            transport_event_id="evt_forged",
            payload=_payload("forged"),
        )
    store.close()
    writer.close()


def test_main_and_two_employee_apps_share_one_canonical_owner_event(
    tmp_path: Path,
) -> None:
    """App-scoped Open IDs for one union identity must not conflict."""
    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime
    from src.autonomous.provisioning.hire_state import HirePhase

    writer, store, ledger = _ledger(tmp_path)
    ledger.publish(
        tenant_key="tenant_1",
        chat_id="oc_team",
        thread_id="",
        message_id="om_shared",
        transport_principal_id="main_bot",
        transport_event_id="evt_main",
        payload=GroupEventPayload(
            sender_id="ou_main_owner",
            sender_id_type="open_id",
            sender_type="user",
            sender_tenant_key="tenant_1",
            text="ordinary group task",
            timestamp=1.0,
        ),
    )
    records = {}
    payloads = {}
    states = []
    for index in (1, 2):
        acceptance_id = f"acc_employee_{index}"
        agent_id = f"agt_employee_{index}"
        records[acceptance_id] = SimpleNamespace(
            metadata=SimpleNamespace(
                tenant_key="tenant_1",
                agent_id=agent_id,
                bot_principal_id=f"bot_employee_{index}",
                event_id=f"evt_employee_{index}",
                chat_id="oc_team",
                message_id="om_shared",
                thread_root_message_id="",
                sender_principal_id=f"ou_employee_app_{index}",
            )
        )
        payloads[acceptance_id] = SimpleNamespace(
            normalized_parts=(
                {
                    "type": "message",
                    "message_type": "text",
                    "chat_type": "group",
                    "content": {"text": "ordinary group task"},
                    "sender_id": f"ou_employee_app_{index}",
                    "sender_union_id": "on_owner",
                    "sender_id_type": "open_id",
                    "sender_type": "user",
                    "sender_tenant_key": "tenant_1",
                    "feishu_thread_id": "",
                },
            )
        )
        states.append(
            SimpleNamespace(
                tenant_key="tenant_1",
                agent_id=agent_id,
                phase=HirePhase.ACTIVE,
                requester_principal_id="ou_main_owner",
                requester_union_id="on_owner",
            )
        )

    runtime = EmployeeDepartmentRuntime()
    runtime._ingress = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        state=SimpleNamespace(by_acceptance_id=records),
        get_payload=payloads.__getitem__,
    )
    runtime._service = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        synchronize_projection=lambda: None,
        list_states=lambda: tuple(states),
    )
    runtime._group_ledger = ledger  # noqa: SLF001

    assert runtime._record_employee_ingress_group_event("acc_employee_1") is True  # noqa: SLF001
    assert runtime._record_employee_ingress_group_event("acc_employee_2") is True  # noqa: SLF001
    canonical = ledger.window(
        tenant_key="tenant_1",
        chat_id="oc_team",
        current_message_id="om_shared",
    )
    assert len(canonical.records) == 1
    assert canonical.records[0].transport_principal_id == "main_bot"
    store.close()
    writer.close()


def test_canonical_partial_uses_journal_order_and_reports_warning(tmp_path: Path) -> None:
    writer, store, ledger = _ledger(tmp_path)
    for message_id, text, timestamp in (
        ("om_prior", "prior", 1.0),
        ("om_current", "current", 2.0),
    ):
        ledger.publish(
            tenant_key="tenant_1",
            chat_id="oc_team",
            thread_id="",
            message_id=message_id,
            transport_principal_id="main_bot",
            transport_event_id=f"evt_{message_id}",
            payload=GroupEventPayload(
                sender_id="ou_user",
                sender_id_type="open_id",
                sender_type="user",
                sender_tenant_key="tenant_1",
                text=text,
                timestamp=timestamp,
            ),
        )
    request = AuthorizedContextRequest(
        tenant_key="tenant_1",
        agent_id="agt_worker",
        bot_principal_id="bot_worker",
        app_id="cli_worker",
        channel_generation=1,
        chat_id="oc_team",
        thread_root_message_id="om_current",
        feishu_thread_id="",
        current_message_id="om_current",
        requester_principal_id="ou_user",
    )
    snapshot = ledger.assemble_partial(
        request,
        warning_reason=ContextUnavailableReason.ORDERING,
    )

    assert snapshot.quality is ContextQuality.CANONICAL_PARTIAL
    assert [item.text for item in snapshot.group_messages] == ["prior"]
    assert snapshot.thread_messages[0].text == "current"
    assert [item.code for item in snapshot.warnings] == ["order_unavailable"]
    store.close()
    writer.close()


def test_canonical_partial_recovers_legacy_root_id_in_thread_field(
    tmp_path: Path,
) -> None:
    writer, store, ledger = _ledger(tmp_path)
    ledger.publish(
        tenant_key="tenant_1",
        chat_id="oc_team",
        thread_id="om_topic_root",
        message_id="om_current",
        transport_principal_id="main_bot",
        transport_event_id="evt_current",
        payload=_payload(timestamp=2.0),
    )
    request = AuthorizedContextRequest(
        tenant_key="tenant_1",
        agent_id="agt_worker",
        bot_principal_id="bot_worker",
        app_id="cli_worker",
        channel_generation=1,
        chat_id="oc_team",
        thread_root_message_id="om_topic_root",
        feishu_thread_id="omt_topic",
        current_message_id="om_current",
        requester_principal_id="ou_user",
    )

    snapshot = ledger.assemble_partial(
        request,
        warning_reason=ContextUnavailableReason.SOURCE,
    )

    assert snapshot.thread_messages[0].thread_id == "omt_topic"
    assert snapshot.watermark is not None
    assert snapshot.watermark.feishu_thread_id == "omt_topic"
    store.close()
    writer.close()


def test_canonical_partial_rejects_legacy_root_without_feishu_thread_id(
    tmp_path: Path,
) -> None:
    writer, store, ledger = _ledger(tmp_path)
    ledger.publish(
        tenant_key="tenant_1",
        chat_id="oc_team",
        thread_id="om_topic_root",
        message_id="om_current",
        transport_principal_id="main_bot",
        transport_event_id="evt_current",
        payload=_payload(timestamp=2.0),
    )
    request = AuthorizedContextRequest(
        tenant_key="tenant_1",
        agent_id="agt_worker",
        bot_principal_id="bot_worker",
        app_id="cli_worker",
        channel_generation=1,
        chat_id="oc_team",
        thread_root_message_id="om_topic_root",
        feishu_thread_id="",
        current_message_id="om_current",
        requester_principal_id="ou_user",
    )

    with pytest.raises(ContextUnavailableError) as raised:
        ledger.assemble_partial(
            request,
            warning_reason=ContextUnavailableReason.SOURCE,
        )

    assert raised.value.reason is ContextUnavailableReason.ROOT_THREAD_BINDING
    store.close()
    writer.close()


def test_canonical_partial_rejects_nonmatching_legacy_topic_as_context_error(
    tmp_path: Path,
) -> None:
    writer, store, ledger = _ledger(tmp_path)
    for thread_id, message_id, timestamp in (
        ("om_other_topic_root", "om_other", 1.0),
        ("om_current_topic_root", "om_current", 2.0),
    ):
        ledger.publish(
            tenant_key="tenant_1",
            chat_id="oc_team",
            thread_id=thread_id,
            message_id=message_id,
            transport_principal_id="main_bot",
            transport_event_id=f"evt_{message_id}",
            payload=_payload(timestamp=timestamp),
        )
    request = AuthorizedContextRequest(
        tenant_key="tenant_1",
        agent_id="agt_worker",
        bot_principal_id="bot_worker",
        app_id="cli_worker",
        channel_generation=1,
        chat_id="oc_team",
        thread_root_message_id="om_current_topic_root",
        feishu_thread_id="omt_current_topic",
        current_message_id="om_current",
        requester_principal_id="ou_user",
    )

    with pytest.raises(ContextUnavailableError) as raised:
        ledger.assemble_partial(
            request,
            warning_reason=ContextUnavailableReason.SOURCE,
        )

    assert raised.value.reason is ContextUnavailableReason.ROOT_THREAD_BINDING
    store.close()
    writer.close()
