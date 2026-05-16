"""Tests for delivery sequence and binding management."""

import threading

from src.card.delivery.binding import BindingStore
from src.card.delivery.sequence import SequenceManager


class TestSequenceManager:
    """SequenceManager tests."""

    def test_sequence_increments(self):
        sm = SequenceManager()
        assert sm.next_sequence("card_1") == 1
        assert sm.next_sequence("card_1") == 2
        assert sm.next_sequence("card_1") == 3

    def test_sequence_floor_raise(self):
        sm = SequenceManager()
        sm.next_sequence("card_1")  # 1
        sm.next_sequence("card_1")  # 2
        sm.raise_floor("card_1", 10)
        # Next should be 11 (floor + 1)
        assert sm.next_sequence("card_1") == 11
        assert sm.next_sequence("card_1") == 12

    def test_sequence_floor_no_decrease(self):
        sm = SequenceManager()
        sm.raise_floor("card_1", 10)
        sm.raise_floor("card_1", 5)  # Should not decrease
        assert sm.next_sequence("card_1") == 11

    def test_sequence_independent_cards(self):
        sm = SequenceManager()
        assert sm.next_sequence("card_a") == 1
        assert sm.next_sequence("card_b") == 1
        assert sm.next_sequence("card_a") == 2

    def test_current_without_increment(self):
        sm = SequenceManager()
        sm.next_sequence("card_1")
        sm.next_sequence("card_1")
        assert sm.current("card_1") == 2

    def test_reset(self):
        sm = SequenceManager()
        sm.next_sequence("card_1")
        sm.raise_floor("card_1", 5)
        sm.reset("card_1")
        assert sm.current("card_1") == 0
        assert sm.next_sequence("card_1") == 1

    def test_thread_safety(self):
        sm = SequenceManager()
        results = []

        def increment():
            for _ in range(100):
                results.append(sm.next_sequence("card_1"))

        threads = [threading.Thread(target=increment) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 500 values should be unique
        assert len(set(results)) == 500


class TestBindingStore:
    """BindingStore tests."""

    def test_create_and_get(self):
        store = BindingStore()
        binding = store.create("sess_1", "chat_abc")
        assert binding.session_id == "sess_1"
        assert binding.chat_id == "chat_abc"
        assert store.get("sess_1") is binding

    def test_get_nonexistent(self):
        store = BindingStore()
        assert store.get("nonexistent") is None

    def test_set_page(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        store.set_page("sess_1", 0, "msg_1", "card_1", "sig_abc", "hello")

        binding = store.get("sess_1")
        assert 0 in binding.pages
        page = binding.pages[0]
        assert page.message_id == "msg_1"
        assert page.card_id == "card_1"
        assert page.signature == "sig_abc"
        assert page.last_text == "hello"

    def test_update_text(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        store.set_page("sess_1", 0, "msg_1", "card_1", "sig_1")
        store.update_text("sess_1", 0, "new text")

        binding = store.get("sess_1")
        assert binding.pages[0].last_text == "new text"

    def test_update_signature(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        store.set_page("sess_1", 0, "msg_1", "card_1", "sig_old")
        store.update_signature("sess_1", 0, "sig_new")

        binding = store.get("sess_1")
        assert binding.pages[0].signature == "sig_new"

    def test_remove(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        removed = store.remove("sess_1")
        assert removed is not None
        assert removed.session_id == "sess_1"
        assert store.get("sess_1") is None

    def test_page_count(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        assert store.page_count("sess_1") == 0
        store.set_page("sess_1", 0, "m1", "c1", "s1")
        store.set_page("sess_1", 1, "m2", "c2", "s2")
        assert store.page_count("sess_1") == 2

    def test_multi_page_management(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        store.set_page("sess_1", 0, "m1", "c1", "s1", "text1")
        store.set_page("sess_1", 1, "m2", "c2", "s2", "text2")
        store.set_page("sess_1", 2, "m3", "c3", "s3", "text3")

        binding = store.get("sess_1")
        assert len(binding.pages) == 3
        assert binding.pages[2].last_text == "text3"

    def test_has_returns_true_for_existing(self):
        store = BindingStore()
        store.create("sess_1", "chat_abc")
        assert store.has("sess_1") is True

    def test_has_returns_false_for_missing(self):
        store = BindingStore()
        assert store.has("nonexistent") is False
        # Also verify after removal
        store.create("sess_2", "chat_xyz")
        store.remove("sess_2")
        assert store.has("sess_2") is False
