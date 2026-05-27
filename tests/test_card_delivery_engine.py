"""Tests for CardDelivery engine."""

import threading
import time
import unittest.mock
from unittest.mock import MagicMock

import dataclasses
import pytest

from src.card.delivery.engine import CardDelivery, MutationOutcome, SequenceConflictError, TransportError
from src.card.delivery.types import MutationOutcome as MutationOutcomeType
from src.card.types import ActiveElement, RenderedCard
from tests.helpers.delivery_internals import DeliveryInspector


class MockCardClient:
    """Mock implementation of CardAPIClient."""

    def __init__(self):
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.elements: list[dict] = []
        self.streaming_creates: list[dict] = []
        self.card_references: list[dict] = []
        self._create_counter = 0
        self._raise_on_update: Exception | None = None
        self._raise_on_streaming_create: Exception | None = None

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        card_id = f"card_{self._create_counter}"
        self.creates.append({
            "chat_id": chat_id, "card_json": card_json,
            "reply_to": reply_to, "idempotency_key": idempotency_key,
        })
        return (msg_id, card_id)

    def update_card(self, card_id, card_json, *, sequence=0):
        if self._raise_on_update:
            raise self._raise_on_update
        self.updates.append({"card_id": card_id, "card_json": card_json, "sequence": sequence})

    def update_element(self, card_id, element_id, content, *, sequence=0):
        if self._raise_on_update:
            raise self._raise_on_update
        self.elements.append({"card_id": card_id, "element_id": element_id, "content": content, "sequence": sequence})

    def create_streaming_card(self, card_json):
        if self._raise_on_streaming_create:
            raise self._raise_on_streaming_create
        self._create_counter += 1
        card_id = f"stream_card_{self._create_counter}"
        self.streaming_creates.append({"card_json": card_json})
        return card_id

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, idempotency_key=None):
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        self.card_references.append({
            "chat_id": chat_id, "card_id": card_id,
            "reply_to": reply_to, "idempotency_key": idempotency_key,
        })
        return msg_id


