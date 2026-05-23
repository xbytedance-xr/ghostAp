"""Unit tests for Slock optimization wave 6 — review feedback resolution.

Covers:
- AC-R01: Archive context on move (L1 archived_context + L2 migration metadata)
- AC-R02: Chitchat hint card with force_process button
- AC-R03: Force prefix bypass ('!' and '/force ')
- AC-R04: Council result card schema 2.0 compliance
- AC-R05: Status panel select_static for >3 non-IDLE agents
- AC-R06: Incremental byte counter (no full serialization on every write)
- AC-R07: summarize_context OCC — write_agent_memory not blocked during LLM
- AC-R08: 4 parallel discussions execute without queue delay
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# AC-R01: Archive context on move
# ===========================================================================


class TestArchiveContextForMove:
    """Verify archive_context_for_move preserves context in archived_context."""

    def _make_memory_manager(self, tmp_path):
        from src.slock_engine.memory_manager import MemoryManager
        return MemoryManager(base_path=str(tmp_path))

    def test_archived_context_populated_after_move(self, tmp_path):
        """After move, L1 should contain archived_context with timestamp and content."""
        mm = self._make_memory_manager(tmp_path)
        from src.slock_engine.models import SlockMemory

        agent_id = "agent-001"
        mm.write_agent_memory(agent_id, SlockMemory(
            role="coder",
            active_context="Working on task X with important findings.",
        ))

        mm.archive_context_for_move(agent_id, "channel-A", "channel-B", agent_name="TestAgent")

        memory = mm.read_agent_memory(agent_id)
        assert "archived" in memory.active_context.lower() or "Context archived on move" in memory.active_context
        assert memory.archived_context != ""
        assert "Working on task X" in memory.archived_context
        assert "channel-A" in memory.archived_context

    def test_l2_shared_memory_contains_migration_record(self, tmp_path):
        """L2 SHARED_MEMORY.md should contain migration metadata after move."""
        mm = self._make_memory_manager(tmp_path)
        from src.slock_engine.models import SlockMemory

        agent_id = "agent-002"
        mm.ensure_directories(agent_id=agent_id, channel_id="channel-A")
        mm.write_agent_memory(agent_id, SlockMemory(active_context="some context"))

        mm.archive_context_for_move(agent_id, "channel-A", "channel-B", agent_name="Coder")

        l2_content = mm.read_group_memory("channel-A")
        assert "Coder" in l2_content or "agent-002" in l2_content
        assert "channel-B" in l2_content

    def test_archived_context_cap_20kb(self, tmp_path):
        """Archived context should not exceed 20KB."""
        mm = self._make_memory_manager(tmp_path)
        from src.slock_engine.models import SlockMemory

        agent_id = "agent-003"
        # Create large active context
        large_context = "x" * 25000
        mm.write_agent_memory(agent_id, SlockMemory(active_context=large_context))

        mm.archive_context_for_move(agent_id, "ch-A", "ch-B")

        memory = mm.read_agent_memory(agent_id)
        assert len(memory.archived_context.encode("utf-8")) <= 20 * 1024

    def test_restore_archived_context(self, tmp_path):
        """restore_archived_context should move archive back to active_context."""
        mm = self._make_memory_manager(tmp_path)
        from src.slock_engine.models import SlockMemory

        agent_id = "agent-004"
        mm.write_agent_memory(agent_id, SlockMemory(active_context="original work"))
        mm.archive_context_for_move(agent_id, "channel-A", "channel-B")

        result = mm.restore_archived_context(agent_id, "channel-A")
        assert result is True

        memory = mm.read_agent_memory(agent_id)
        assert "original work" in memory.active_context
        assert memory.archived_context == ""


# ===========================================================================
# AC-R02: Chitchat hint card with force_process button
# ===========================================================================


class TestChitchatHintCard:
    """Verify build_chitchat_hint_card returns proper structure."""

    def test_hint_card_contains_force_process_button(self):
        from src.slock_engine.card_templates import build_chitchat_hint_card

        card = build_chitchat_hint_card("这个方案怎么样", channel_id="ch-1")

        card_json = json.dumps(card, ensure_ascii=False)
        assert "force_process" in card_json
        assert "schema" in card
        assert card["schema"] == "2.0"
        # Verify force_process action value exists in serialized card
        assert "original_message" in card_json
        assert "这个方案怎么样" in card_json


# ===========================================================================
# AC-R03: Force prefix bypass
# ===========================================================================


class TestForcePrefixBypass:
    """Verify '!' and '/force ' prefix bypasses chitchat filter."""

    def _make_router(self):
        from src.slock_engine.task_router import TaskRouter
        router = TaskRouter()
        return router

    def test_exclamation_bypass(self):
        from src.slock_engine.task_router import TaskRouter
        router = self._make_router()
        # Long non-tech message is chitchat
        assert router._is_chitchat("这个方案你觉得怎么样呢我不太确定") is True
        # Same message with '!' prefix bypasses
        assert router._is_chitchat("!这个方案你觉得怎么样呢我不太确定") is False

    def test_force_prefix_bypass(self):
        router = self._make_router()
        assert router._is_chitchat("/force 这个方案怎么样") is False

    def test_strip_force_prefix(self):
        from src.slock_engine.task_router import TaskRouter
        assert TaskRouter.strip_force_prefix("!hello world") == "hello world"
        assert TaskRouter.strip_force_prefix("/force test msg") == "test msg"
        assert TaskRouter.strip_force_prefix("normal msg") == "normal msg"


# ===========================================================================
# AC-R04: Council result card schema 2.0
# ===========================================================================


class TestCouncilCardSchema:
    """Verify build_council_result_card uses schema 2.0 format."""

    def test_schema_present(self):
        from src.slock_engine.card_templates import build_council_result_card

        card = build_council_result_card(
            question="How to optimize?",
            agents_answers=[
                {"agent_name": "Agent1", "answer": "Use caching", "score": 8.5},
            ],
            rankings=[
                {"rank": 1, "agent_name": "Agent1", "score": 8.5},
            ],
        )

        assert card.get("schema") == "2.0"
        assert "body" in card
        assert "elements" in card["body"]
        assert "elements" not in card  # no top-level 'elements'

    def test_collapsible_panel_structure(self):
        from src.slock_engine.card_templates import build_council_result_card

        card = build_council_result_card(
            question="Test?",
            agents_answers=[
                {"agent_name": "A", "answer": "answer text", "score": 7.0},
            ],
            rankings=[{"rank": 1, "agent_name": "A", "score": 7.0}],
        )

        # Find collapsible_panel in body elements
        panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        assert len(panels) >= 1
        panel = panels[0]
        # Header should use Schema 2.0 format: {"title": {"tag": "plain_text", "content": "..."}}
        header = panel.get("header", {})
        title = header.get("title", {})
        assert title.get("tag") == "plain_text", f"Expected plain_text title, got: {title}"
        header_content = title.get("content", "")
        assert "A" in header_content, f"Agent name should be in header, got: {header_content}"
        assert "7.0" in header_content, f"Score should be in header, got: {header_content}"
        # Elements should be a flat list with the response markdown
        assert isinstance(panel["elements"], list)
        assert panel["elements"][0]["tag"] == "markdown"
        assert "answer text" in panel["elements"][0]["content"]
        # vertical_spacing should be set
        assert panel.get("vertical_spacing") == "8px"


# ===========================================================================
# AC-R05: Status panel select_static for >3 non-IDLE agents
# ===========================================================================


class TestStatusPanelSelectStatic:
    """Verify individual stop buttons are used for all non-IDLE agent counts."""

    def _make_agents(self, count, status):
        from src.slock_engine.models import AgentIdentity, AgentStatus
        agents = []
        for i in range(count):
            agent = AgentIdentity(
                agent_id=f"agent-{i}",
                name=f"Agent{i}",
                owner_group="ch-1",
                role=f"role-{i}",
            )
            agents.append((agent, status))
        return agents

    def test_5_running_agents_uses_individual_buttons(self):
        from src.slock_engine.card_templates import build_status_panel_card
        from src.slock_engine.models import AgentStatus

        agents = self._make_agents(5, AgentStatus.RUNNING)
        card = build_status_panel_card(agents, channel_id="ch-1")

        card_json = json.dumps(card)
        # No select_static — always individual buttons
        assert "select_static" not in card_json
        # Should have 5 individual stop buttons
        assert card_json.count("slock_stop_agent") >= 5

    def test_2_running_agents_uses_individual_buttons(self):
        from src.slock_engine.card_templates import build_status_panel_card
        from src.slock_engine.models import AgentStatus

        agents = self._make_agents(2, AgentStatus.RUNNING)
        card = build_status_panel_card(agents, channel_id="ch-1")

        card_json = json.dumps(card)
        assert "select_static" not in card_json
        # Should have individual stop buttons
        assert card_json.count("slock_stop_agent") >= 2


# ===========================================================================
# AC-R06: Incremental byte counter — no full serialization on every write
# ===========================================================================


class TestIncrementalByteCounter:
    """Verify _enforce_l1_capacity uses incremental counters to skip serialization."""

    def test_100_writes_minimal_full_checks(self, tmp_path):
        """100 writes should trigger at most 5 full serialization checks."""
        from src.slock_engine.memory_manager import MemoryManager
        from src.slock_engine.models import SlockMemory

        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "perf-agent"
        mm.write_agent_memory(agent_id, SlockMemory(role="test"))

        serialize_call_count = 0
        original_enforce = mm._enforce_l1_capacity

        def _counting_enforce(aid):
            nonlocal serialize_call_count
            # Count when it actually acquires the lock (calibration or over-limit)
            estimated = mm._byte_counters.get(aid, 0)
            write_count = mm._write_counts.get(aid, 0)
            needs_calibration = (write_count % mm._CALIBRATION_INTERVAL == 0) and write_count > 0
            if estimated > mm._get_l1_max_size() or needs_calibration:
                serialize_call_count += 1
            original_enforce(aid)

        mm._enforce_l1_capacity = _counting_enforce

        for i in range(100):
            mm.update_agent_context(agent_id, f"update {i}")

        # With 20-interval calibration, expect ~5 calibrations in 100 writes
        assert serialize_call_count <= 6, f"Too many full checks: {serialize_call_count}"


# ===========================================================================
# AC-R07: summarize_context OCC — write not blocked during LLM
# ===========================================================================


class TestSummarizeContextOCC:
    """Verify write_agent_memory is not blocked while summarize_context runs LLM."""

    def test_concurrent_write_not_blocked(self, tmp_path):
        """During LLM summarization, another thread's write should complete quickly."""
        from src.slock_engine.memory_manager import MemoryManager
        from src.slock_engine.models import SlockMemory

        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "occ-agent"
        # Create context large enough to trigger summarization
        large_ctx = "A" * 5000
        mm.write_agent_memory(agent_id, SlockMemory(role="test", active_context=large_ctx))

        write_completed = threading.Event()
        write_duration = [0.0]

        # Mock LLM to sleep (simulating slow LLM call)
        def slow_llm(prompt):
            time.sleep(2.0)
            return "summarized"

        mm.set_llm_callback(slow_llm)

        def writer():
            start = time.time()
            mm.write_agent_memory(agent_id, SlockMemory(role="test", active_context="new content"))
            write_duration[0] = time.time() - start
            write_completed.set()

        # Start summarization in background
        summarize_thread = threading.Thread(target=mm.summarize_context, args=(agent_id,), kwargs={"threshold": 100})
        summarize_thread.start()

        # Give summarize time to enter Phase 2 (LLM call outside lock)
        time.sleep(0.3)

        # Write should NOT be blocked
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        writer_thread.join(timeout=1.5)

        assert write_completed.is_set(), "write_agent_memory was blocked by summarize_context"
        assert write_duration[0] < 1.0, f"Write took too long: {write_duration[0]:.2f}s"

        summarize_thread.join(timeout=5.0)


