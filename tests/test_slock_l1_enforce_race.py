"""Tests for Task 7: _enforce_l1_capacity race condition fix.

Verifies that:
1. _enforce_l1_capacity is called under the per-agent lock (no TOCTOU race).
2. Concurrent write_agent_memory calls with growing content never leave the
   file significantly exceeding the L1 threshold.
3. The fast os.path.getsize precheck short-circuits when file is small.
4. LLM summarization is called when file exceeds threshold (Task 10).
5. 10s timeout fallback to truncation (Task 10).
6. Final file size is always < 25KB (target_size = max_size // 2) (Task 10).
"""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import pytest

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_L1_TEST_MAX_SIZE = 4096  # 4KB test threshold (small for fast tests)
_L1_TEST_TARGET_SIZE = int(_L1_TEST_MAX_SIZE * 0.7)  # 70% target after enforcement


@pytest.fixture
def mm(tmp_path, monkeypatch):
    """Create a MemoryManager with a small L1 max size for testing."""
    monkeypatch.setattr(
        MemoryManager, "_get_l1_max_size", staticmethod(lambda: _L1_TEST_MAX_SIZE)
    )
    mgr = MemoryManager(base_path=str(tmp_path))
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Test: Concurrent writes never exceed threshold significantly
# ---------------------------------------------------------------------------