class TestCardDeliveryCreate:
    """First delivery creates cards."""

    def test_first_deliver_creates_card_and_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [RenderedCard(
            _card_json={"body": {"elements": []}},
            structure_signature="sig_1", page_index=0, total_pages=1,
        )]

        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert len(outcomes) == 1
        assert outcomes[0].kind == "applied"
        assert len(client.creates) == 1
        assert client.creates[0]["chat_id"] == "chat_abc"

        binding = delivery.get_binding("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert binding.pages[0].signature == "sig_1"


class TestCardDeliveryUpdate:
    """Subsequent deliveries compare signatures."""

    def test_signature_change_triggers_update(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        r2 = [RenderedCard(_card_json={"updated": True}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "applied"
        assert len(client.updates) == 1

    def test_text_only_triggers_element_content(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="hello")
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", active_element=active, page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        active2 = ActiveElement(element_id="el_1", text="hello world")
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_1", active_element=active2, page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "applied"
        assert len(client.elements) == 1
        assert client.elements[0]["content"] == "hello world"

    def test_no_change_skips(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="same")
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", active_element=active, page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        outcomes = delivery.deliver("sess_1", "chat_abc", r1)
        assert outcomes[0].kind == "skipped"
        assert len(client.updates) == 0
        assert len(client.elements) == 0


class TestSequenceConflict:
    def test_sequence_conflict_reconcile(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        client._raise_on_update = SequenceConflictError(next_floor=10)
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert "sequence_conflict" in outcomes[0].message


class TestMultiPage:
    def test_multi_page_delivery(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert len(outcomes) == 2
        assert all(o.kind == "applied" for o in outcomes)

    def test_new_page_created_on_growth(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        r2 = [
            RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)
        assert outcomes[1].kind == "applied"
        assert len(client.creates) == 2

    def test_existing_history_pages_are_not_updated_after_continuation(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        first = [
            RenderedCard(_card_json={"page": 0, "title": "old"}, structure_signature="p0_t1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1, "title": "latest 1s"}, structure_signature="p1_t1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_1", "chat_abc", first)

        second = [
            RenderedCard(_card_json={"page": 0, "title": "old 2s"}, structure_signature="p0_t2", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1, "title": "latest 2s"}, structure_signature="p1_t2", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", second)
        assert [o.kind for o in outcomes] == ["skipped", "applied"]


class TestPageShrink:
    def test_shrink_removes_stale_page_from_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r2 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_1", "chat_abc", r2)

        r1 = [RenderedCard(_card_json={"page": 0}, structure_signature="sig_2", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        binding = delivery.get_binding("sess_1")
        assert 0 in binding.pages
        assert 1 not in binding.pages


class TestStreamingFallback:
    def test_streaming_success_uses_cardkit_path(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="streaming text")
        rendered = [RenderedCard(
            _card_json={"config": {"streaming_mode": True}, "body": {}},
            structure_signature="sig_1", active_element=active, page_index=0, total_pages=1,
        )]

        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert outcomes[0].kind == "applied"
        assert len(client.streaming_creates) == 1
        assert len(client.card_references) == 1
        assert len(client.creates) == 0

    def test_streaming_fallback_to_im_api(self):
        client = MockCardClient()
        client._raise_on_streaming_create = RuntimeError("CardKit unavailable")
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="fallback text")
        rendered = [RenderedCard(
            _card_json={"config": {"streaming_mode": True}, "body": {}},
            structure_signature="sig_1", active_element=active, page_index=0, total_pages=1,
        )]

        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert outcomes[0].kind == "applied"
        assert len(client.creates) == 1


class TestClose:
    def test_close_removes_binding_and_is_idempotent(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)
        assert delivery.get_binding("sess_1") is not None

        delivery.close("sess_1")
        assert delivery.get_binding("sess_1") is None

        # Second close does not raise
        delivery.close("sess_1")
        assert delivery.get_binding("sess_1") is None

        # Deliver after close is no-op
        outcomes = delivery.deliver("sess_1", "chat_abc", r1)
        assert outcomes == []


class TestTransportError:
    def test_transport_error_returns_reconcile(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        client._raise_on_update = TransportError("connection timeout")
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert "connection timeout" in outcomes[0].message

    def test_stale_binding_discards_session_and_short_circuits_remaining_pages(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered_v1 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_stale", "chat_abc", rendered_v1)

        update_attempts = {"n": 0}

        def stale_update(*args, **kwargs):
            update_attempts["n"] += 1
            raise TransportError("message not found", code=99992354)

        client.update_card = stale_update
        rendered_v2 = [
            RenderedCard(_card_json={"page": 0, "v": 2}, structure_signature="sig_2", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1, "v": 2}, structure_signature="sig_2", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_stale", "chat_abc", rendered_v2)

        assert [outcome.message for outcome in outcomes] == ["history_page_frozen", "recreate:99992354"]
        assert update_attempts["n"] == 1
        assert delivery.get_binding("sess_stale") is None

        client.update_card = MockCardClient.update_card.__get__(client, MockCardClient)
        outcomes = delivery.deliver("sess_stale", "chat_abc", rendered_v2)
        assert [outcome.kind for outcome in outcomes] == ["applied", "applied"]

    def test_transport_error_on_element_falls_back_to_update(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="hello")
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", active_element=active, page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        def failing_element(*args, **kwargs):
            raise TransportError("element push failed")

        client.update_element = failing_element
        active2 = ActiveElement(element_id="el_1", text="world")
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_1", active_element=active2, page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "applied"
        assert len(client.updates) == 1


class TestCreateCardFailure:
    def test_create_card_failure_returns_reconcile_no_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        def failing_create(*args, **kwargs):
            raise RuntimeError("API unavailable")
        client.create_card = failing_create

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)

        assert len(outcomes) == 1
        assert outcomes[0].kind == "reconcile"

        binding = delivery.get_binding("sess_1")
        if binding is not None:
            assert 0 not in binding.pages


class TestConcurrentDeliverClose:
    """Multi-threaded deliver/close race condition safety test."""

    def test_concurrent_deliver_and_close_no_exception(self):
        client = MockCardClient()
        delivery = CardDelivery(client)
        session_id = "concurrent_sess"
        errors: list[Exception] = []

        def deliver_task():
            try:
                cards = [RenderedCard(
                    page_index=0, _card_json={"header": {"title": {"content": "test"}}},
                    structure_signature="sig-concurrent",
                )]
                delivery.deliver(session_id, "chat_1", cards)
            except Exception as e:
                errors.append(e)

        def close_task():
            try:
                delivery.close(session_id)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = threading.Thread(target=deliver_task if i % 2 == 0 else close_task)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert delivery.get_binding(session_id) is None


class TestCardDeliveryTOCTOU:
    def test_deliver_during_close_no_api_call_after_cleanup(self):
        client = MockCardClient()
        delivery = CardDelivery(client)
        session_id = "toctou_1"

        rendered = [RenderedCard(_card_json={"body": {}}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver(session_id, "chat_1", rendered)

        barrier = threading.Barrier(2, timeout=10)
        results: dict[str, list] = {"deliver": [], "errors": []}

        def deliver_thread():
            try:
                barrier.wait()
                time.sleep(0.02)
                outcome = delivery.deliver(session_id, "chat_1", rendered)
                results["deliver"] = outcome
            except Exception as e:
                results["errors"].append(e)

        def close_thread():
            try:
                barrier.wait()
                delivery.close(session_id)
            except Exception as e:
                results["errors"].append(e)

        t1 = threading.Thread(target=deliver_thread)
        t2 = threading.Thread(target=close_thread)
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert not results["errors"]
        assert results["deliver"] == []
        assert len(client.creates) == 1


class TestDeliveryTimeout:
    def test_deliver_timeout_on_slow_api(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        def slow_create(*args, **kwargs):
            raise TimeoutError("Connection timed out after 30s")
        client.create_card = slow_create

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)

        assert outcomes[0].kind == "reconcile"

    def test_update_timeout_returns_reconcile(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        def timeout_update(*args, **kwargs):
            raise TimeoutError("Request timed out")
        client.update_card = timeout_update

        r2 = [RenderedCard(_card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)
        assert outcomes[0].kind == "reconcile"


class TestPartialMultipageFailure:
    def test_partial_multipage_create_failure_and_retry(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        call_count = 0
        original_create = client.create_card

        def _failing_second_create(chat_id, card_json, *, reply_to=None, idempotency_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated page 1 creation failure")
            return original_create(chat_id, card_json, reply_to=reply_to, idempotency_key=idempotency_key)

        client.create_card = _failing_second_create

        rendered = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_a", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_b", page_index=1, total_pages=2),
        ]

        outcomes = delivery.deliver("sess_partial", "chat_abc", rendered)
        assert outcomes[0].kind == "applied"
        assert outcomes[1].kind == "reconcile"

        # Retry: fix client and deliver again
        client.create_card = original_create
        outcomes2 = delivery.deliver("sess_partial", "chat_abc", rendered)
        assert outcomes2[0].kind == "skipped"
        assert outcomes2[1].kind == "applied"

    def test_retry_missing_page_reuses_same_idempotency_key(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        call_count = 0
        original_create = client.create_card

        def _failing_second_create(chat_id, card_json, *, reply_to=None, idempotency_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                _failing_second_create.last_key = idempotency_key
                raise TimeoutError("lost")
            return original_create(chat_id, card_json, reply_to=reply_to, idempotency_key=idempotency_key)
        _failing_second_create.last_key = None

        client.create_card = _failing_second_create
        rendered = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_a", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_b", page_index=1, total_pages=2),
        ]

        delivery.deliver("sess_retry", "chat_abc", rendered)
        failed_key = _failing_second_create.last_key

        client.create_card = original_create
        delivery.deliver("sess_retry", "chat_abc", rendered)
        assert failed_key
        assert client.creates[-1]["idempotency_key"] == failed_key


class TestSessionLockEviction:
    def _make_delivery(self, *, max_locks: int = 10, lock_ttl: float = 600.0):
        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=max_locks, session_lock_ttl=lock_ttl)
        return client, delivery

    def test_session_lock_evicted_after_close_and_ttl(self):
        import time as _time
        client, delivery = self._make_delivery(max_locks=10, lock_ttl=600.0)

        for i in range(9):
            sid = f"zombie_{i}"
            rendered = [RenderedCard(_card_json={}, structure_signature=f"sig_{i}", page_index=0, total_pages=1)]
            delivery.deliver(sid, "chat_1", rendered)

        inspector = DeliveryInspector.from_delivery(delivery)
        for i in range(9):
            inspector.timestamps[f"zombie_{i}"] = _time.monotonic() - 700.0
        for i in range(9):
            delivery._bindings.remove(f"zombie_{i}")

        assert len(inspector.session_locks) == 9
        inspector.evict_stale_two_phase()
        assert len(inspector.session_locks) < 9

    def test_session_lock_not_evicted_while_active(self):
        import time as _time
        client, delivery = self._make_delivery(max_locks=10, lock_ttl=600.0)

        for i in range(9):
            sid = f"active_{i}"
            rendered = [RenderedCard(_card_json={}, structure_signature=f"sig_{i}", page_index=0, total_pages=1)]
            delivery.deliver(sid, "chat_1", rendered)

        inspector = DeliveryInspector.from_delivery(delivery)
        for i in range(9):
            inspector.timestamps[f"active_{i}"] = _time.monotonic() - 700.0

        inspector.evict_stale_two_phase()
        for i in range(9):
            assert f"active_{i}" in inspector.session_locks


class TestEvictionConcurrencyStress:
    def test_concurrent_deliver_and_eviction_no_deadlock(self):
        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=50, session_lock_ttl=1.0)
        delivery._eviction_interval = 0.05

        num_threads = 25
        iterations_per_thread = 20
        barrier = threading.Barrier(num_threads, timeout=30)
        errors: list[Exception] = []

        def worker(thread_id: int):
            try:
                barrier.wait()
                for i in range(iterations_per_thread):
                    session_id = f"stress_t{thread_id}_i{i}"
                    rendered = [RenderedCard(
                        _card_json={"body": {"elements": [{"tag": "markdown", "content": f"t{thread_id}i{i}"}]}},
                        structure_signature=f"sig_{thread_id}_{i}", page_index=0,
                    )]
                    try:
                        delivery.deliver(session_id, f"chat_{thread_id}", rendered)
                    except Exception:
                        pass
                    if i % 3 == 0:
                        try:
                            delivery.close(session_id)
                        except Exception:
                            pass
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"{len(alive)} threads still alive"
        assert not errors
        delivery._shutdown()


class TestDeliverRejectedAtCapacity:
    def test_noop_degradation_when_capacity_full(self):
        client = MockCardClient()
        max_locks = 10
        delivery = CardDelivery(client, max_session_locks=max_locks)

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        for i in range(max_locks):
            delivery.deliver(f"session_{i}", "chat_1", rendered)

        delivery._lock_pool._lru_evict_oldest = lambda: None
        result = delivery.deliver("session_overflow", "chat_1", rendered)
        assert result[0].kind == "applied"
        delivery._shutdown()

    def test_existing_session_not_rejected(self):
        client = MockCardClient()
        max_locks = 5
        delivery = CardDelivery(client, max_session_locks=max_locks)

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        for i in range(max_locks):
            delivery.deliver(f"session_{i}", "chat_1", rendered)

        rendered_update = [RenderedCard(_card_json={"new": True}, structure_signature="sig_2", page_index=0, total_pages=1)]
        result = delivery.deliver("session_0", "chat_1", rendered_update)
        assert result[0].kind in ("applied", "reconcile")
        delivery._shutdown()


class TestDeliverEmptyRenderedOpenSession:
    def test_empty_rendered_new_session(self):
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            outcomes = delivery.deliver("sess_empty", "chat_1", [])
            assert outcomes == []
            assert len(client.creates) == 0
        finally:
            delivery._shutdown()


class TestNonContiguousPageIndex:
    def test_gap_in_page_index_no_crash(self):
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            rendered = [
                RenderedCard(_card_json={"body": {"elements": []}}, structure_signature="sig_0", page_index=0, total_pages=3),
                RenderedCard(_card_json={"body": {"elements": []}}, structure_signature="sig_2", page_index=2, total_pages=3),
            ]
            outcomes = delivery.deliver("sess_gap", "chat_1", rendered)
            assert len(outcomes) == 2
            assert len(client.creates) == 2
        finally:
            delivery._shutdown()


class TestTOCTOUClosedAfterLock:
    def test_closed_between_check_and_lock(self):
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            rendered = [RenderedCard(
                _card_json={"body": {"elements": []}}, structure_signature="sig1", page_index=0, total_pages=1,
            )]
            delivery.deliver("toctou_sess", "chat_1", rendered)
            delivery.close("toctou_sess")
            outcomes = delivery.deliver("toctou_sess", "chat_1", rendered)
            assert outcomes == []
            assert len(client.creates) == 1
        finally:
            delivery._shutdown()


class TestSessionLockPoolFullScan:
    def test_full_scan_cleans_stale_locks(self):
        from src.card.delivery.lock_pool import SessionLockPool

        pool = SessionLockPool(max_locks=100, lock_ttl=0.1, eviction_interval=9999)
        try:
            for i in range(10):
                pool.acquire(f"s{i}")
            time.sleep(0.2)
            pool._last_full_scan = 0
            pool._full_scan_if_needed()
            assert pool.count == 0
        finally:
            pool.shutdown()

    def test_full_scan_skips_active_bindings(self):
        from src.card.delivery.lock_pool import SessionLockPool

        pool = SessionLockPool(
            max_locks=100, lock_ttl=0.1, eviction_interval=9999,
            has_active_binding=lambda sid: sid == "s_active",
        )
        try:
            pool.acquire("s_active")
            pool.acquire("s_stale")
            time.sleep(0.2)
            pool._last_full_scan = 0
            pool._full_scan_if_needed()
            assert pool.contains("s_active")
            assert not pool.contains("s_stale")
        finally:
            pool.shutdown()


class TestAsyncDeliveryExactlyOnce:
    def test_terminal_hook_fires_exactly_once_async(self):
        from src.card.delivery.engine import CardDelivery
        from src.card.events import CardEvent, CardEventType
        from src.card.session import CardSession
        from src.card.session.config import SessionCallbacks, SessionConfig
        from src.card.state.models import CardMetadata

        hook_calls = []
        hook_done = threading.Event()

        class _TrackingHook:
            def on_dispatched(self, event, state):
                pass
            def on_terminal(self, state, reason):
                hook_calls.append(reason)
                hook_done.set()

        client = MockCardClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(mode_name="Test", tool_name="test", model_name="m1")
        callbacks = SessionCallbacks(hooks=(_TrackingHook(),))
        config = SessionConfig(metadata=metadata, sync_delivery=False)

        session = CardSession(
            chat_id="async_test_chat", config=config,
            delivery=delivery, callbacks=callbacks, session_id="async_sess_1",
        )
        session._sync_delivery = False

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent.completed(summary="done"))

        assert hook_done.wait(timeout=5.0)
        assert len(hook_calls) == 1
        assert session.closed


# ---------------------------------------------------------------------------
# Delivery close race (merged)
# ---------------------------------------------------------------------------


class _SlowCreateClient:
    def __init__(self, delay: float = 0.1):
        self._delay = delay
        self._create_counter = 0
        self.creates: list[dict] = []
        self.create_started = threading.Event()

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self.create_started.set()
        time.sleep(self._delay)
        self._create_counter += 1
        self.creates.append({"chat_id": chat_id})
        return (f"msg_{self._create_counter}", f"card_{self._create_counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass

    def create_streaming_card(self, card_json):
        raise NotImplementedError

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, idempotency_key=None):
        raise NotImplementedError


class TestDeliveryCloseRace:
    def _rendered(self, sig: str = "sig_1") -> list[RenderedCard]:
        return [RenderedCard(_card_json={"body": {"elements": []}}, structure_signature=sig, page_index=0, total_pages=1)]

    def test_close_during_create_card_no_exception(self):
        client = _SlowCreateClient(delay=0.15)
        delivery = CardDelivery(client)
        session_id = "race_1"
        errors: list[Exception] = []

        def deliver_thread():
            try:
                delivery.deliver(session_id, "chat_1", self._rendered())
            except Exception as e:
                errors.append(e)

        def close_thread():
            try:
                client.create_started.wait(timeout=5)
                delivery.close(session_id)
            except Exception as e:
                errors.append(e)

        t_deliver = threading.Thread(target=deliver_thread)
        t_close = threading.Thread(target=close_thread)
        t_deliver.start(); t_close.start()
        t_deliver.join(timeout=5); t_close.join(timeout=5)

        assert errors == []
        assert delivery.get_binding(session_id) is None
        delivery._shutdown()

    def test_per_session_lock_serializes_deliver_and_close(self):
        client = _SlowCreateClient(delay=0.2)
        delivery = CardDelivery(client)
        session_id = "race_5"
        deliver_outcomes: list = []
        close_done = threading.Event()

        def deliver_thread():
            deliver_outcomes.extend(delivery.deliver(session_id, "chat_1", self._rendered()))

        def close_thread():
            client.create_started.wait(timeout=5)
            delivery.close(session_id)
            close_done.set()

        t1 = threading.Thread(target=deliver_thread)
        t2 = threading.Thread(target=close_thread)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert len(deliver_outcomes) == 1
        assert deliver_outcomes[0].kind == "applied"
        assert close_done.is_set()
        assert delivery.get_binding(session_id) is None
        delivery._shutdown()


class TestCardDeliveryDestructor:
    def test_del_calls_lock_pool_shutdown(self):
        client = MagicMock()
        client.create_card = MagicMock(return_value=("msg_1", "card_1"))
        delivery = CardDelivery(client)
        mock_shutdown = MagicMock()
        delivery._lock_pool.shutdown = mock_shutdown
        delivery.__del__()
        mock_shutdown.assert_called_once()
        delivery._shutdown()

    def test_del_suppresses_exceptions(self):
        client = MagicMock()
        client.create_card = MagicMock(return_value=("msg_1", "card_1"))
        delivery = CardDelivery(client)
        delivery._lock_pool.shutdown = MagicMock(side_effect=RuntimeError("already shut down"))
        delivery.__del__()  # Should not raise
        delivery._lock_pool.shutdown = MagicMock()
        delivery._shutdown()


class TestMutationOutcomeStructure:
    def test_is_frozen_dataclass(self):
        outcome = MutationOutcomeType(kind="applied")
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.kind = "rejected"  # type: ignore[misc]

    @pytest.mark.parametrize("kind", ["applied", "reconcile", "skipped", "rejected"])
    def test_all_kinds_constructible(self, kind):
        o = MutationOutcomeType(kind=kind)
        assert o.kind == kind

    def test_default_message_is_empty(self):
        o = MutationOutcomeType(kind="applied")
        assert o.message == ""
