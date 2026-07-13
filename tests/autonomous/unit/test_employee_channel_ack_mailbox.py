from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from src.autonomous.ingress.models import (
    EmployeeIngressAck,
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
    IngressAcceptance,
)
from src.autonomous.provisioning.channel_worker import (
    IngressAckMailbox,
    _ConnectionAdmission,
)


def _transport_contract() -> tuple[
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
    EmployeeIngressAck,
]:
    payload = EmployeeIngressPayload(1, "ing_mailbox", ({"type": "text"},), ())
    metadata = EmployeeIngressMetadata(
        1, payload.envelope_id, "tenant", "agt_mailbox", "bot_mailbox",
        "cli_mailbox", 7, "conn_mailbox", "evt_mailbox", "om_mailbox",
        "im.message.receive_v1", "", "oc_mailbox", "", "ou_sender",
        "2026-07-13T00:00:00Z", payload.payload_sha256,
        payload.payload_sha256, payload.canonical_size_bytes, 0, 0,
    )
    acceptance = IngressAcceptance(
        1, "acc_mailbox", payload.envelope_id, metadata.dedup_key,
        metadata.semantic_digest, 1, "a" * 64, "2026-07-13T00:00:00Z",
    )
    return metadata, payload, EmployeeIngressAck(
        1, "req_mailbox", acceptance, metadata.agent_id, metadata.app_id,
        metadata.channel_generation, metadata.connection_id,
        metadata.semantic_digest, False, "2026-07-13T00:00:01Z",
    )


def test_mailbox_accepts_only_the_matching_current_transport_ack() -> None:
    _metadata, _payload, ack = _transport_contract()
    mailbox = IngressAckMailbox()
    pending = mailbox.register(
        request_id=ack.request_id,
        agent_id=ack.agent_id,
        app_id=ack.app_id,
        generation=ack.channel_generation,
        connection_id=ack.connection_id,
        semantic_digest=ack.semantic_digest,
        envelope_id=ack.acceptance.envelope_id,
        dedup_key=ack.acceptance.dedup_key,
    )

    assert mailbox.deliver(replace(ack, request_id="req_stale")) is False
    assert mailbox.deliver(replace(ack, connection_id="conn_stale")) is False
    assert mailbox.deliver(replace(ack, channel_generation=8)) is False
    assert mailbox.deliver(ack) is True
    assert mailbox.wait(pending, timeout=0.1) == ack


def test_mailbox_rejects_same_digest_ack_for_other_canonical_acceptance() -> None:
    _metadata, _payload, ack = _transport_contract()
    mailbox = IngressAckMailbox()
    pending = mailbox.register(
        request_id=ack.request_id,
        agent_id=ack.agent_id,
        app_id=ack.app_id,
        generation=ack.channel_generation,
        connection_id=ack.connection_id,
        semantic_digest=ack.semantic_digest,
        envelope_id=ack.acceptance.envelope_id,
        dedup_key=ack.acceptance.dedup_key,
    )
    wrong_envelope = replace(
        ack,
        acceptance=replace(ack.acceptance, envelope_id="ing_other"),
    )
    wrong_dedup = replace(
        ack,
        acceptance=replace(ack.acceptance, dedup_key="dedup_" + "b" * 64),
    )

    assert mailbox.deliver(wrong_envelope) is False
    assert mailbox.deliver(wrong_dedup) is False
    assert mailbox.deliver(ack) is True
    assert mailbox.wait(pending, timeout=0.1) == ack


def test_mailbox_timeout_removes_ownership_and_ignores_late_ack() -> None:
    _metadata, _payload, ack = _transport_contract()
    mailbox = IngressAckMailbox()
    pending = mailbox.register(
        request_id=ack.request_id,
        agent_id=ack.agent_id,
        app_id=ack.app_id,
        generation=ack.channel_generation,
        connection_id=ack.connection_id,
        semantic_digest=ack.semantic_digest,
        envelope_id=ack.acceptance.envelope_id,
        dedup_key=ack.acceptance.dedup_key,
    )

    with pytest.raises(TimeoutError, match="durable ACK"):
        mailbox.wait(pending, timeout=0.01)

    assert mailbox.deliver(ack) is False


def test_mailbox_parent_close_unblocks_every_pending_callback() -> None:
    _metadata, _payload, ack = _transport_contract()
    mailbox = IngressAckMailbox()
    pending = mailbox.register(
        request_id=ack.request_id,
        agent_id=ack.agent_id,
        app_id=ack.app_id,
        generation=ack.channel_generation,
        connection_id=ack.connection_id,
        semantic_digest=ack.semantic_digest,
        envelope_id=ack.acceptance.envelope_id,
        dedup_key=ack.acceptance.dedup_key,
    )
    result: list[BaseException] = []

    def wait() -> None:
        try:
            mailbox.wait(pending, timeout=10.0)
        except BaseException as exc:  # captured for the asserting thread
            result.append(exc)

    thread = threading.Thread(target=wait)
    thread.start()
    mailbox.close()
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], EOFError)
    assert mailbox.deliver(ack) is False


def test_reconnect_cancels_old_pending_and_mailbox_accepts_new_owner() -> None:
    _metadata, _payload, ack = _transport_contract()
    mailbox = IngressAckMailbox()
    old = mailbox.register(
        request_id=ack.request_id,
        agent_id=ack.agent_id,
        app_id=ack.app_id,
        generation=ack.channel_generation,
        connection_id=ack.connection_id,
        semantic_digest=ack.semantic_digest,
        envelope_id=ack.acceptance.envelope_id,
        dedup_key=ack.acceptance.dedup_key,
    )

    mailbox.cancel_pending()

    with pytest.raises(EOFError, match="parent unavailable"):
        mailbox.wait(old, timeout=0.1)
    assert mailbox.deliver(ack) is False
    new_ack = replace(
        ack,
        request_id="req_reconnected",
        connection_id="conn_reconnected",
    )
    new = mailbox.register(
        request_id=new_ack.request_id,
        agent_id=new_ack.agent_id,
        app_id=new_ack.app_id,
        generation=new_ack.channel_generation,
        connection_id=new_ack.connection_id,
        semantic_digest=new_ack.semantic_digest,
        envelope_id=new_ack.acceptance.envelope_id,
        dedup_key=new_ack.acceptance.dedup_key,
    )
    assert mailbox.deliver(new_ack) is True
    assert mailbox.wait(new, timeout=0.1) == new_ack


def test_stale_observer_emits_no_ready_and_current_ready_precedes_ingress() -> None:
    admission = _ConnectionAdmission()
    mailbox = IngressAckMailbox()
    stale_epoch = admission.epoch
    current_epoch = admission.begin_reconnect(mailbox)
    order: list[tuple[str, str]] = []
    waiting = threading.Event()

    def callback() -> None:
        waiting.set()
        _epoch, connection_id = admission.wait_snapshot(
            deadline=time.monotonic() + 1.0
        )
        order.append(("INGRESS", connection_id))

    thread = threading.Thread(target=callback)
    thread.start()
    assert waiting.wait(0.5)
    assert admission.publish_observed(
        "conn_stale",
        expected_epoch=stale_epoch,
        emit_ready=lambda: order.append(("READY", "conn_stale")),
    ) is False
    assert order == []
    assert admission.publish_observed(
        "conn_current",
        expected_epoch=current_epoch,
        emit_ready=lambda: order.append(("READY", "conn_current")),
    ) is True
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert order == [
        ("READY", "conn_current"),
        ("INGRESS", "conn_current"),
    ]