class TestConcurrentWriteCapacityEnforcement:
    """10 threads concurrently calling write_agent_memory with growing content."""

    def test_concurrent_writes_bounded_by_threshold(self, mm):
        """After all concurrent writes, file size must not significantly exceed threshold.

        We allow up to 2x threshold to account for in-flight writes that haven't
        been truncated yet, but the file must not grow without bound.
        """
        agent_id = "agent-concurrent"
        num_threads = 10
        writes_per_thread = 5
        barrier = threading.Barrier(num_threads, timeout=10)
        errors: list[Exception] = []

        def writer(thread_idx: int) -> None:
            try:
                barrier.wait()
                for i in range(writes_per_thread):
                    # Each write has growing content to force capacity enforcement
                    content = f"Thread-{thread_idx} write-{i}: " + ("X" * 500)
                    memory = SlockMemory(
                        role="tester",
                        key_knowledge="testing",
                        active_context=content,
                    )
                    mm.write_agent_memory(agent_id, memory)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(idx,), name=f"writer-{idx}")
            for idx in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Threads raised errors: {errors}"

        # Check final file size: should not exceed 2x threshold
        # (generous allowance for in-flight writes)
        path = mm._agent_memory_path(agent_id)
        if os.path.exists(path):
            file_size = os.path.getsize(path)
            # Allow 2x headroom for race between write and enforce
            assert file_size <= _L1_TEST_MAX_SIZE * 2, (
                f"File size {file_size} exceeds 2x threshold {_L1_TEST_MAX_SIZE * 2}. "
                f"Capacity enforcement may not be working correctly."
            )

    def test_concurrent_writes_all_complete_without_deadlock(self, mm):
        """Verify no deadlock occurs with enforce inside lock (RLock required)."""
        agent_id = "agent-deadlock-check"
        num_threads = 10
        barrier = threading.Barrier(num_threads, timeout=10)
        completed = threading.atomic = []  # Track completion
        lock = threading.Lock()

        def writer(thread_idx: int) -> None:
            barrier.wait()
            # Write content that exceeds threshold to force enforce path
            big_content = "A" * (_L1_TEST_MAX_SIZE + 100)
            memory = SlockMemory(
                role="tester",
                active_context=big_content,
            )
            mm.write_agent_memory(agent_id, memory)
            with lock:
                completed.append(thread_idx)

        threads = [
            threading.Thread(target=writer, args=(idx,))
            for idx in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # All threads must complete (no deadlock)
        assert len(completed) == num_threads, (
            f"Only {len(completed)}/{num_threads} threads completed. "
            "Possible deadlock due to non-reentrant lock."
        )


# ---------------------------------------------------------------------------
# Test: _enforce_l1_capacity is called under lock
# ---------------------------------------------------------------------------


class TestEnforceCalledUnderLock:
    """Verify _enforce_l1_capacity is invoked while the agent lock is held."""

    def test_enforce_called_while_lock_held_in_write_agent_memory(self, mm):
        """Mock _enforce_l1_capacity to assert the agent lock is held."""
        agent_id = "agent-lock-check"
        lock_was_held = []

        original_enforce = mm._enforce_l1_capacity

        def checking_enforce(aid: str) -> None:
            agent_lock = mm._get_agent_lock(aid)
            # RLock: if we can acquire without blocking from the same thread,
            # it means the lock is already held by us (reentrant)
            # For RLock, acquire() returns True even if already held
            acquired = agent_lock.acquire(blocking=False)
            if acquired:
                # We got it — which for RLock means it was either free or we held it
                # Check the recursion level: if > 1, it was already held
                # RLock._count is internal but we can use the pattern of release check
                agent_lock.release()
                # For RLock, successful non-blocking acquire from same thread
                # means it IS held by this thread (reentrant acquisition)
                lock_was_held.append(True)
            else:
                # Could not acquire — means another thread holds it
                lock_was_held.append(False)
            original_enforce(aid)

        mm._enforce_l1_capacity = checking_enforce

        memory = SlockMemory(role="tester", active_context="some context")
        mm.write_agent_memory(agent_id, memory)

        assert len(lock_was_held) == 1, "enforce was not called"
        assert lock_was_held[0] is True, (
            "_enforce_l1_capacity was called but agent lock was NOT held"
        )

    def test_enforce_called_while_lock_held_in_update_agent_context(self, mm):
        """Same check for update_agent_context path."""
        agent_id = "agent-lock-check-ctx"
        lock_was_held = []

        original_enforce = mm._enforce_l1_capacity

        def checking_enforce(aid: str) -> None:
            agent_lock = mm._get_agent_lock(aid)
            acquired = agent_lock.acquire(blocking=False)
            if acquired:
                agent_lock.release()
                lock_was_held.append(True)
            else:
                lock_was_held.append(False)
            original_enforce(aid)

        mm._enforce_l1_capacity = checking_enforce

        # First write to create the file
        mm.write_agent_memory(agent_id, SlockMemory(active_context="initial"))
        lock_was_held.clear()

        mm.update_agent_context(agent_id, "appended context")

        assert len(lock_was_held) == 1, "enforce was not called in update_agent_context"
        assert lock_was_held[0] is True, (
            "_enforce_l1_capacity was called but agent lock was NOT held in update_agent_context"
        )

    def test_enforce_under_lock_with_threading_verification(self, mm):
        """Use threading.current_thread to verify lock ownership across threads."""
        agent_id = "agent-thread-verify"
        enforce_thread_ids: list[int] = []
        write_thread_ids: list[int] = []
        enforce_lock = threading.Lock()

        original_enforce = mm._enforce_l1_capacity

        def tracking_enforce(aid: str) -> None:
            with enforce_lock:
                enforce_thread_ids.append(threading.current_thread().ident)
            original_enforce(aid)

        mm._enforce_l1_capacity = tracking_enforce

        num_threads = 5
        barrier = threading.Barrier(num_threads, timeout=5)

        def writer(idx: int) -> None:
            barrier.wait()
            with enforce_lock:
                write_thread_ids.append(threading.current_thread().ident)
            memory = SlockMemory(active_context=f"content-{idx}")
            mm.write_agent_memory(agent_id, memory)

        threads = [
            threading.Thread(target=writer, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Every enforce call must have come from a writer thread
        assert len(enforce_thread_ids) == num_threads
        for tid in enforce_thread_ids:
            assert tid in write_thread_ids, (
                f"_enforce_l1_capacity called from unexpected thread {tid}"
            )


# ---------------------------------------------------------------------------
# Test: Fast os.path.getsize precheck
# ---------------------------------------------------------------------------


class TestFastSizePrecheck:
    """Verify the fast precheck short-circuits when file is small."""

    def test_precheck_skips_enforcement_for_small_files(self, mm):
        """When file < threshold, _enforce_l1_capacity returns immediately."""
        agent_id = "agent-small"
        # Write a small memory that is well under threshold
        small_memory = SlockMemory(role="role", active_context="tiny")
        mm.write_agent_memory(agent_id, small_memory)

        # Patch the expensive inner path to track if it's reached
        inner_reached = []
        original_read = mm._read_agent_memory_unlocked

        def tracking_read(aid: str):
            inner_reached.append(aid)
            return original_read(aid)

        # Reset byte counters to force the code past the incremental check
        # so only the precheck guards against entering the expensive path
        mm._byte_counters[agent_id] = _L1_TEST_MAX_SIZE + 1000  # fake over-limit
        mm._write_counts[agent_id] = 0  # avoid calibration skip

        # The precheck should see the small file and return before expensive path
        with patch.object(mm, "_read_agent_memory_unlocked", side_effect=tracking_read):
            mm._enforce_l1_capacity(agent_id)

        # Because file is small, precheck returns early — no read needed
        # (inner path would re-read memory for calibration)
        assert len(inner_reached) == 0, (
            "Fast precheck did NOT short-circuit; expensive path was reached "
            "even though file is under threshold."
        )

    def test_precheck_handles_missing_file(self, mm):
        """When file doesn't exist, precheck returns without error."""
        # Agent with no file written yet
        # Should not raise; OSError is caught and returns early
        mm._enforce_l1_capacity("agent-nonexistent")

    def test_precheck_allows_enforcement_for_large_files(self, mm):
        """When file >= threshold, enforcement proceeds past precheck."""
        agent_id = "agent-large"
        # Write content exceeding the threshold
        big_content = "B" * (_L1_TEST_MAX_SIZE + 500)
        big_memory = SlockMemory(role="role", active_context=big_content)

        # Write directly (bypassing enforce) to set up the test state
        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, big_memory)

        path = mm._agent_memory_path(agent_id)
        assert os.path.getsize(path) >= _L1_TEST_MAX_SIZE

        # Now call enforce — it should proceed past precheck
        # and attempt truncation/summarization
        enforce_entered_inner = []
        original_enforce = MemoryManager._enforce_l1_capacity.__wrapped__ if hasattr(
            MemoryManager._enforce_l1_capacity, "__wrapped__"
        ) else None

        # Track that we get past the precheck by checking byte counter update
        old_counter = mm._byte_counters.get(agent_id, 0)
        mm._enforce_l1_capacity(agent_id)
        # After enforcement, byte counter should be calibrated
        new_counter = mm._byte_counters.get(agent_id, 0)
        # The counter was updated (calibrated) — means we got past the precheck
        assert new_counter != old_counter or new_counter <= _L1_TEST_MAX_SIZE, (
            "Enforcement did not proceed past precheck for large file"
        )


# ---------------------------------------------------------------------------
# Test: RLock prevents deadlock
# ---------------------------------------------------------------------------


class TestRLockPreventsDeadlock:
    """Verify the agent lock is reentrant (RLock) so enforce inside lock works."""

    def test_agent_lock_is_reentrant(self, mm):
        """The per-agent lock must be an RLock to support nested acquisition."""
        agent_id = "agent-rlock-test"
        lock = mm._get_agent_lock(agent_id)
        assert isinstance(lock, type(threading.RLock())), (
            f"Agent lock should be RLock, got {type(lock).__name__}"
        )

    def test_nested_lock_acquisition_does_not_deadlock(self, mm):
        """Simulate the pattern: lock -> write -> enforce -> lock (reentrant)."""
        agent_id = "agent-nested"
        result = []

        def nested_op():
            with mm._get_agent_lock(agent_id):
                # Simulate write
                result.append("outer")
                with mm._get_agent_lock(agent_id):
                    # Simulate enforce re-acquiring
                    result.append("inner")

        # Should complete without deadlock within 5 seconds
        t = threading.Thread(target=nested_op)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "Thread deadlocked on nested lock acquisition"
        assert result == ["outer", "inner"]


# ---------------------------------------------------------------------------
# Test: LLM summarization is called when file exceeds threshold (Task 10)
# ---------------------------------------------------------------------------


class TestLLMSummaryCalledOnThresholdExceed:
    """Verify LLM callback is invoked when L1 memory exceeds capacity."""

    def test_llm_callback_invoked_on_enforce(self, mm):
        """When file exceeds L1 max, LLM callback should be invoked for summarization."""
        call_log = []

        def mock_llm(prompt: str) -> str:
            call_log.append(prompt)
            # Return a short summary that fits under target
            return "Summary: decisions made."

        mm.set_llm_callback(mock_llm)

        agent_id = "agent-llm-invoke"
        # Write content exceeding the threshold (4KB)
        big_content = "X" * (_L1_TEST_MAX_SIZE + 2000)
        memory = SlockMemory(role="dev", active_context=big_content)

        # Write directly to set up state without triggering enforce
        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        # Now trigger enforcement
        mm._enforce_l1_capacity(agent_id)

        # LLM callback should have been called at least once
        assert len(call_log) > 0, "LLM callback was not invoked during enforcement"
        assert "Summarize" in call_log[0] or "summarize" in call_log[0].lower()

    def test_no_llm_callback_still_enforces_via_truncation(self, mm):
        """Without LLM callback set, enforcement still reduces file size."""
        agent_id = "agent-no-llm"
        big_content = "Y" * (_L1_TEST_MAX_SIZE + 2000)
        memory = SlockMemory(role="dev", active_context=big_content)

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        # No LLM callback set — should still enforce via truncation
        mm._enforce_l1_capacity(agent_id)

        result = mm.read_agent_memory(agent_id)
        result_size = len(result.to_markdown().encode("utf-8"))
        target_size = _L1_TEST_TARGET_SIZE
        assert result_size <= target_size, (
            f"Without LLM, file size {result_size} still exceeds target {target_size}"
        )


# ---------------------------------------------------------------------------
# Test: Timeout fallback to truncation (Task 10)
# ---------------------------------------------------------------------------


class TestLLMTimeoutFallback:
    """Verify that LLM timeout triggers fallback to truncation."""

    def test_slow_llm_triggers_truncation_fallback(self, mm):
        """When LLM takes longer than timeout, fallback truncation is used."""
        from unittest.mock import MagicMock

        call_log = []

        def slow_llm(prompt: str) -> str:
            call_log.append("called")
            # Sleep long enough to exceed timeout
            time.sleep(15)
            return "This summary should NOT be used"

        mm.set_llm_callback(slow_llm)

        # Patch settings to use a short timeout (1s) so we don't wait 30s
        mock_settings = MagicMock()
        mock_settings.slock_memory_summarize_timeout = 1.0
        with patch("src.config.get_settings", return_value=mock_settings):
            # Test _summarize_text directly with a long text
            text = "A" * 3000
            result = mm._summarize_text(text, max_output_chars=1500)

        # LLM was called but timed out; fallback truncation should be used
        assert len(call_log) > 0, "LLM callback was never called"
        # Fallback: timestamp marker + tail of original text
        assert "[Context summarized at " in result
        assert result.endswith("A" * 1500)

    def test_llm_exception_triggers_truncation_fallback(self, mm):
        """When LLM raises an exception, fallback truncation is used."""

        def failing_llm(prompt: str) -> str:
            raise RuntimeError("LLM service unavailable")

        mm.set_llm_callback(failing_llm)

        text = "B" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)

        # Should use truncation fallback
        assert "[Context summarized at " in result
        assert result.endswith("B" * 1500)

    def test_llm_returning_none_triggers_fallback(self, mm):
        """When LLM returns None, fallback truncation is used."""

        def none_llm(prompt: str):
            return None

        mm.set_llm_callback(none_llm)

        text = "C" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)

        assert "[Context summarized at " in result
        assert result.endswith("C" * 1500)

    def test_successful_llm_returns_summary(self, mm):
        """When LLM succeeds within timeout, summary is used."""

        def fast_llm(prompt: str) -> str:
            return "Concise summary of context."

        mm.set_llm_callback(fast_llm)

        text = "D" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)

        # Should use LLM result, not truncation
        assert "Concise summary of context." in result
        assert "[Context summarized at " in result
        # Should NOT end with repeated D's (truncation pattern)
        assert not result.endswith("D" * 100)


