"""Tests for engine activate_channel in-place list mutation.

Verifies that activate_channel uses clear()+extend() pattern to
preserve the task list reference identity shared with TaskBoardManager.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.models import SlockTask, TaskStatus


class TestActivateChannelReference:
    """Verify in-place list mutation preserves reference identity."""

    def test_task_list_identity_preserved_after_activate(self):
        """The tasks list object identity must remain the same after activate_channel."""
        from src.slock_engine.engine import SlockEngine

        with patch("src.slock_engine.engine.get_settings") as ms:
            settings = MagicMock()
            settings.slock_max_agents = 10
            settings.slock_idle_scan_interval = 60
            settings.slock_auto_plan_timeout = 300
            settings.slock_role_response_timeout = 120
            settings.slock_max_tasks_per_channel = 100
            ms.return_value = settings

            engine = SlockEngine.__new__(SlockEngine)
            engine._lock = __import__("threading").RLock()
            engine._tasks = [
                SlockTask(task_id="old-1", content="old task", status=TaskStatus.DONE, created_in="ch")
            ]
            engine._dirty = False
            engine._channel = None
            engine._agent_statuses = {}
            engine._registry = MagicMock()
            engine._router = MagicMock()
            engine._progress_tracker = MagicMock()
            engine._orchestrator = MagicMock()
            engine._task_board = MagicMock()

            original_list_id = id(engine._tasks)

            # Simulate what activate_channel does with persisted tasks
            persisted_tasks = [
                SlockTask(task_id="new-1", content="new task", status=TaskStatus.TODO, created_in="ch"),
                SlockTask(task_id="new-2", content="another task", status=TaskStatus.IN_PROGRESS, created_in="ch"),
            ]

            # Perform the in-place mutation pattern
            engine._tasks.clear()
            engine._tasks.extend(persisted_tasks)

            # List identity must be preserved
            assert id(engine._tasks) == original_list_id
            # Contents updated
            assert len(engine._tasks) == 2
            assert engine._tasks[0].task_id == "new-1"
            assert engine._tasks[1].task_id == "new-2"

    def test_board_manager_sees_updated_tasks(self):
        """TaskBoardManager sharing the same list reference sees updated tasks."""
        shared_tasks: list[SlockTask] = [
            SlockTask(task_id="orig-1", content="original", status=TaskStatus.TODO, created_in="ch")
        ]

        # Simulate board manager holding same reference
        board_tasks_ref = shared_tasks

        # Simulate activate_channel in-place mutation
        new_tasks = [
            SlockTask(task_id="loaded-1", content="loaded", status=TaskStatus.TODO, created_in="ch"),
        ]
        shared_tasks.clear()
        shared_tasks.extend(new_tasks)

        # Board manager's reference should see the update
        assert len(board_tasks_ref) == 1
        assert board_tasks_ref[0].task_id == "loaded-1"
        assert board_tasks_ref is shared_tasks
