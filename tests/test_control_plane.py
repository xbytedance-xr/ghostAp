"""Tests for src.feishu.control_plane module."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from src.feishu.control_plane import ControlPlane
from src.tasking import TaskEvent, TaskStatus


class TestControlPlane:
    """ControlPlane unit tests — verifies deferred exit and system command gating."""

    def _make_cp(self, *, scheduler=None, project_manager=None, exit_fn=None):
        scheduler = scheduler or MagicMock()
        pm = project_manager or MagicMock()
        fn = exit_fn or MagicMock()
        cp = ControlPlane(scheduler, pm, fn)
        return cp

    def test_is_system_cmd_inflight_initially_false(self):
        cp = self._make_cp()
        try:
            assert cp.is_system_cmd_inflight("chat1") is False
        finally:
            cp.stop()

    def test_system_cmd_gate_tracks_running(self):
        cp = self._make_cp()
        try:
            ev = MagicMock(spec=TaskEvent)
            ev.task_type = "system_help"
            ev.chat_id = "chat1"
            ev.project_id = None
            ev.status = TaskStatus.RUNNING
            cp.on_scheduler_event(ev)
            assert cp.is_system_cmd_inflight("chat1") is True
        finally:
            cp.stop()

    def test_system_cmd_gate_clears_on_success(self):
        cp = self._make_cp()
        try:
            ev_run = MagicMock(spec=TaskEvent)
            ev_run.task_type = "system_help"
            ev_run.chat_id = "chat1"
            ev_run.project_id = None
            ev_run.status = TaskStatus.RUNNING
            cp.on_scheduler_event(ev_run)

            ev_done = MagicMock(spec=TaskEvent)
            ev_done.task_type = "system_help"
            ev_done.chat_id = "chat1"
            ev_done.project_id = None
            ev_done.status = TaskStatus.SUCCEEDED
            cp.on_scheduler_event(ev_done)
            assert cp.is_system_cmd_inflight("chat1") is False
        finally:
            cp.stop()

    def test_request_deferred_exit_and_should_defer(self):
        scheduler = MagicMock()
        scheduler.list_tasks.return_value = []
        cp = self._make_cp(scheduler=scheduler)
        try:
            assert cp.should_defer_exit(chat_id="c1", project_id=None) is False
        finally:
            cp.stop()

    def test_stop_is_idempotent(self):
        cp = self._make_cp()
        cp.stop()
        cp.stop()  # should not raise
