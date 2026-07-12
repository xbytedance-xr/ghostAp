"""Tests for Employee Message Router and Response Channel."""

from __future__ import annotations

import pytest

from src.autonomous.provisioning.response import (
    DeliveryState,
    EmployeeResponseChannel,
    OutboxEntry,
)
from src.autonomous.provisioning.router import (
    EmployeeMessageRouter,
    InboundMessage,
    RouteDecision,
)


class _FakeMembership:
    def __init__(self, members: dict[str, set[str]] | None = None, tenants: dict[str, str] | None = None) -> None:
        self._members = members or {}
        self._tenants = tenants or {}

    def is_member(self, agent_id, chat_id):
        return chat_id in self._members.get(agent_id, set())

    def get_tenant(self, agent_id):
        return self._tenants.get(agent_id, "")


class _FakeExecution:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[dict] = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("execution failed")
        return f"task_{len(self.calls)}"


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


def _msg(agent_id="agt_alpha", chat_id="chat_1") -> InboundMessage:
    return InboundMessage(
        agent_id=agent_id,
        tenant_key="tenant_1",
        chat_id=chat_id,
        thread_root_id="thread_1",
        message_id="msg_1",
        sender_id="user_1",
        text="do something",
        timestamp=1000.0,
    )


class TestEmployeeMessageRouter:
    def test_routes_to_execution(self) -> None:
        membership = _FakeMembership(
            members={"agt_alpha": {"chat_1"}},
            tenants={"agt_alpha": "tenant_1"},
        )
        execution = _FakeExecution()
        router = EmployeeMessageRouter(membership=membership, execution=execution)
        result = router.route(_msg(), tool="codex", model="gpt", effort="high")
        assert result.decision == RouteDecision.EXECUTE
        assert result.task_id == "task_1"
        assert len(execution.calls) == 1

    def test_cross_tenant_rejected(self) -> None:
        membership = _FakeMembership(
            members={"agt_alpha": {"chat_1"}},
            tenants={"agt_alpha": "other_tenant"},
        )
        router = EmployeeMessageRouter(membership=membership, execution=_FakeExecution())
        result = router.route(_msg(), tool="codex", model="gpt", effort="high")
        assert result.decision == RouteDecision.REJECT
        assert "cross-tenant" in result.error

    def test_non_member_rejected(self) -> None:
        membership = _FakeMembership(
            members={"agt_alpha": set()},
            tenants={"agt_alpha": "tenant_1"},
        )
        router = EmployeeMessageRouter(membership=membership, execution=_FakeExecution())
        result = router.route(_msg(), tool="codex", model="gpt", effort="high")
        assert result.decision == RouteDecision.REJECT
        assert "not a member" in result.error

    def test_execution_failure_returns_unknown(self) -> None:
        membership = _FakeMembership(
            members={"agt_alpha": {"chat_1"}},
            tenants={"agt_alpha": "tenant_1"},
        )
        router = EmployeeMessageRouter(membership=membership, execution=_FakeExecution(fail=True))
        result = router.route(_msg(), tool="codex", model="gpt", effort="high")
        assert result.decision == RouteDecision.UNKNOWN

    def test_queue_depth_and_drain(self) -> None:
        membership = _FakeMembership(
            members={"agt_alpha": {"chat_1"}},
            tenants={"agt_alpha": "tenant_1"},
        )
        execution = _FakeExecution()
        router = EmployeeMessageRouter(membership=membership, execution=execution)
        import threading
        barrier = threading.Barrier(2, timeout=5)
        blocking_execution = _FakeExecution()
        original_execute = blocking_execution.execute

        def slow_execute(**kwargs):
            barrier.wait()
            return original_execute(**kwargs)
        blocking_execution.execute = slow_execute
        router._execution = blocking_execution
        t = threading.Thread(target=lambda: router.route(_msg(), tool="codex", model="gpt", effort="high"))
        t.start()
        import time; time.sleep(0.05)
        msg2 = InboundMessage(
            agent_id="agt_alpha", tenant_key="tenant_1",
            chat_id="chat_1", thread_root_id="", message_id="msg_2",
            sender_id="user_2", text="second",
        )
        result2 = router.route(msg2, tool="codex", model="gpt", effort="high")
        assert result2.decision == RouteDecision.QUEUE
        barrier.wait()
        t.join(timeout=5)
        # After first execution completes, auto-drain dispatches msg_2
        # so it should have been executed by the blocking_execution
        assert len(blocking_execution.calls) >= 1


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
