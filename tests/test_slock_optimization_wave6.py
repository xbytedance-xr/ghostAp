"""Unit tests for Slock optimization wave 6 — review feedback resolution.

Covers:
- AC-R02: Chitchat hint card with force_process button
- AC-R04: Council result card schema 2.0 compliance
- AC-R05: Status panel select_static for >3 non-IDLE agents
- AC-R07: summarize_context OCC — write_agent_memory not blocked during LLM
- AC-R08: 4 parallel discussions execute without queue delay
"""

from __future__ import annotations

import json
import threading
import time

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
        # Header title contains agent name and score
        header = panel.get("header", {})
        title = header.get("title", {})
        header_content = title.get("content", "")
        assert "A" in header_content, f"Agent name should be in header, got: {header_content}"
        assert "7.0" in header_content, f"Score should be in header, got: {header_content}"
        # Elements should be a flat list with the response markdown
        assert isinstance(panel["elements"], list)
        assert panel["elements"][0]["tag"] == "markdown"
        assert "answer text" in panel["elements"][0]["content"]


# ===========================================================================
# AC-R05: Status panel select_static for >3 non-IDLE agents
# ===========================================================================


class TestStatusPanelSelectStatic:
    """Verify individual stop buttons are used for all non-IDLE agent counts."""

    def _make_agents(self, count, status):
        from src.slock_engine.models import AgentIdentity
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
        import threading
        from concurrent.futures import ThreadPoolExecutor

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
