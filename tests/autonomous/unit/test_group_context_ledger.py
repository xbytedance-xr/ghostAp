from __future__ import annotations

from pathlib import Path

import pytest

from src.autonomous.context.group_ledger import (
    GroupContextLedger,
    GroupEventPayload,
    GroupLedgerError,
)
from src.autonomous.context.models import (
    AuthorizedContextRequest,
    ContextQuality,
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
