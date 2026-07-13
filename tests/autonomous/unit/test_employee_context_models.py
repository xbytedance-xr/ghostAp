from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.autonomous.context import (
    AssembledContext,
    ContextLayer,
    ContextLayerMetrics,
    ContextMessage,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeMessageScope,
    MessageRevision,
    ThreadContextConfig,
    ThreadWatermark,
    TrimmingRecord,
)
from src.config.settings import Settings


def _scope(**overrides: str) -> EmployeeMessageScope:
    values = {
        "tenant_key": "tenant_1",
        "agent_id": "agt_1",
        "bot_principal_id": "bot_1",
        "app_id": "cli_1",
        "chat_id": "oc_1",
        "thread_root_message_id": "om_root",
        "feishu_thread_id": "omt_1",
        "current_message_id": "om_current",
    }
    values.update(overrides)
    return EmployeeMessageScope(**values)


def _message(**overrides: object) -> ContextMessage:
    values: dict[str, object] = {
        "message_id": "om_1",
        "sender_id": "ou_1",
        "sender_type": "user",
        "text": "hello",
        "timestamp": 1_700_000_000.0,
        "chat_id": "oc_1",
        "thread_id": "omt_1",
        "root_id": "om_root",
        "sender_id_type": "open_id",
        "sender_tenant_key": "tenant_1",
        "msg_type": "text",
        "create_time_ms": 1_700_000_000_000,
        "update_time_ms": 1_700_000_000_000,
        "thread_message_position": 1,
    }
    values.update(overrides)
    return ContextMessage(**values)