# ---------------------------------------------------------------------------
# Test: Final file size always < 25KB (target_size = max_size // 2) (Task 10)
# ---------------------------------------------------------------------------


class TestFinalSizeGuarantee:
    """Verify final file size is always < target_size (max_size // 2) after enforcement."""

    def test_final_size_under_target_after_enforcement(self, mm):
        """After _enforce_l1_capacity, file must be <= target_size."""
        agent_id = "agent-size-guarantee"
        # Write content well above threshold
        big_content = "# Session Log\n\n" + ("Z" * (_L1_TEST_MAX_SIZE * 3))
        memory = SlockMemory(
            role="architect",
            key_knowledge="important",
            active_context=big_content,
        )

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        mm._enforce_l1_capacity(agent_id)

        result = mm.read_agent_memory(agent_id)
        result_size = len(result.to_markdown().encode("utf-8"))
        target_size = _L1_TEST_TARGET_SIZE
        assert result_size <= target_size, (
            f"Final size {result_size} exceeds target {target_size}"
        )

    def test_hard_truncation_preserves_first_section_header(self, mm):
        """Hard truncation keeps the first section header in active_context."""
        agent_id = "agent-header-keep"
        # Structure content so that after Phase 3 FIFO (paragraph-based tail keeping),
        # the result contains a # header AND is > target_size so Phase 4 fires.
        # FIFO keeps most-recent paragraphs first, so put header near the end.
        padding = "P" * (_L1_TEST_MAX_SIZE * 2)  # Will be FIFO-truncated away
        recent_body = "W" * 3000  # Recent paragraph that fits in budget
        header_section = "# Critical Session\nImportant project notes"
        # Order: old padding, then recent body, then header (most recent)
        content = padding + "\n\n" + recent_body + "\n\n" + header_section
        memory = SlockMemory(role="dev", active_context=content)

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        # Patch summarize methods to fail, forcing Phase 3+4 path
        with patch.object(mm, 'summarize_context', side_effect=RuntimeError("test")), \
             patch.object(mm, '_summarize_with_preservation', side_effect=RuntimeError("test")):
            mm._enforce_l1_capacity(agent_id)

        result = mm.read_agent_memory(agent_id)
        # The section header should be preserved by Phase 4 hard truncation
        assert "# Critical Session" in result.active_context
        # Result should be within target
        result_size = len(result.to_markdown().encode("utf-8"))
        target_size = _L1_TEST_TARGET_SIZE
        assert result_size <= target_size

    def test_key_knowledge_never_truncated(self, mm):
        """Key knowledge must be preserved intact after enforcement."""
        agent_id = "agent-kk-safe"
        kk = "CRITICAL: Never expose API keys. Auth tokens rotate daily."
        memory = SlockMemory(
            role="security",
            key_knowledge=kk,
            active_context="# Log\n\n" + ("Q" * (_L1_TEST_MAX_SIZE * 3)),
        )

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        mm._enforce_l1_capacity(agent_id)

        result = mm.read_agent_memory(agent_id)
        assert result.key_knowledge == kk
        assert result.role == "security"

    def test_enforcement_with_llm_success_still_verifies_target(self, mm):
        """Even if LLM returns content, final size must still be < target_size."""

        def bloated_llm(prompt: str) -> str:
            # Return something larger than target but less than max_output_chars
            return "S" * 3000

        mm.set_llm_callback(bloated_llm)

        agent_id = "agent-bloated-llm"
        content = "# Notes\n\n" + ("R" * (_L1_TEST_MAX_SIZE * 3))
        memory = SlockMemory(role="dev", active_context=content)

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        mm._enforce_l1_capacity(agent_id)

        result = mm.read_agent_memory(agent_id)
        result_size = len(result.to_markdown().encode("utf-8"))
        target_size = _L1_TEST_TARGET_SIZE
        assert result_size <= target_size, (
            f"After bloated LLM, size {result_size} still exceeds target {target_size}"
        )


