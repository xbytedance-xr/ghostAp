"""Tests for authorized Context handoff and employee Response Channel."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from src.autonomous.context import (
    AssembledContext,
    AuthorizedContextRequest,
    ContextLayer,
    ContextPreparingExecutionPort,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeExecutionInput,
    ThreadWatermark,
)
from src.autonomous.provisioning.response import (
    DeliveryState,
    EmployeeResponseChannel,
)


class _FakeDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        if self._fail:
            raise RuntimeError("delivery failed")
        self.sent.append(kwargs)
        return "msg_1"

    def send_card(self, **kwargs):
        if self._fail:
            raise RuntimeError("delivery failed")
        self.sent.append(kwargs)
        return "msg_2"


def _authorized_request() -> AuthorizedContextRequest:
    return AuthorizedContextRequest(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        bot_principal_id="bot_alpha",
        app_id="cli_alpha",
        channel_generation=3,
        chat_id="oc_team",
        thread_root_message_id="om_root",
        feishu_thread_id="omt_thread",
        current_message_id="om_current",
        requester_principal_id="ou_requester",
        system_prompt_token_reserve=2,
        constraints_digest="a" * 64,
    )


def _assembled_context() -> AssembledContext:
    from tests.autonomous.unit.test_employee_thread_context import _msg

    current = _msg("om_current", create=3_000, position=1)
    current = replace(
        current,
        is_current=True,
        chat_id="oc_team",
        thread_id="omt_thread",
        sender_id="ou_requester",
    )
    watermark = ThreadWatermark(
        thread_root_id="om_root",
        last_message_id="om_current",
        last_timestamp=3.0,
        message_count=1,
        tenant_key="tenant_1",
        chat_id="oc_team",
        feishu_thread_id="omt_thread",
        revision_digest="b" * 64,
    )
    return AssembledContext(
        thread_messages=(current,),
        group_messages=(),
        l1_summary="",
        l2_summary="",
        total_tokens_estimate=3,
        watermark=watermark,
        layers_used=(ContextLayer.THREAD_FULL,),
        snapshot_hash="c" * 64,
        system_prompt_tokens_reserved=2,
        constraints_digest="a" * 64,
    )


class _FakeContextService:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    def assemble(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class _FakeAuthorizedDelegate:
    def __init__(self) -> None:
        self.calls: list[EmployeeExecutionInput] = []

    def execute(self, execution_input: EmployeeExecutionInput) -> str:
        self.calls.append(execution_input)
        return "task_authorized"


class _FakeAuthorityFence:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    def run_if_current(self, request, action):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return action()


class TestContextPreparingExecutionPort:
    def test_authorized_contracts_are_frozen_and_exclude_untrusted_fields(self) -> None:
        request = _authorized_request()
        with pytest.raises(FrozenInstanceError):
            request.app_id = "cli_other"  # type: ignore[misc]
        assert not hasattr(request, "credential_ref")
        assert not hasattr(request, "system_prompt")
        assert not hasattr(request, "text")
        assert not hasattr(request, "raw_payload")

        execution_input = EmployeeExecutionInput(
            request=request,
            tool="codex",
            model="gpt",
            effort="high",
            context=_assembled_context(),
        )
        with pytest.raises(FrozenInstanceError):
            execution_input.tool = "other"  # type: ignore[misc]

    @pytest.mark.parametrize("generation", [0, -1, True])
    def test_request_rejects_invalid_generation(self, generation) -> None:
        with pytest.raises(ValueError):
            replace(_authorized_request(), channel_generation=generation)

    def test_prepares_context_once_then_delegates_once(self) -> None:
        request = _authorized_request()
        snapshot = _assembled_context()
        context_service = _FakeContextService(result=snapshot)
        delegate = _FakeAuthorizedDelegate()
        fence = _FakeAuthorityFence()
        port = ContextPreparingExecutionPort(
            context_service=context_service,
            authority_fence=fence,
            delegate=delegate,
        )

        result = port.execute(
            request,
            tool="codex",
            model="gpt",
            effort="high",
        )

        assert result == "task_authorized"
        assert context_service.calls == [request]
        assert fence.calls == [request]
        assert len(delegate.calls) == 1
        assert delegate.calls[0].request is request
        assert delegate.calls[0].context is snapshot

    def test_execution_input_rejects_context_authority_mismatches(self) -> None:
        request = _authorized_request()
        snapshot = _assembled_context()
        assert snapshot.watermark is not None
        mismatches = (
            replace(
                snapshot,
                watermark=replace(snapshot.watermark, chat_id="oc_other"),
            ),
            replace(
                snapshot,
                thread_messages=tuple(
                    replace(message, is_current=False)
                    for message in snapshot.thread_messages
                ),
            ),
            replace(snapshot, system_prompt_tokens_reserved=3),
            replace(
                snapshot,
                thread_messages=tuple(
                    replace(message, sender_id="ou_attacker")
                    if message.is_current
                    else message
                    for message in snapshot.thread_messages
                ),
            ),
        )

        for context in mismatches:
            with pytest.raises(ValueError):
                EmployeeExecutionInput(
                    request=request,
                    tool="codex",
                    model="gpt",
                    effort="high",
                    context=context,
                )

    @pytest.mark.parametrize("reason", list(ContextUnavailableReason))
    def test_context_failure_preserves_typed_error_and_never_delegates(
        self,
        reason: ContextUnavailableReason,
    ) -> None:
        expected = ContextUnavailableError(reason)
        context_service = _FakeContextService(error=expected)
        delegate = _FakeAuthorizedDelegate()
        port = ContextPreparingExecutionPort(
            context_service=context_service,
            authority_fence=_FakeAuthorityFence(),
            delegate=delegate,
        )

        with pytest.raises(ContextUnavailableError) as raised:
            port.execute(
                _authorized_request(),
                tool="codex",
                model="gpt",
                effort="high",
            )

        assert raised.value is expected
        assert delegate.calls == []

    @pytest.mark.parametrize("value", [{}, object()])
    def test_rejects_raw_payloads_before_context_or_delegate(self, value) -> None:
        context_service = _FakeContextService(result=_assembled_context())
        delegate = _FakeAuthorizedDelegate()
        port = ContextPreparingExecutionPort(
            context_service=context_service,
            authority_fence=_FakeAuthorityFence(),
            delegate=delegate,
        )
        with pytest.raises(TypeError):
            port.execute(value, tool="codex", model="gpt", effort="high")
        assert context_service.calls == []
        assert delegate.calls == []

    def test_post_assembly_revocation_is_fenced_before_delegate(self) -> None:
        request = _authorized_request()
        expected = ContextUnavailableError(ContextUnavailableReason.SCOPE)
        context_service = _FakeContextService(result=_assembled_context())
        fence = _FakeAuthorityFence(expected)
        delegate = _FakeAuthorizedDelegate()
        port = ContextPreparingExecutionPort(
            context_service=context_service,
            authority_fence=fence,
            delegate=delegate,
        )

        with pytest.raises(ContextUnavailableError) as raised:
            port.execute(request, tool="codex", model="gpt", effort="high")

        assert raised.value is expected
        assert context_service.calls == [request]
        assert fence.calls == [request]
        assert delegate.calls == []


class TestEmployeeResponseChannel:
    def test_enqueue_text_delivers(self) -> None:
        delivery = _FakeDelivery()
        channel = EmployeeResponseChannel(delivery=delivery)
        entry = channel.enqueue_text(
            agent_id="agt_alpha",
            chat_id="chat_1",
            text="Task completed!",
        )
        assert entry.state == DeliveryState.DELIVERED
        assert len(delivery.sent) == 1
        assert delivery.sent[0]["agent_id"] == "agt_alpha"

    def test_enqueue_card_delivers(self) -> None:
        delivery = _FakeDelivery()
        channel = EmployeeResponseChannel(delivery=delivery)
        entry = channel.enqueue_card(
            agent_id="agt_alpha",
            chat_id="chat_1",
            card_json={"type": "template", "data": {}},
        )
        assert entry.state == DeliveryState.DELIVERED

    def test_delivery_failure_retries(self) -> None:
        delivery = _FakeDelivery(fail=True)
        channel = EmployeeResponseChannel(delivery=delivery, max_retry=3)
        entry = channel.enqueue_text(
            agent_id="agt_alpha",
            chat_id="chat_1",
            text="will fail",
        )
        assert entry.state == DeliveryState.PENDING
        assert entry.attempts == 1
        channel.retry_pending()
        channel.retry_pending()
        assert entry.state == DeliveryState.FAILED
        assert entry.attempts == 3

    def test_pending_count(self) -> None:
        delivery = _FakeDelivery(fail=True)
        channel = EmployeeResponseChannel(delivery=delivery, max_retry=5)
        channel.enqueue_text(agent_id="agt_a", chat_id="c1", text="a")
        channel.enqueue_text(agent_id="agt_b", chat_id="c2", text="b")
        assert channel.pending_count() == 2
        assert channel.pending_count("agt_a") == 1
