"""Tests for slock_engine optimization wave 1 (Tasks 29-32).

Covers:
- Task 29: Memory capacity enforcement (_enforce_l1_capacity, _enforce_text_capacity)
- Task 30: Crash recovery (recover_orphan_tasks)
- Task 31: Command aliases (slash_commands + intent_router)
- Task 32: Task chain manager
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from src.slock_engine.intent_router import IntentRouter
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory, SlockTask, TaskStatus
from src.slock_engine.slash_commands import SlockCommandAction, parse_slock_command
from src.slock_engine.task_board_manager import TaskBoardManager
from src.slock_engine.task_chain_manager import TaskChainManager
from src.slock_engine.task_router import TaskRouter

# ===========================================================================
# Task 29: Memory Capacity Enforcement
# ===========================================================================


class TestMemoryCapacity:
    """Tests for _enforce_l1_capacity and _enforce_text_capacity."""

    def test_l1_no_truncation_under_limit(self, tmp_path, monkeypatch):
        """Write memory smaller than max — verify no truncation occurs."""
        mm = MemoryManager(base_path=str(tmp_path))
        # Patch _get_l1_max_size to return a generous limit (10 KB)
        monkeypatch.setattr(MemoryManager, "_get_l1_max_size", staticmethod(lambda: 10_000))

        mem = SlockMemory(role="Coder", key_knowledge="Python", active_context="short context")
        mm.write_agent_memory("a1", mem)

        restored = mm.read_agent_memory("a1")
        assert restored.active_context == "short context"
        assert restored.role == "Coder"
        assert restored.key_knowledge == "Python"

    def test_l1_fifo_truncation_over_limit(self, tmp_path, monkeypatch):
        """Write a huge active_context exceeding limit, call _enforce_l1_capacity, verify truncation."""
        mm = MemoryManager(base_path=str(tmp_path))
        # Set a very small L1 max size (200 bytes) to force truncation
        monkeypatch.setattr(MemoryManager, "_get_l1_max_size", staticmethod(lambda: 200))

        # Create a memory with a large active_context that will exceed 200 bytes total
        large_context = "A" * 500
        mem = SlockMemory(role="Coder", active_context=large_context)
        # Write without enforcement first (bypass the normal write path)
        mm._write_agent_memory_unlocked("a1", mem)

        # Now enforce capacity
        mm._enforce_l1_capacity("a1")

        # Read back — should be truncated
        restored = mm.read_agent_memory("a1")
        serialized = restored.to_markdown().encode("utf-8")
        assert len(serialized) <= 200
        # The active_context should be the tail portion of the original
        assert restored.active_context.endswith("A" * min(len(restored.active_context), 50))

    def test_l2_text_truncation(self, tmp_path):
        """Test _enforce_text_capacity with text over limit returns truncated tail."""
        mm = MemoryManager(base_path=str(tmp_path))
        max_size = 50
        content = "X" * 200

        result = mm._enforce_text_capacity(content, max_size, "L2")

        result_bytes = result.encode("utf-8")
        assert len(result_bytes) <= max_size
        # Should keep the tail portion
        assert result == "X" * 50

    def test_l2_text_no_truncation_under_limit(self, tmp_path):
        """Under limit returns same text unchanged."""
        mm = MemoryManager(base_path=str(tmp_path))
        max_size = 500
        content = "Hello, this is a short text."

        result = mm._enforce_text_capacity(content, max_size, "L2")

        assert result == content


# ===========================================================================
# Task 30: Crash Recovery
# ===========================================================================


def _make_task_board_manager(tasks: list[SlockTask]) -> TaskBoardManager:
    """Helper to construct a TaskBoardManager with minimal mocks."""
    lock = threading.RLock()
    dirty_flag = [False]
    router = TaskRouter(task_claim_ttl=3600.0)
    memory = MagicMock()

    mgr = TaskBoardManager(
        lock=lock,
        tasks=tasks,
        channel_getter=lambda: None,
        chat_id_getter=lambda: "test-chan",
        dirty_getter=lambda: dirty_flag[0],
        dirty_setter=lambda v: dirty_flag.__setitem__(0, v),
        router=router,
        memory=memory,
        registry_get=lambda x: None,
        execute_agent_fn=lambda *a, **kw: None,
    )
    return mgr


class TestCrashRecovery:
    """Tests for recover_orphan_tasks in TaskBoardManager."""

    def test_orphan_recovery_downgrades_in_progress(self):
        """Tasks in IN_PROGRESS state should be downgraded to TODO."""
        tasks = [
            SlockTask(task_id="t1", content="task 1", status=TaskStatus.IN_PROGRESS, claimed_by="agent1"),
            SlockTask(task_id="t2", content="task 2", status=TaskStatus.IN_PROGRESS, claimed_by="agent2"),
        ]
        mgr = _make_task_board_manager(tasks)

        recovered = mgr.recover_orphan_tasks()

        assert len(recovered) == 2
        for task in tasks:
            assert task.status == TaskStatus.TODO

    def test_orphan_recovery_skips_done_and_todo(self):
        """Tasks in DONE/TODO should not be affected by recovery."""
        tasks = [
            SlockTask(task_id="t1", content="done task", status=TaskStatus.DONE),
            SlockTask(task_id="t2", content="todo task", status=TaskStatus.TODO),
            SlockTask(task_id="t3", content="orphan", status=TaskStatus.IN_PROGRESS, claimed_by="agent1"),
        ]
        mgr = _make_task_board_manager(tasks)

        recovered = mgr.recover_orphan_tasks()

        assert len(recovered) == 1
        assert recovered[0].task_id == "t3"
        # Verify DONE and TODO tasks unchanged
        assert tasks[0].status == TaskStatus.DONE
        assert tasks[1].status == TaskStatus.TODO

    def test_orphan_recovery_releases_claims(self):
        """After recovery, claimed_by should be None and claimed_at should be None."""
        tasks = [
            SlockTask(task_id="t1", content="task 1", status=TaskStatus.IN_PROGRESS, claimed_by="agent1"),
            SlockTask(task_id="t2", content="task 2", status=TaskStatus.IN_REVIEW, claimed_by="agent2"),
        ]
        mgr = _make_task_board_manager(tasks)

        recovered = mgr.recover_orphan_tasks()

        assert len(recovered) == 2
        for task in recovered:
            assert task.claimed_by is None
            assert task.claimed_at is None
            assert task.status == TaskStatus.TODO


# ===========================================================================
# Task 31: Command Aliases
# ===========================================================================


class TestCommandAliases:
    """Tests for alias resolution in slash_commands and intent_router."""

    def test_slash_r_resolves_to_role_list(self):
        """/r -> action is ROLE_LIST."""
        cmd = parse_slock_command("/r")
        assert cmd.action == SlockCommandAction.ROLE_LIST

    def test_slash_t_resolves_to_task_list(self):
        """/t -> action is TASK_LIST."""
        cmd = parse_slock_command("/t")
        assert cmd.action == SlockCommandAction.TASK_LIST

    def test_slash_c_resolves_to_council(self):
        """/c topic -> action is COUNCIL."""
        cmd = parse_slock_command("/c some topic to discuss")
        assert cmd.action == SlockCommandAction.COUNCIL
        assert cmd.args == "some topic to discuss"

    def test_slash_nr_resolves_to_new_role(self):
        """/nr TestAgent -> action is NEW_ROLE."""
        cmd = parse_slock_command("/nr TestAgent")
        assert cmd.action == SlockCommandAction.NEW_ROLE
        assert cmd.args == "TestAgent"

    def test_slash_s_resolves_to_slock(self):
        """/s status -> action is STATUS."""
        cmd = parse_slock_command("/s status")
        assert cmd.action == SlockCommandAction.STATUS

    def test_chinese_role_alias(self):
        """Chinese '角色列表' -> ROLE_LIST via intent_router fast_classify."""
        router = IntentRouter(confidence_threshold=0.7)
        result = router.fast_classify("角色列表")
        assert result is not None
        assert result.action == SlockCommandAction.ROLE_LIST

    def test_chinese_task_alias(self):
        """Chinese '任务列表' -> TASK_LIST via intent_router fast_classify."""
        router = IntentRouter(confidence_threshold=0.7)
        result = router.fast_classify("任务列表")
        assert result is not None
        assert result.action == SlockCommandAction.TASK_LIST

    def test_chinese_panel_alias(self):
        """Chinese '面板' -> HELP via intent_router fast_classify."""
        router = IntentRouter(confidence_threshold=0.7)
        result = router.fast_classify("面板")
        assert result is not None
        assert result.action == SlockCommandAction.HELP


# ===========================================================================
# Task 32: Task Chain Manager
# ===========================================================================


class TestTaskChainManager:
    """Tests for TaskChainManager template parsing, successor/predecessor resolution, and chain tracking."""

    def test_parse_default_template(self):
        """Default config produces coder->reviewer->tester chain."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")
        templates = tcm.templates
        assert len(templates) == 1
        template = templates[0]
        assert template.name == "coder->reviewer->tester"
        assert len(template.steps) == 3
        assert template.steps[0].role == "coder"
        assert template.steps[1].role == "reviewer"
        assert template.steps[2].role == "tester"

    def test_successor_coder_is_reviewer(self):
        """get_successor_role('coder') returns 'reviewer'."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")
        assert tcm.get_successor_role("coder") == "reviewer"

    def test_successor_tester_is_none(self):
        """get_successor_role('tester') returns None (terminal)."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")
        assert tcm.get_successor_role("tester") is None

    def test_predecessor_reviewer_is_coder(self):
        """get_predecessor_role('reviewer') returns 'coder'."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")
        assert tcm.get_predecessor_role("reviewer") == "coder"

    def test_should_chain_coder(self):
        """should_chain('coder') is True (has successor)."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")
        assert tcm.should_chain("coder") is True

    def test_should_chain_tester(self):
        """should_chain('tester') is False (terminal, no successor)."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")
        assert tcm.should_chain("tester") is False

    def test_start_and_advance_chain(self):
        """Start a chain from coder, advance through reviewer, verify instance tracking."""
        tcm = TaskChainManager(chain_config="coder->reviewer->tester")

        # Start chain
        instance = tcm.start_chain("task-origin-001", "coder")
        assert instance is not None
        assert instance.current_role == "coder"
        assert instance.origin_task_id == "task-origin-001"
        assert instance.task_ids["coder"] == "task-origin-001"
        assert tcm.is_chain_active("task-origin-001") is True

        # Advance: coder completes -> reviewer
        next_role = tcm.advance_chain("task-origin-001", "coder", "task-review-002")
        assert next_role == "reviewer"
        status = tcm.get_chain_status("task-origin-001")
        assert status is not None
        assert status.current_role == "reviewer"
        assert "coder" in status.completed_roles
        assert status.task_ids["reviewer"] == "task-review-002"

        # Advance: reviewer completes -> tester
        next_role = tcm.advance_chain("task-origin-001", "reviewer", "task-test-003")
        assert next_role == "tester"
        status = tcm.get_chain_status("task-origin-001")
        assert status is not None
        assert status.current_role == "tester"

        # Advance: tester completes -> None (chain complete)
        next_role = tcm.advance_chain("task-origin-001", "tester", "task-final-004")
        assert next_role is None
        # Chain should be removed from active tracking
        assert tcm.is_chain_active("task-origin-001") is False

    def test_multi_template_parsing(self):
        """Config 'a->b->c, x->y' produces 2 templates."""
        tcm = TaskChainManager(chain_config="a->b->c, x->y")
        templates = tcm.templates
        assert len(templates) == 2

        # First template: a->b->c
        assert templates[0].name == "a->b->c"
        assert len(templates[0].steps) == 3
        assert templates[0].steps[0].role == "a"
        assert templates[0].steps[1].role == "b"
        assert templates[0].steps[2].role == "c"

        # Second template: x->y
        assert templates[1].name == "x->y"
        assert len(templates[1].steps) == 2
        assert templates[1].steps[0].role == "x"
        assert templates[1].steps[1].role == "y"
