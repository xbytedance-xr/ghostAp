from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.acp.manager import ACPSessionManager


def _make_mock_session(*, last_active: float = 0.0, server_running: bool = True) -> MagicMock:
    session = MagicMock()
    session.last_active = last_active
    session.is_server_running.return_value = server_running
    session.session_id = "mock-sid-001"
    session.message_count = 1
    session.to_snapshot.return_value = {"id": "mock-sid-001"}
    session.close.return_value = None
    return session


class TestKeepaliveThreadLifecycle:
    def test_keepalive_thread_starts_when_interval_positive(self):
        mgr = ACPSessionManager("coco", keepalive_interval=1)
        try:
            assert mgr._keepalive_thread is not None
            assert mgr._keepalive_thread.is_alive()
            assert mgr._keepalive_thread.daemon is True
        finally:
            mgr.cleanup_all()
            assert mgr._keepalive_thread is None

    def test_keepalive_no_thread_when_interval_zero(self):
        mgr = ACPSessionManager("coco", keepalive_interval=0)
        try:
            assert mgr._keepalive_thread is None
        finally:
            mgr.cleanup_all()

    def test_cleanup_all_stops_keepalive_thread(self):
        mgr = ACPSessionManager("coco", keepalive_interval=1)
        t = mgr._keepalive_thread
        assert t is not None
        assert t.is_alive()
        mgr.cleanup_all()
        assert not t.is_alive()
        assert mgr._keepalive_thread is None


class TestKeepaliveSessionCleanup:
    def test_keepalive_cleans_dead_session(self):
        mgr = ACPSessionManager("coco", keepalive_interval=1, idle_healthcheck_s=0)
        try:
            session = _make_mock_session(last_active=time.time() - 300, server_running=False)
            key = "chat1:proj1"
            with mgr._lock:
                mgr._sessions[key] = session

            deadline = time.time() + 5
            while time.time() < deadline:
                with mgr._lock:
                    if key not in mgr._sessions:
                        break
                time.sleep(0.1)

            with mgr._lock:
                assert key not in mgr._sessions
            session.is_server_running.assert_called()
        finally:
            mgr.cleanup_all()

    def test_keepalive_keeps_active_session(self):
        mgr = ACPSessionManager("coco", keepalive_interval=1, idle_healthcheck_s=0)
        try:
            session = _make_mock_session(last_active=time.time() - 300, server_running=True)
            key = "chat2:proj2"
            with mgr._lock:
                mgr._sessions[key] = session

            time.sleep(2.5)

            with mgr._lock:
                assert key in mgr._sessions
            session.is_server_running.assert_called()
        finally:
            mgr.cleanup_all()
