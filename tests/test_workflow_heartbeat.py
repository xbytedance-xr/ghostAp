"""Tests for the Workflow Engine progress heartbeat (Task B2).

The heartbeat is a daemon thread that periodically calls ``_fire_progress``
while a workflow run is active, so the progress card (and the new elapsed
counters) keep refreshing even when no agent start/done/phase event fires
during a long blocking ``agent()`` call.

These tests never spawn a real Node.js subprocess. They drive the heartbeat
helper methods directly and use ``threading.Event`` synchronization + a tiny
patched interval to stay deterministic and fast.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import src.workflow_engine.engine as engine_mod
from src.workflow_engine.engine import WorkflowEngine


class TestWorkflowHeartbeat(unittest.TestCase):
    """Validate the heartbeat thread lifecycle and _fire_progress cadence."""

    def _make_engine(self, root_path: str = "/tmp") -> WorkflowEngine:
        return WorkflowEngine(
            chat_id="test_chat",
            root_path=root_path,
            agent_type="coco",
            engine_name="TestEngine",
        )

    def test_heartbeat_calls_fire_progress_repeatedly(self):
        """With a tiny interval, the heartbeat thread should call
        _fire_progress multiple times, then stop cleanly."""
        engine = self._make_engine()

        beats = threading.Event()
        call_count = {"n": 0}
        lock = threading.Lock()  # leaf lock: only guards the test counter

        def _count():
            with lock:
                call_count["n"] += 1
                if call_count["n"] >= 3:
                    beats.set()

        # Patch the module-level interval tiny (the loop reads it fresh each
        # iteration) and replace _fire_progress with a counting stub.
        with patch.object(engine_mod, "PROGRESS_HEARTBEAT_S", 0.01):
            with patch.object(engine, "_fire_progress", side_effect=_count):
                engine._start_heartbeat()
                try:
                    # Wait until at least 3 beats have fired (generous timeout).
                    fired = beats.wait(timeout=5.0)
                    self.assertTrue(fired, "heartbeat did not fire _fire_progress repeatedly")
                finally:
                    engine._stop_heartbeat()

        # Thread must be fully stopped after _stop_heartbeat.
        self.assertIsNone(engine._heartbeat_thread)
        with lock:
            self.assertGreaterEqual(call_count["n"], 3)

    def test_heartbeat_start_is_idempotent(self):
        """Calling _start_heartbeat twice must not spawn a second thread."""
        engine = self._make_engine()

        with patch.object(engine_mod, "PROGRESS_HEARTBEAT_S", 0.05):
            with patch.object(engine, "_fire_progress"):
                engine._start_heartbeat()
                first_thread = engine._heartbeat_thread
                self.assertIsNotNone(first_thread)
                try:
                    engine._start_heartbeat()
                    # Same thread object — no duplicate heartbeat.
                    self.assertIs(engine._heartbeat_thread, first_thread)
                finally:
                    engine._stop_heartbeat()

        self.assertIsNone(engine._heartbeat_thread)
        self.assertFalse(first_thread.is_alive())

    def test_heartbeat_stop_without_start_is_safe(self):
        """_stop_heartbeat with no running thread must not raise."""
        engine = self._make_engine()
        # Should be a no-op (no thread was started).
        engine._stop_heartbeat()
        self.assertIsNone(engine._heartbeat_thread)
        self.assertTrue(engine._heartbeat_stop.is_set())

    def test_heartbeat_swallows_fire_progress_errors(self):
        """A raising _fire_progress must not kill the heartbeat thread nor
        propagate out of the loop."""
        engine = self._make_engine()

        beats = threading.Event()
        call_count = {"n": 0}
        lock = threading.Lock()  # leaf lock: only guards the test counter

        def _boom():
            with lock:
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    beats.set()
            raise RuntimeError("boom")

        with patch.object(engine_mod, "PROGRESS_HEARTBEAT_S", 0.01):
            with patch.object(engine, "_fire_progress", side_effect=_boom):
                engine._start_heartbeat()
                try:
                    fired = beats.wait(timeout=5.0)
                    self.assertTrue(fired, "heartbeat stopped after first error")
                    # Thread should still be alive despite repeated exceptions.
                    self.assertTrue(engine._heartbeat_thread.is_alive())
                finally:
                    engine._stop_heartbeat()

        self.assertIsNone(engine._heartbeat_thread)

    def test_on_stop_sets_heartbeat_stop_event(self):
        """_on_stop must signal the heartbeat to stop (best-effort guard)."""
        engine = self._make_engine()
        self.assertFalse(engine._heartbeat_stop.is_set())
        engine._on_stop()
        self.assertTrue(engine._heartbeat_stop.is_set())


if __name__ == "__main__":
    unittest.main()
