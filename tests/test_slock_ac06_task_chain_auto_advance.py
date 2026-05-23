"""AC06: 任务链自动推进验证测试。

验证 coder 任务 DONE 后自动创建 reviewer 任务。
测试 TaskBoardManager.complete_task 和 _maybe_spawn_chain_successor。
"""

from __future__ import annotations

import threading
import uuid
from unittest.mock import MagicMock, call

import pytest

from src.slock_engine.models import AgentIdentity, SlockTask, TaskStatus
from src.slock_engine.task_board_manager import TaskBoardManager
from src.slock_engine.task_chain_manager import TaskChainManager


class TestAC06TaskChainAutoAdvance:
    """AC06: coder 任务完成后自动创建 reviewer 任务。"""

    def _make_mock_agent(self, agent_id: str, role: str, name: str = "") -> MagicMock:
        """创建一个模拟的 agent 对象。"""
        agent = MagicMock()
        agent.role = role
        agent.agent_id = agent_id
        agent.name = name or role
        return agent

    def _make_task_board_manager(
        self,
        tasks: list[SlockTask] | None = None,
        chain_manager: TaskChainManager | None = None,
    ) -> tuple[TaskBoardManager, dict]:
        """创建一个 TaskBoardManager 实例及其依赖 mock。"""
        tasks = tasks or []
        lock = threading.RLock()

        # Mock dependencies
        channel = MagicMock()
        channel.channel_id = "test-channel-123"

        router = MagicMock()
        router.task_claim = MagicMock()
        router.task_claim.claim.return_value = True
        router.task_claim.release.return_value = True

        memory = MagicMock()
        memory.write_task_board = MagicMock()

        # Mock registry getter
        registry_get = MagicMock()

        # Mock execute agent function
        execute_agent_fn = MagicMock()

        # Track dirty state
        dirty = {"value": False}

        def dirty_getter() -> bool:
            return dirty["value"]

        def dirty_setter(val: bool) -> None:
            dirty["value"] = val

        mgr = TaskBoardManager(
            lock=lock,
            tasks=tasks,
            channel_getter=lambda: channel,
            chat_id_getter=lambda: "test-chat-456",
            dirty_getter=dirty_getter,
            dirty_setter=dirty_setter,
            router=router,
            memory=memory,
            registry_get=registry_get,
            execute_agent_fn=execute_agent_fn,
            chain_manager=chain_manager,
        )

        mocks = {
            "channel": channel,
            "router": router,
            "memory": memory,
            "registry_get": registry_get,
            "execute_agent_fn": execute_agent_fn,
            "dirty": dirty,
        }

        return mgr, mocks

    def test_coder_task_done_creates_reviewer_task(self):
        """AC06: coder 任务完成后自动创建 reviewer 任务。"""
        # Setup: chain manager with coder->reviewer->tester
        chain_manager = TaskChainManager(chain_config="coder->reviewer->tester")

        # Create a coder task that's in progress
        coder_task = SlockTask(
            task_id=str(uuid.uuid4()),
            content="Implement authentication module",
            status=TaskStatus.IN_PROGRESS,
            claimed_by="agent-coder-001",
            created_in="test-channel-123",
        )

        mgr, mocks = self._make_task_board_manager(
            tasks=[coder_task],
            chain_manager=chain_manager,
        )

        # Mock the coder agent
        coder_agent = self._make_mock_agent("agent-coder-001", "coder", "Coder Alice")
        mocks["registry_get"].return_value = coder_agent

        # Complete the coder task
        result = mgr.complete_task(coder_task.task_id, "agent-coder-001")

        # Verify task was completed
        assert result is True

        # Verify a new task was created (reviewer task)
        # The manager should have 2 tasks now: the completed coder task + new reviewer task
        # Check that registry_get was called to get the agent's role
        mocks["registry_get"].assert_called_with("agent-coder-001")

        # Verify chain manager was queried for successor
        assert chain_manager.get_successor_role("coder") == "reviewer"

    def test_complete_task_calls_maybe_spawn_chain_successor(self):
        """验证 complete_task 调用 _maybe_spawn_chain_successor。"""
        chain_manager = TaskChainManager(chain_config="coder->reviewer")

        task = SlockTask(
            task_id=str(uuid.uuid4()),
            content="Test task",
            status=TaskStatus.IN_PROGRESS,
            claimed_by="agent-1",
        )

        mgr, mocks = self._make_task_board_manager(
            tasks=[task],
            chain_manager=chain_manager,
        )

        # Mock agent with role
        agent = self._make_mock_agent("agent-1", "coder")
        mocks["registry_get"].return_value = agent

        # Complete the task
        mgr.complete_task(task.task_id, "agent-1")

        # Verify the task is now DONE
        assert task.status == TaskStatus.DONE

    def test_maybe_spawn_chain_successor_returns_new_task(self):
        """验证 _maybe_spawn_chain_successor 创建新任务。"""
        chain_manager = TaskChainManager(chain_config="coder->reviewer")

        completed_task = SlockTask(
            task_id=str(uuid.uuid4()),
            content="Fix login bug",
            status=TaskStatus.DONE,
            claimed_by="agent-coder",
        )

        mgr, mocks = self._make_task_board_manager(
            tasks=[completed_task],
            chain_manager=chain_manager,
        )

        # Mock the coder agent
        coder_agent = self._make_mock_agent("agent-coder", "coder")
        mocks["registry_get"].return_value = coder_agent

        # Call _maybe_spawn_chain_successor directly
        new_task = mgr._maybe_spawn_chain_successor(completed_task, "agent-coder")

        # Verify a new task was created
        assert new_task is not None
        assert isinstance(new_task, SlockTask)
        assert new_task.task_id != completed_task.task_id

        # Verify the new task content references the chain
        assert "chain:coder->reviewer" in new_task.content
        assert "Fix login bug" in new_task.content

    def test_no_chain_manager_no_successor(self):
        """没有 chain_manager 时不创建后继任务。"""
        task = SlockTask(
            task_id=str(uuid.uuid4()),
            content="Test",
            status=TaskStatus.DONE,
            claimed_by="agent-1",
        )

        mgr, mocks = self._make_task_board_manager(
            tasks=[task],
            chain_manager=None,  # No chain manager
        )

        # Call _maybe_spawn_chain_successor
        result = mgr._maybe_spawn_chain_successor(task, "agent-1")

        # Should return None
        assert result is None

    def test_agent_without_role_no_successor(self):
        """Agent 没有 role 属性时不创建后继任务。"""
        chain_manager = TaskChainManager(chain_config="coder->reviewer")

        task = SlockTask(
            task_id=str(uuid.uuid4()),
            content="Test",
            status=TaskStatus.DONE,
            claimed_by="agent-1",
        )

        mgr, mocks = self._make_task_board_manager(
            tasks=[task],
            chain_manager=chain_manager,
        )

        # Mock agent without role
        agent = MagicMock()
        agent.role = ""  # Empty role
        mocks["registry_get"].return_value = agent

        result = mgr._maybe_spawn_chain_successor(task, "agent-1")
        assert result is None

    def test_terminal_role_no_successor(self):
        """链中的最后一个角色（终端角色）不创建后继任务。"""
        chain_manager = TaskChainManager(chain_config="coder->reviewer->tester")

        # Tester is the terminal role
        task = SlockTask(
            task_id=str(uuid.uuid4()),
            content="Run integration tests",
            status=TaskStatus.DONE,
            claimed_by="agent-tester",
        )

        mgr, mocks = self._make_task_board_manager(
            tasks=[task],
            chain_manager=chain_manager,
        )

        tester_agent = self._make_mock_agent("agent-tester", "tester")
        mocks["registry_get"].return_value = tester_agent

        result = mgr._maybe_spawn_chain_successor(task, "agent-tester")

        # Tester is terminal, no successor
        assert result is None

    def test_chain_manager_get_successor_role(self):
        """验证 TaskChainManager.get_successor_role 返回正确的后继角色。"""
        chain_manager = TaskChainManager(chain_config="coder->reviewer->tester")

        assert chain_manager.get_successor_role("coder") == "reviewer"
        assert chain_manager.get_successor_role("reviewer") == "tester"
        assert chain_manager.get_successor_role("tester") is None  # Terminal
        assert chain_manager.get_successor_role("unknown_role") is None

    def test_multiple_chain_templates(self):
        """支持多个链模板。"""
        chain_manager = TaskChainManager(
            chain_config="coder->reviewer, writer->editor"
        )

        assert chain_manager.get_successor_role("coder") == "reviewer"
        assert chain_manager.get_successor_role("writer") == "editor"
        assert chain_manager.get_successor_role("reviewer") is None
        assert chain_manager.get_successor_role("editor") is None

    def test_chain_tracking_start_advance(self):
        """验证链实例被正确追踪。"""
        chain_manager = TaskChainManager(chain_config="coder->reviewer->tester")

        origin_task_id = "task-origin-123"

        # Start chain
        instance = chain_manager.start_chain(origin_task_id, "coder")
        assert instance is not None
        assert instance.origin_task_id == origin_task_id
        assert instance.current_role == "coder"
        assert chain_manager.is_chain_active(origin_task_id)

        # Advance chain
        new_task_id = "task-reviewer-456"
        next_role = chain_manager.advance_chain(origin_task_id, "coder", new_task_id)
        assert next_role == "reviewer"

        # Check updated instance
        instance = chain_manager.get_chain_status(origin_task_id)
        assert instance is not None
        assert instance.current_role == "reviewer"
        assert "coder" in instance.completed_roles
        assert instance.task_ids["reviewer"] == new_task_id
