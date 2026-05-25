"""Tests for TaskBoardManager._idle_scan_once method.

Covers seven distinct code paths:
1. No resolve_agent_for_role — returns early
2. No TODO tasks — returns early
3. No chain_manager — falls back to "coder" role
4. Agent not found — resolve returns None, skips task
5. Agent not IDLE — registry reports RUNNING, skips task
6. Successful claim + execution
7. execute_agent_fn raises — exception caught, no crash
"""

import threading
from unittest.mock import MagicMock

from src.slock_engine.models import SlockTask, TaskStatus
from src.slock_engine.task_board_manager import TaskBoardManager

# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------


def _make_manager(
    tasks=None,
    chain_manager=None,
    resolve_fn=None,
    registry_get=None,
    execute_fn=None,
):
    """Build a TaskBoardManager with all mocked dependencies."""
    lock = threading.RLock()
    task_list = tasks if tasks is not None else []
    channel = MagicMock()
    channel.channel_id = "test-chan"
    router = MagicMock()
    router.task_claim.claim.return_value = True

    _execute_fn = execute_fn or MagicMock()

    # Mock context implementing SlockEngineContext protocol
    class MockContext:
        pass

    context = MockContext()
    context.channel = channel
    context.chat_id = "chat-1"
    context.dirty = False

    def _set_dirty(value):
        pass

    context.set_dirty = _set_dirty
    context.execute_agent = lambda agent, content, callbacks: _execute_fn(agent, content, callbacks)
    context.resolve_agent_for_role = (lambda role, channel_id: resolve_fn(role, channel_id)) if resolve_fn else (lambda role, channel_id: None)
    context.execute_task = lambda task_id, agent_id, callbacks: None

    mgr = TaskBoardManager(
        lock=lock,
        tasks=task_list,
        context=context,
        router=router,
        memory=MagicMock(),
        registry_get=registry_get or MagicMock(return_value=None),
        chain_manager=chain_manager,
        notifier=MagicMock(),
    )
    return mgr


def _make_task(task_id="task-1", status=TaskStatus.TODO, content="do something"):
    """Create a real SlockTask with sensible defaults."""
    task = SlockTask(content=content)
    task.status = status
    if task_id != "task-1":
        task.task_id = task_id
    return task


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestIdleScanOnce:
    """Tests for TaskBoardManager._idle_scan_once."""

    def test_no_resolve_agent_for_role_returns_early(self):
        """Path 1: When resolve_agent_for_role returns None, _idle_scan_once returns early."""
        task = _make_task(status=TaskStatus.TODO)
        execute_fn = MagicMock()
        mgr = _make_manager(tasks=[task], resolve_fn=None, execute_fn=execute_fn)

        # Should not raise and should not attempt to process any tasks
        mgr._idle_scan_once()

        # execute_agent should never be called
        execute_fn.assert_not_called()

    def test_no_todo_tasks_returns_early(self):
        """Path 2: When all tasks are DONE/IN_PROGRESS, nothing happens."""
        done_task = _make_task(task_id="t-done", status=TaskStatus.DONE)
        in_progress_task = _make_task(task_id="t-ip", status=TaskStatus.IN_PROGRESS)

        resolve_fn = MagicMock(return_value=MagicMock(agent_id="agent-1"))
        mgr = _make_manager(
            tasks=[done_task, in_progress_task],
            resolve_fn=resolve_fn,
        )

        mgr._idle_scan_once()

        # resolve should never be called since no TODO tasks exist
        resolve_fn.assert_not_called()

    def test_no_chain_manager_defaults_to_coder_role(self):
        """Path 3: Without chain_manager, falls back to 'coder' role for resolve."""
        task = _make_task(status=TaskStatus.TODO)
        agent = MagicMock()
        agent.agent_id = "agent-coder"
        agent.status = "idle"

        resolve_fn = MagicMock(return_value=agent)
        registry_get = MagicMock(return_value=MagicMock(status="idle"))
        execute_fn = MagicMock()

        mgr = _make_manager(
            tasks=[task],
            chain_manager=None,  # No chain manager
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )

        mgr._idle_scan_once()

        # Should resolve with "coder" as the default role and channel_id
        resolve_fn.assert_called_with("coder", "test-chan")

    def test_agent_not_found_skips_task(self):
        """Path 4: resolve_agent_for_role returns None, task is skipped."""
        task = _make_task(status=TaskStatus.TODO)

        resolve_fn = MagicMock(return_value=None)
        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["reviewer"]
        template_mock.first_role = "reviewer"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            execute_fn=execute_fn,
        )

        mgr._idle_scan_once()

        # execute should not be called since agent was not found
        execute_fn.assert_not_called()

    def test_agent_not_idle_skips_task(self):
        """Path 5: Agent exists but registry reports non-idle status — skip."""
        task = _make_task(status=TaskStatus.TODO)

        agent = MagicMock()
        agent.agent_id = "agent-busy"

        resolve_fn = MagicMock(return_value=agent)

        # Registry reports the agent is RUNNING (not idle)
        running_entry = MagicMock()
        running_entry.status = "running"
        registry_get = MagicMock(return_value=running_entry)

        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )

        mgr._idle_scan_once()

        # Should not claim or execute since agent is not idle
        execute_fn.assert_not_called()

    def test_successful_claim_and_execution(self):
        """Path 6: Agent is idle, claim succeeds, execute_task is called."""
        task = _make_task(task_id="task-42", status=TaskStatus.TODO, content="implement feature X")

        agent = MagicMock()
        agent.agent_id = "agent-idle"
        agent.name = "IdleAgent"

        resolve_fn = MagicMock(return_value=agent)

        idle_entry = MagicMock()
        idle_entry.status = "idle"
        registry_get = MagicMock(return_value=idle_entry)

        execute_fn = MagicMock(return_value="result")

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )

        # Ensure claim succeeds
        mgr._router.task_claim.claim.return_value = True

        # Mock execute_task (now the entry point for idle scan dispatch)
        mgr.execute_task = MagicMock(return_value="result")

        mgr._idle_scan_once()

        # execute_task should have been called with task_id and agent_id
        mgr.execute_task.assert_called_once_with(task.task_id, agent.agent_id)

    def test_execute_agent_fn_raises_no_crash(self):
        """Path 7: execute_task raises an exception — scan does not crash."""
        task = _make_task(task_id="task-err", status=TaskStatus.TODO, content="risky task")

        agent = MagicMock()
        agent.agent_id = "agent-err"
        agent.name = "ErrAgent"

        resolve_fn = MagicMock(return_value=agent)

        idle_entry = MagicMock()
        idle_entry.status = "idle"
        registry_get = MagicMock(return_value=idle_entry)

        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )

        mgr._router.task_claim.claim.return_value = True

        # Mock execute_task to raise
        mgr.execute_task = MagicMock(side_effect=RuntimeError("something went wrong"))

        # Should NOT raise — exception is caught internally
        mgr._idle_scan_once()

        # Confirm execute was attempted
        mgr.execute_task.assert_called_once()