def test_employee_message_scope_is_frozen_and_excludes_credentials() -> None:
    scope = _scope()

    assert not hasattr(scope, "credential_ref")
    with pytest.raises(FrozenInstanceError):
        scope.agent_id = "agt_other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_key", ""),
        ("agent_id", "employee_1"),
        ("agent_id", "agt_ "),
        ("bot_principal_id", "principal_1"),
        ("app_id", "app_1"),
        ("chat_id", "chat_1"),
        ("thread_root_message_id", "root_1"),
        ("feishu_thread_id", "thread_1"),
        ("current_message_id", "msg_1"),
    ],
)
def test_employee_message_scope_rejects_blank_or_wrong_identifier_space(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        _scope(**{field: value})


def test_message_revision_is_stable_and_covers_updates_and_tombstones() -> None:
    original = _message()
    edited = _message(
        text="edited",
        update_time_ms=1_700_000_001_000,
        edited=True,
    )
    deleted = _message(text="stale secret", deleted=True)

    first = MessageRevision.from_message(original)
    assert first == MessageRevision.from_message(original)
    assert first.digest != MessageRevision.from_message(edited).digest
    assert MessageRevision.from_message(deleted).digest != first.digest
    assert deleted.text == ""


def test_message_revision_covers_chat_and_thread_identity() -> None:
    original = _message()

    assert MessageRevision.from_message(original).digest != MessageRevision.from_message(
        _message(chat_id="oc_2")
    ).digest
    assert MessageRevision.from_message(original).digest != MessageRevision.from_message(
        _message(thread_id="omt_2", root_id="om_other")
    ).digest


def test_message_order_key_is_deterministic_when_positions_are_missing() -> None:
    positioned = _message(thread_message_position=2, message_position=9)
    fallback = _message(
        message_id="om_2",
        thread_message_position=None,
        message_position=None,
        create_time_ms=1_700_000_001_000,
        update_time_ms=1_700_000_001_000,
    )

    assert positioned.order_key == (2, 9, 1_700_000_000_000, "om_1")
    assert fallback.order_key == (-1, -1, 1_700_000_001_000, "om_2")


def test_context_error_exposes_reason_without_detail_text() -> None:
    error = ContextUnavailableError(
        ContextUnavailableReason.PAGINATION,
        internal_detail="secret response body",
    )

    assert error.reason is ContextUnavailableReason.PAGINATION
    assert str(error) == "CONTEXT_UNAVAILABLE:pagination"
    assert "secret response body" not in repr(error)
    legacy = ContextUnavailableError("app_secret=LEAK")
    assert str(legacy) == "CONTEXT_UNAVAILABLE:source"
    assert "LEAK" not in repr(legacy)


def test_context_message_and_watermark_reject_missing_scope() -> None:
    with pytest.raises(ValueError, match="chat_id"):
        _message(chat_id="")
    root_message = _message(
        message_id="om_root",
        thread_id="omt_1",
        root_id="",
    )
    assert root_message.root_id == ""
    with pytest.raises(ValueError, match="root_id requires thread_id"):
        _message(thread_id="", root_id="om_root")
    with pytest.raises(ValueError, match="revision_digest"):
        ThreadWatermark(
            thread_root_id="om_root",
            last_message_id="om_1",
            last_timestamp=1.0,
            message_count=1,
            tenant_key="tenant_1",
            chat_id="oc_1",
            feishu_thread_id="omt_1",
            revision_digest="",
        )


def test_snapshot_metadata_is_frozen_and_diagnostics_do_not_repeat_plaintext() -> None:
    message = _message(is_current=True)
    revision = MessageRevision.from_message(message)
    watermark = ThreadWatermark(
        thread_root_id="om_root",
        last_message_id="om_1",
        last_timestamp=message.timestamp,
        message_count=1,
        revision=1,
        tenant_key="tenant_1",
        chat_id="oc_1",
        feishu_thread_id="omt_1",
        revision_digest=revision.digest,
    )
    metrics = ContextLayerMetrics(
        layer=ContextLayer.THREAD_FULL,
        source_messages=1,
        retained_messages=1,
        source_chars=5,
        retained_chars=5,
    )
    snapshot = AssembledContext(
        thread_messages=(message,),
        group_messages=(),
        l1_summary="",
        l2_summary="",
        total_tokens_estimate=2,
        watermark=watermark,
        layers_used=(ContextLayer.THREAD_FULL,),
        layer_metrics=(metrics,),
        trimming_trace=(TrimmingRecord(ContextLayer.L2_GROUP, 0, 0),),
        snapshot_hash=revision.digest,
    )

    assert snapshot.layer_metrics == (metrics,)
    assert "hello" not in repr(snapshot.diagnostics())
    with pytest.raises(FrozenInstanceError):
        snapshot.snapshot_hash = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ContextLayerMetrics(ContextLayer.THREAD_FULL, 1.5, 1, 1, 1),
        lambda: ContextLayerMetrics(ContextLayer.THREAD_FULL, True, 1, 1, 1),
        lambda: TrimmingRecord(ContextLayer.L2_GROUP, True, 0),
        lambda: TrimmingRecord(ContextLayer.L2_GROUP, 0, 1.5),
    ],
)
def test_structural_counts_require_real_integers(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_thread_context_config_is_frozen_and_reads_settings() -> None:
    settings = Settings(
        _env_file=None,
        autonomous_thread_context_max_messages=321,
        autonomous_thread_context_max_chars=123_456,
        autonomous_group_context_max_messages=45,
        autonomous_context_max_tokens=32_000,
        autonomous_thread_context_page_size=40,
        autonomous_group_context_page_size=15,
        autonomous_context_fetch_timeout_seconds=12.5,
        autonomous_context_max_pages=99,
    )

    config = ThreadContextConfig.from_settings(settings)

    assert config.max_thread_messages == 321
    assert config.max_context_chars == 123_456
    assert config.max_group_messages == 45
    assert config.max_context_tokens == 32_000
    assert config.thread_page_size == 40
    assert config.group_page_size == 15
    assert config.fetch_timeout_seconds == 12.5
    assert config.max_pages == 99
    with pytest.raises(FrozenInstanceError):
        config.max_pages = 1  # type: ignore[misc]


def test_thread_context_config_preserves_legacy_positional_order() -> None:
    config = ThreadContextConfig(200, 50, 128_000, 0.25, 40, 15, 12.0)

    assert config.tokens_per_char == 0.25
    assert config.thread_page_size == 40
    assert config.group_page_size == 15
    assert config.fetch_timeout_seconds == 12.0
