"""Tests for force_complete_task permission/audit model (AC-20)."""
from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import SlockTask, TaskStatus, TaskTimelineEvent
from src.slock_engine.task_board_manager import TaskBoardManager


def _make_task(task_id: str = "task-1", claimed_by: str = "agent-1") -> SlockTask:
    """Create a minimal test task."""
    return SlockTask(
        task_id=task_id,
        content="test task",
        status=TaskStatus.IN_PROGRESS,
        claimed_by=claimed_by,
        claimed_at=time.time(),
        created_in="oc_test",
    )


class TestForceCompleteAudit:
    """Verify force_complete_task permission and audit behavior."""

    def setup_method(self):
        self.task = _make_task()
        tasks = [self.task]
        self.notifier = MagicMock()
        router = MagicMock()
        router.task_claim = MagicMock()

        self.tbm = TaskBoardManager(
            lock=threading.RLock(),
            tasks=tasks,
            channel_getter=lambda: None,
            chat_id_getter=lambda: "chat_test",
            dirty_getter=lambda: False,
            dirty_setter=MagicMock(),
            router=router,
            memory=MagicMock(),
            registry_get=MagicMock(return_value=None),
            execute_agent_fn=MagicMock(),
            notifier=self.notifier,
        )

    def test_system_actor_allowed_and_logged(self, caplog):
        """actor_id='system:escalation' should be allowed and audit logged."""
        with caplog.at_level(logging.INFO):
            self.tbm.force_complete_task(
                "task-1",
                reason="超时中止",
                actor_id="system:escalation",
            )
        assert self.task.status == TaskStatus.DONE
        assert "System force_complete" in caplog.text
        assert "system:escalation" in caplog.text

    def test_system_timeout_actor_allowed(self, caplog):
        """actor_id='system:timeout' should also be allowed."""
        with caplog.at_level(logging.INFO):
            self.tbm.force_complete_task(
                "task-1",
                reason="超时",
                actor_id="system:timeout",
            )
        assert self.task.status == TaskStatus.DONE
        assert "system:timeout" in caplog.text

    def test_empty_actor_id_rejected(self):
        """Empty actor_id (no system: prefix) should raise PermissionError."""
        with pytest.raises(PermissionError, match="requires actor_id"):
            self.tbm.force_complete_task("task-1", reason="test")

    def test_unauthorized_user_rejected(self):
        """Non-admin, non-owner, non-claimer user should be rejected."""
        with pytest.raises(PermissionError, match="not authorized"):
            self.tbm.force_complete_task(
                "task-1",
                reason="test",
                actor_id="user_123",
                admin_ids={"admin_1"},
                owner_id="owner_1",
            )

    def test_admin_user_allowed(self):
        """Admin user should be allowed."""
        self.tbm.force_complete_task(
            "task-1",
            reason="admin action",
            actor_id="admin_1",
            admin_ids={"admin_1"},
        )
        assert self.task.status == TaskStatus.DONE

    def test_claimer_allowed(self):
        """Task claimer should be allowed."""
        self.tbm.force_complete_task(
            "task-1",
            reason="self complete",
            actor_id="agent-1",  # matches claimed_by
        )
        assert self.task.status == TaskStatus.DONE

    def test_owner_allowed(self):
        """Channel owner should be allowed."""
        self.tbm.force_complete_task(
            "task-1",
            reason="owner action",
            actor_id="owner_1",
            owner_id="owner_1",
        )
        assert self.task.status == TaskStatus.DONE