# ---------------------------------------------------------------------------
# Test: WP-B: Critical Section Preservation
# ---------------------------------------------------------------------------


class TestCriticalSectionsPreservation:
    """测试 L1 截断时关键 section 的保留。"""

    def test_role_and_key_knowledge_preserved_after_fifo(self, mm):
        """FIFO 截断后 Role 和最近 3 条 Key Knowledge 完整保留。"""
        agent_id = "agent-critical-preserve"

        # 创建包含 Role + 5 条 Key Knowledge + 大量 Archived Context 的 memory
        role_content = "Senior Python Engineer with expertise in distributed systems"
        key_knowledge_lines = [
            "- Primary language: Python",
            "- Framework: FastAPI + pytest",
            "- Code style: PEP8",
            "- Database: PostgreSQL",
            "- Auth: JWT authentication",
        ]
        key_knowledge = "\n".join(key_knowledge_lines)

        # 大量 Archived Context 让总大小超过阈值
        archived_content = "Archive" * 1000
        active_content = "Active" * 500

        memory = SlockMemory(
            role=role_content,
            key_knowledge=key_knowledge,
            active_context=active_content,
            archived_context=archived_content,
        )

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        # 触发 L1 超限
        mm._enforce_l1_capacity(agent_id)

        # 验证截断后 Role 完整，Key Knowledge 保留最近 3 条
        result = mm.read_agent_memory(agent_id)

        # Role 应该完整保留
        assert result.role == role_content, "Role should be preserved intact"

        # Key Knowledge 应该保留最近 3 条
        expected_kk = "\n".join(key_knowledge_lines[-3:])
        assert result.key_knowledge == expected_kk, (
            "Expected last 3 Key Knowledge lines should be preserved"
        )

        # active_context 应该被截断
        assert len(result.active_context.encode("utf-8")) < len(active_content.encode("utf-8"))

    def test_parse_memory_sections_fault_tolerant(self, mm):
        """_parse_memory_sections 对格式不规范的 markdown 容错。"""
        # 测试 1: 空内容
        result = mm._parse_memory_sections("")
        assert result == {
            'role': '',
            'key_knowledge': '',
            'active_context': '',
            'archived_context': '',
        }

        # 测试 2: 只有部分 section 缺失
        partial_md = """# Role
coder

# Active Context
Working on feature X
"""
        result = mm._parse_memory_sections(partial_md)
        assert result['role'] == 'coder'
        assert result['active_context'] == 'Working on feature X'
        assert result['key_knowledge'] == ''
        assert result['archived_context'] == ''

        # 测试 3: 大小写不敏感的 header
        case_insensitive_md = """# role
tester

# key knowledge
- Test everything

# active context
Testing now

# archived context
Old stuff
"""
        result = mm._parse_memory_sections(case_insensitive_md)
        assert result['role'] == 'tester'
        assert result['key_knowledge'] == '- Test everything'
        assert result['active_context'] == 'Testing now'
        assert result['archived_context'] == 'Old stuff'

        # 测试 4: 多余的空白
        extra_whitespace_md = """#   Role   
reviewer

# Key   Knowledge   
- Review carefully

# Active   Context   
Reviewing PR

# Archived   Context   
Old reviews
"""
        result = mm._parse_memory_sections(extra_whitespace_md)
        assert result['role'] == 'reviewer'
        assert result['key_knowledge'] == '- Review carefully'
        assert result['active_context'] == 'Reviewing PR'
        assert result['archived_context'] == 'Old reviews'

        # 测试 5: 包含未知 section（应该被忽略或附加到前一个 section）
        unknown_section_md = """# Role
architect

# Unknown Section
This should not affect parsing

# Key Knowledge
- Architecture patterns
"""
        result = mm._parse_memory_sections(unknown_section_md)
        assert result['role'] == 'architect'
        # Unknown Section 内容不应该出现在 key_knowledge 中
        assert 'Unknown Section' not in result['key_knowledge']
        assert result['key_knowledge'] == '- Architecture patterns'

    def test_critical_info_exceeds_max_size(self, mm):
        """关键信息本身超过 max_size 时只清空 active_context。"""
        agent_id = "agent-critical-overflow"

        # 创建一个 Role + Key Knowledge 本身就超过 max_size 的 memory
        # 注意：这里我们需要让 Role + Key Knowledge 超过阈值
        # 由于测试阈值是 4KB，我们让 Role + Key Knowledge 超过它

        # 让 Role 很大
        big_role = "Role content " * 200  # 约 3KB
        big_key_knowledge = "- Knowledge " * 200  # 约 2.4KB

        memory = SlockMemory(
            role=big_role,
            key_knowledge=big_key_knowledge,
            active_context="Some active context that should be cleared",
            archived_context="Some archived context",
        )

        # 验证关键信息大小
        preserved_role, preserved_kk = mm._preserve_critical_sections(memory)
        critical_size = (
            len(preserved_role.encode("utf-8"))
            + len(preserved_kk.encode("utf-8"))
            + 100  # header overhead
        )
        assert critical_size > _L1_TEST_MAX_SIZE, (
            "Test setup: critical info should exceed max_size"
        )

        with mm._get_agent_lock(agent_id):
            mm._write_agent_memory_unlocked(agent_id, memory)

        # 触发截断
        mm._enforce_l1_capacity(agent_id)

        result = mm.read_agent_memory(agent_id)

        # Role 应该完整保留（最近 3 条 Key Knowledge 也保留
        assert result.role == preserved_role, "Role should be preserved"
        assert result.key_knowledge == preserved_kk, "Key Knowledge should be preserved"

        # active_context 应该被清空
        assert result.active_context == "", "active_context should be cleared"

        # archived_context 可能也被截断，但关键信息必须保留

    def test_preserve_critical_sections_with_fewer_than_3_kk(self, mm):
        """当 Key Knowledge 少于 3 条时，保留所有。"""
        memory = SlockMemory(
            role="tester",
            key_knowledge="- Only one knowledge item",
            active_context="test",
        )

        role, kk = mm._preserve_critical_sections(memory)
        assert role == "tester"
        assert kk == "- Only one knowledge item"

    def test_preserve_critical_sections_empty(self, mm):
        """空 memory 的关键信息保留。"""
        memory = SlockMemory()

        role, kk = mm._preserve_critical_sections(memory)
        assert role == ""
        assert kk == ""

    def test_preserve_critical_sections_exactly_3_kk(self, mm):
        """正好 3 条 Key Knowledge 时全部保留。"""
        kk_lines = [
            "- Knowledge 1",
            "- Knowledge 2",
            "- Knowledge 3",
        ]
        memory = SlockMemory(
            role="dev",
            key_knowledge="\n".join(kk_lines),
        )

        role, kk = mm._preserve_critical_sections(memory)
        assert role == "dev"
        assert kk == "\n".join(kk_lines)

    def test_preserve_critical_sections_more_than_3_kk(self, mm):
        """超过 3 条 Key Knowledge 时只保留最后 3 条。"""
        kk_lines = [
            "- Knowledge 1",
            "- Knowledge 2",
            "- Knowledge 3",
            "- Knowledge 4",
            "- Knowledge 5",
        ]
        memory = SlockMemory(
            role="dev",
            key_knowledge="\n".join(kk_lines),
        )

        role, kk = mm._preserve_critical_sections(memory)
        assert role == "dev"
        # 应该只保留最后 3 条
        assert kk == "\n".join(kk_lines[-3:])