class TestTimelineWrites:
    """Verify that task operations write timeline events."""

    def test_claim_task_writes_timeline_event(self):
        """claim_task appends a 'claimed' timeline event."""
        task = _make_task(task_id="t-timeline", status=TaskStatus.TODO, content="timeline test")

        agent = MagicMock()
        agent.agent_id = "agent-tl"

        resolve_fn = MagicMock(return_value=agent)
        idle_entry = MagicMock()
        idle_entry.status = "idle"
        registry_get = MagicMock(return_value=idle_entry)
        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )
        mgr._router.task_claim.claim.return_value = True

        mgr._idle_scan_once()

        # Task should have a timeline event after claim
        assert len(task.timeline) >= 1
        event = task.timeline[0]
        assert event.event_type == "claimed"
        assert event.agent_id == "agent-tl"


class TestIdleScanLifecycle:
    """Verify _is_scanning guard and execute_task routing."""

    def test_is_scanning_prevents_reentry(self):
        """If _is_scanning is True, _idle_scan_once returns immediately."""
        task = _make_task(status=TaskStatus.TODO)
        agent = MagicMock()
        agent.agent_id = "agent-1"

        resolve_fn = MagicMock(return_value=agent)
        idle_entry = MagicMock()
        idle_entry.status = "idle"
        registry_get = MagicMock(return_value=idle_entry)
        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )
        mgr._router.task_claim.claim.return_value = True

        # Set scanning flag manually
        mgr._is_scanning = True

        mgr._idle_scan_once()

        # execute should NOT be called because guard blocked reentry
        execute_fn.assert_not_called()

    def test_scanning_flag_reset_after_scan(self):
        """_is_scanning flag is reset to False after scan completes."""
        task = _make_task(status=TaskStatus.TODO)
        agent = MagicMock()
        agent.agent_id = "agent-1"

        resolve_fn = MagicMock(return_value=agent)
        idle_entry = MagicMock()
        idle_entry.status = "idle"
        registry_get = MagicMock(return_value=idle_entry)
        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )
        mgr._router.task_claim.claim.return_value = True

        # Run scan normally
        mgr._idle_scan_once()

        # After scan, flag should be False
        assert mgr._is_scanning is False

    def test_routes_through_execute_task(self):
        """_idle_scan_once calls self.execute_task() instead of raw _execute_agent_fn."""
        task = _make_task(task_id="task-route", status=TaskStatus.TODO)
        agent = MagicMock()
        agent.agent_id = "agent-route"
        agent.name = "Router"

        resolve_fn = MagicMock(return_value=agent)
        idle_entry = MagicMock()
        idle_entry.status = "idle"
        registry_get = MagicMock(return_value=idle_entry)
        execute_fn = MagicMock()

        template_mock = MagicMock()
        template_mock.roles = ["coder"]
        template_mock.first_role = "coder"
        chain_manager = MagicMock()
        chain_manager.find_chain_for_task.return_value = template_mock

        mgr = _make_manager(
            tasks=[task],
            chain_manager=chain_manager,
            resolve_fn=resolve_fn,
            registry_get=registry_get,
            execute_fn=execute_fn,
        )
        mgr._router.task_claim.claim.return_value = True

        # Mock execute_task to verify it's called
        mgr.execute_task = MagicMock()

        mgr._idle_scan_once()

        # execute_task should be called (not raw _execute_agent_fn)
        mgr.execute_task.assert_called_once()
        call_args = mgr.execute_task.call_args[0]
        assert call_args[0] == task.task_id
        assert call_args[1] == "agent-route"

