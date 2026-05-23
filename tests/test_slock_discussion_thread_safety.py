"""Thread-safety tests for DiscussionThread.add_message / add_participant / get_* methods.

Validates that concurrent access via _data_lock does not corrupt shared state.
"""

import threading

import pytest

from src.slock_engine.models import DiscussionMessage, DiscussionThread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MESSAGES_PER_THREAD = 50
NUM_THREADS = 20


def _make_message(sender: str, idx: int) -> DiscussionMessage:
    return DiscussionMessage(
        sender_agent_id=sender,
        content=f"msg-{idx}",
        round_num=idx,
    )


# ---------------------------------------------------------------------------
# Tests: add_message thread safety
# ---------------------------------------------------------------------------


class TestAddMessageThreadSafety:
    """Concurrent add_message calls must not lose messages."""

    def test_concurrent_add_message_no_loss(self) -> None:
        """20 threads each add MESSAGES_PER_THREAD messages; total must be exact."""
        thread = DiscussionThread(thread_id="test-thread", channel_id="test_channel")
        barrier = threading.Barrier(NUM_THREADS)
        errors: list[Exception] = []

        def worker(thread_idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(MESSAGES_PER_THREAD):
                    msg = _make_message(f"agent-{thread_idx}", i)
                    thread.add_message(msg)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,))
            for t in range(NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Worker threads raised: {errors}"
        expected = NUM_THREADS * MESSAGES_PER_THREAD
        assert len(thread.messages) == expected, (
            f"Expected {expected} messages, got {len(thread.messages)}"
        )

    def test_get_messages_returns_snapshot(self) -> None:
        """get_messages returns a copy; mutations do not affect the thread."""
        thread = DiscussionThread(thread_id="snapshot-test", channel_id="test_channel")
        thread.add_message(_make_message("a", 0))
        thread.add_message(_make_message("b", 1))

        snapshot = thread.get_messages()
        assert len(snapshot) == 2

        # Mutating the snapshot should not affect internal state
        snapshot.pop()
        assert len(thread.get_messages()) == 2


# ---------------------------------------------------------------------------
# Tests: add_participant thread safety
# ---------------------------------------------------------------------------


class TestAddParticipantThreadSafety:
    """Concurrent add_participant calls must never produce duplicates."""

    def test_concurrent_add_participant_no_duplicates(self) -> None:
        """Multiple threads attempt to add the same agent_ids concurrently."""
        thread = DiscussionThread(thread_id="participant-test", channel_id="test_channel")
        num_unique_agents = 30
        barrier = threading.Barrier(NUM_THREADS)
        errors: list[Exception] = []

        def worker(_thread_idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(num_unique_agents):
                    thread.add_participant(f"agent-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,))
            for t in range(NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Worker threads raised: {errors}"
        participants = thread.get_participants()
        assert len(participants) == num_unique_agents, (
            f"Expected {num_unique_agents} unique participants, got {len(participants)}"
        )
        # Verify no duplicates
        assert len(set(participants)) == len(participants)

    def test_concurrent_add_unique_participants(self) -> None:
        """Each thread adds a unique participant; total must equal NUM_THREADS."""
        thread = DiscussionThread(thread_id="unique-participant-test", channel_id="test_channel")
        barrier = threading.Barrier(NUM_THREADS)
        errors: list[Exception] = []

        def worker(thread_idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                thread.add_participant(f"unique-agent-{thread_idx}")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,))
            for t in range(NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Worker threads raised: {errors}"
        participants = thread.get_participants()
        assert len(participants) == NUM_THREADS
        assert len(set(participants)) == NUM_THREADS

    def test_get_participants_returns_snapshot(self) -> None:
        """get_participants returns a copy; mutations do not affect the thread."""
        thread = DiscussionThread(thread_id="part-snapshot-test", channel_id="test_channel")
        thread.add_participant("agent-x")
        thread.add_participant("agent-y")

        snapshot = thread.get_participants()
        assert len(snapshot) == 2

        # Mutating the snapshot should not affect internal state
        snapshot.pop()
        assert len(thread.get_participants()) == 2


# ---------------------------------------------------------------------------
# Tests: mixed concurrent access
# ---------------------------------------------------------------------------


class TestMixedConcurrentAccess:
    """Simultaneous add_message and add_participant calls must not deadlock or corrupt."""

    def test_mixed_operations_no_corruption(self) -> None:
        """Half threads add messages, half add participants concurrently."""
        thread = DiscussionThread(thread_id="mixed-test", channel_id="test_channel")
        barrier = threading.Barrier(NUM_THREADS)
        errors: list[Exception] = []
        half = NUM_THREADS // 2

        def message_worker(thread_idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(MESSAGES_PER_THREAD):
                    thread.add_message(_make_message(f"agent-{thread_idx}", i))
            except Exception as exc:
                errors.append(exc)

        def participant_worker(thread_idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(MESSAGES_PER_THREAD):
                    thread.add_participant(f"participant-{thread_idx}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = []
        for t in range(half):
            threads.append(threading.Thread(target=message_worker, args=(t,)))
        for t in range(half, NUM_THREADS):
            threads.append(threading.Thread(target=participant_worker, args=(t,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Worker threads raised: {errors}"
        # Message count: half threads * MESSAGES_PER_THREAD
        expected_messages = half * MESSAGES_PER_THREAD
        assert len(thread.get_messages()) == expected_messages
        # Participant count: (NUM_THREADS - half) threads * MESSAGES_PER_THREAD unique ids
        expected_participants = (NUM_THREADS - half) * MESSAGES_PER_THREAD
        assert len(thread.get_participants()) == expected_participants