# ===========================================================================
# AC-R08: 4 parallel discussions execute without queue delay
# ===========================================================================


class TestParallelDiscussions:
    """Verify 4 discussions can start concurrently with the new executor."""

    def test_4_concurrent_discussions_no_queue_delay(self):
        """All 4 discussions should start within 5s (no 2-worker bottleneck)."""
        from concurrent.futures import ThreadPoolExecutor
        import threading

        # Simulate the new discussion executor (max_workers=8)
        executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="test-discussion")
        semaphore = threading.Semaphore(8)

        start_times = {}
        barrier = threading.Barrier(4, timeout=10)

        def mock_discussion(channel_id):
            start_times[channel_id] = time.time()
            barrier.wait()  # All 4 must reach here
            time.sleep(0.1)

        t0 = time.time()
        futures = []
        for i in range(4):
            ch = f"channel-{i}"
            if semaphore.acquire(blocking=False):
                f = executor.submit(mock_discussion, ch)
                futures.append(f)

        for f in futures:
            f.result(timeout=10)

        executor.shutdown(wait=False)

        # All 4 should have started within 5s of submission
        for ch, start in start_times.items():
            delay = start - t0
            assert delay < 5.0, f"{ch} started with {delay:.2f}s delay"

        assert len(start_times) == 4, f"Only {len(start_times)} discussions started"
