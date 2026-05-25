"""Unit tests for enhanced memory manager methods.

Covers:
- summarize_context (short text no-op, long text with .bak creation)
- _summarize_text (fallback truncation logic)
- read_conversation_replay (missing file, valid JSONL)
- read_group_memory_section (section parsing)
- append_group_memory_section (new section creation, existing section append)
- append_discussion_conclusion (timestamp formatting)
"""

from __future__ import annotations

import json
import os

import pytest

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory


@pytest.fixture()
def mm(tmp_path):
    """Create a MemoryManager rooted at a temporary directory."""
    return MemoryManager(base_path=str(tmp_path))


def _write_agent_memory(mm: MemoryManager, agent_id: str, memory: SlockMemory) -> None:
    """Helper to write agent memory for test setup."""
    mm.write_agent_memory(agent_id, memory)


def _write_jsonl(path: str, records: list[dict]) -> None:
    """Write a list of dicts as JSONL lines to a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class TestSummarizeText:
    """Tests for MemoryManager._summarize_text fallback truncation."""

    def test_short_text_returned_unchanged(self, mm: MemoryManager):
        text = "short text"
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result == text

    def test_text_at_boundary_returned_unchanged(self, mm: MemoryManager):
        text = "x" * 1500
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result == text

    def test_long_text_truncated_with_prefix(self, mm: MemoryManager):
        text = "A" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        # The tail portion should be the last 1500 chars of the original
        assert result.endswith("A" * 1500)

    def test_custom_max_output_chars(self, mm: MemoryManager):
        text = "B" * 500
        result = mm._summarize_text(text, max_output_chars=200)
        assert "[Context summarized at " in result
        assert result.endswith("B" * 200)


class TestSummarizeContext:
    """Tests for MemoryManager.summarize_context."""

    def test_short_context_no_op(self, mm: MemoryManager, tmp_path):
        agent_id = "agent_short"
        short_context = "x" * 100
        memory = SlockMemory(role="tester", active_context=short_context)
        _write_agent_memory(mm, agent_id, memory)

        result = mm.summarize_context(agent_id, threshold=4000)
        assert result is False

        # Memory should be unchanged
        after = mm.read_agent_memory(agent_id)
        assert after.active_context == short_context

    def test_context_at_threshold_no_op(self, mm: MemoryManager, tmp_path):
        agent_id = "agent_boundary"
        # Exactly at threshold should NOT trigger (<=)
        context = "y" * 4000
        memory = SlockMemory(role="coder", active_context=context)
        _write_agent_memory(mm, agent_id, memory)

        result = mm.summarize_context(agent_id, threshold=4000)
        assert result is False

    def test_long_context_triggers_summarization(self, mm: MemoryManager, tmp_path):
        agent_id = "agent_long"
        long_context = "Z" * 5000
        memory = SlockMemory(role="planner", key_knowledge="keep this", active_context=long_context)
        _write_agent_memory(mm, agent_id, memory)

        result = mm.summarize_context(agent_id, threshold=4000)
        assert result is True

        # Backup file should exist
        memory_path = mm.agent_memory_path(agent_id)
        bak_path = memory_path + ".bak"
        assert os.path.exists(bak_path)

        # Backup should contain original content
        with open(bak_path, "r", encoding="utf-8") as f:
            bak_content = f.read()
        assert long_context in bak_content

        # New memory should have compressed active_context
        after = mm.read_agent_memory(agent_id)
        assert "[Context summarized at " in after.active_context
        assert len(after.active_context) < len(long_context)

        # Role and key_knowledge preserved
        assert after.role == "planner"
        assert after.key_knowledge == "keep this"

    def test_custom_threshold(self, mm: MemoryManager, tmp_path):
        agent_id = "agent_custom"
        # Use text longer than default max_output_chars (1500) so truncation fires
        context = "W" * 2000
        memory = SlockMemory(active_context=context)
        _write_agent_memory(mm, agent_id, memory)

        # With threshold=100, 2000-char context should trigger summarization
        result = mm.summarize_context(agent_id, threshold=100)
        assert result is True

        after = mm.read_agent_memory(agent_id)
        assert "[Context summarized at " in after.active_context
        assert len(after.active_context) < len(context)


class TestReadConversationReplay:
    """Tests for MemoryManager.read_conversation_replay."""

    def test_missing_file_returns_empty_list(self, mm: MemoryManager):
        result = mm.read_conversation_replay("nonexistent_channel")
        assert result == []

    def test_empty_file_returns_empty_list(self, mm: MemoryManager, tmp_path):
        channel_id = "empty_channel"
        path = mm.message_archive_path(channel_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")

        result = mm.read_conversation_replay(channel_id)
        assert result == []

    def test_reads_last_n_rounds(self, mm: MemoryManager, tmp_path):
        channel_id = "chat_replay"
        records = []
        for i in range(20):
            sender = "user" if i % 2 == 0 else "agent"
            records.append({
                "sender_type": sender,
                "agent_name": "bot" if sender == "agent" else "",
                "content": f"message_{i}",
                "timestamp": 1000.0 + i,
                "channel_id": channel_id,
                "agent_id": "",
                "metadata": {},
            })

        path = mm.message_archive_path(channel_id)
        _write_jsonl(path, records)

        # n_rounds=3 -> last 6 entries (indices 14..19)
        result = mm.read_conversation_replay(channel_id, n_rounds=3)
        assert len(result) == 6
        assert result[0]["content"] == "message_14"
        assert result[-1]["content"] == "message_19"

    def test_fewer_entries_than_requested(self, mm: MemoryManager, tmp_path):
        channel_id = "short_chat"
        records = [
            {"sender_type": "user", "agent_name": "", "content": "hello", "timestamp": 1.0,
             "channel_id": channel_id, "agent_id": "", "metadata": {}},
            {"sender_type": "agent", "agent_name": "bot", "content": "hi", "timestamp": 2.0,
             "channel_id": channel_id, "agent_id": "", "metadata": {}},
        ]
        path = mm.message_archive_path(channel_id)
        _write_jsonl(path, records)

        # Requesting 5 rounds (10 entries) but only 2 exist
        result = mm.read_conversation_replay(channel_id, n_rounds=5)
        assert len(result) == 2
        assert result[0]["sender_type"] == "user"
        assert result[1]["sender_type"] == "agent"

    def test_malformed_lines_skipped(self, mm: MemoryManager, tmp_path):
        channel_id = "malformed_chat"
        path = mm.message_archive_path(channel_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not valid json\n")
            f.write(json.dumps({"sender_type": "user", "content": "ok", "timestamp": 1.0, "agent_name": ""}) + "\n")
            f.write("{broken\n")
            f.write(json.dumps({"sender_type": "agent", "content": "reply", "timestamp": 2.0, "agent_name": "b"}) + "\n")

        result = mm.read_conversation_replay(channel_id, n_rounds=5)
        assert len(result) == 2
        assert result[0]["content"] == "ok"
        assert result[1]["content"] == "reply"

    def test_result_dict_keys(self, mm: MemoryManager, tmp_path):
        channel_id = "keys_check"
        records = [
            {"sender_type": "user", "agent_name": "", "content": "test", "timestamp": 99.0,
             "channel_id": channel_id, "agent_id": "a1", "metadata": {"foo": "bar"}},
        ]
        path = mm.message_archive_path(channel_id)
        _write_jsonl(path, records)

        result = mm.read_conversation_replay(channel_id, n_rounds=1)
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {"sender_type", "agent_name", "content", "timestamp"}
        assert entry["sender_type"] == "user"
        assert entry["timestamp"] == 99.0


class TestReadGroupMemorySection:
    """Tests for MemoryManager.read_group_memory_section."""

    def test_empty_memory_returns_empty(self, mm: MemoryManager):
        result = mm.read_group_memory_section("no_such_channel", "Decisions")
        assert result == ""

    def test_section_not_found_returns_empty(self, mm: MemoryManager, tmp_path):
        channel_id = "ch1"
        mm.write_group_memory(channel_id, "# Other Section\nsome content\n")

        result = mm.read_group_memory_section(channel_id, "Decisions")
        assert result == ""

    def test_reads_single_section(self, mm: MemoryManager, tmp_path):
        channel_id = "ch2"
        content = "# Decisions\nWe decided to use Python.\nAlso use pytest.\n"
        mm.write_group_memory(channel_id, content)

        result = mm.read_group_memory_section(channel_id, "Decisions")
        assert "We decided to use Python." in result
        assert "Also use pytest." in result

    def test_reads_section_between_headers(self, mm: MemoryManager, tmp_path):
        channel_id = "ch3"
        content = (
            "# Decisions\n"
            "Decision A\n"
            "Decision B\n"
            "\n"
            "# Conventions\n"
            "Convention X\n"
        )
        mm.write_group_memory(channel_id, content)

        decisions = mm.read_group_memory_section(channel_id, "Decisions")
        assert "Decision A" in decisions
        assert "Decision B" in decisions
        assert "Convention X" not in decisions

        conventions = mm.read_group_memory_section(channel_id, "Conventions")
        assert "Convention X" in conventions
        assert "Decision A" not in conventions

    def test_section_at_end_of_file(self, mm: MemoryManager, tmp_path):
        channel_id = "ch4"
        content = (
            "# Blocking Issues\n"
            "Issue 1\n"
            "\n"
            "# Decisions\n"
            "Final decision here\n"
        )
        mm.write_group_memory(channel_id, content)

        result = mm.read_group_memory_section(channel_id, "Decisions")
        assert "Final decision here" in result


class TestAppendGroupMemorySection:
    """Tests for MemoryManager.append_group_memory_section."""

    def test_creates_new_section_in_empty_file(self, mm: MemoryManager, tmp_path):
        channel_id = "new_section_empty"
        mm.append_group_memory_section(channel_id, "Decisions", "We chose Go.")

        content = mm.read_group_memory(channel_id)
        assert "# Decisions" in content
        assert "We chose Go." in content

    def test_creates_new_section_when_not_existing(self, mm: MemoryManager, tmp_path):
        channel_id = "new_section"
        mm.write_group_memory(channel_id, "# Conventions\nUse snake_case.\n")

        mm.append_group_memory_section(channel_id, "Decisions", "Use microservices.")

        content = mm.read_group_memory(channel_id)
        assert "# Conventions" in content
        assert "# Decisions" in content
        assert "Use microservices." in content
        # Original content preserved
        assert "Use snake_case." in content

    def test_appends_to_existing_section(self, mm: MemoryManager, tmp_path):
        channel_id = "append_existing"
        mm.write_group_memory(channel_id, "# Decisions\nFirst decision.\n")

        mm.append_group_memory_section(channel_id, "Decisions", "Second decision.")

        content = mm.read_group_memory(channel_id)
        assert "First decision." in content
        assert "Second decision." in content

    def test_appends_between_sections(self, mm: MemoryManager, tmp_path):
        channel_id = "between_sections"
        initial = (
            "# Decisions\n"
            "Original.\n"
            "\n"
            "# Conventions\n"
            "Convention data.\n"
        )
        mm.write_group_memory(channel_id, initial)

        mm.append_group_memory_section(channel_id, "Decisions", "Added entry.")

        content = mm.read_group_memory(channel_id)
        assert "Original." in content
        assert "Added entry." in content
        assert "Convention data." in content

        # Verify section parsing still works
        decisions = mm.read_group_memory_section(channel_id, "Decisions")
        assert "Original." in decisions
        assert "Added entry." in decisions
        assert "Convention data." not in decisions

    def test_multiple_appends_accumulate(self, mm: MemoryManager, tmp_path):
        channel_id = "multi_append"
        mm.append_group_memory_section(channel_id, "Notes", "Note 1")
        mm.append_group_memory_section(channel_id, "Notes", "Note 2")
        mm.append_group_memory_section(channel_id, "Notes", "Note 3")

        section_content = mm.read_group_memory_section(channel_id, "Notes")
        assert "Note 1" in section_content
        assert "Note 2" in section_content
        assert "Note 3" in section_content


class TestAppendDiscussionConclusion:
    """Tests for MemoryManager.append_discussion_conclusion."""

    def test_default_section_is_decisions(self, mm: MemoryManager, tmp_path):
        channel_id = "conclusion_default"
        mm.append_discussion_conclusion(channel_id, "We will use REST API.")

        content = mm.read_group_memory(channel_id)
        assert "# Decisions" in content
        assert "Discussion Conclusion: We will use REST API." in content

    def test_custom_section(self, mm: MemoryManager, tmp_path):
        channel_id = "conclusion_custom"
        mm.append_discussion_conclusion(channel_id, "Blocked on auth.", section="Blocking Issues")

        content = mm.read_group_memory(channel_id)
        assert "# Blocking Issues" in content
        assert "Discussion Conclusion: Blocked on auth." in content

    def test_timestamp_format(self, mm: MemoryManager, tmp_path):
        channel_id = "conclusion_ts"
        mm.append_discussion_conclusion(channel_id, "Test conclusion.")

        content = mm.read_group_memory(channel_id)
        # Timestamp format: [YYYY-MM-DD HH:MM]
        import re
        match = re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", content)
        assert match is not None, f"Expected timestamp pattern not found in: {content}"

    def test_conclusion_appended_to_existing_section(self, mm: MemoryManager, tmp_path):
        channel_id = "conclusion_existing"
        mm.write_group_memory(channel_id, "# Decisions\nPrevious decision.\n")

        mm.append_discussion_conclusion(channel_id, "New conclusion reached.")

        content = mm.read_group_memory(channel_id)
        assert "Previous decision." in content
        assert "Discussion Conclusion: New conclusion reached." in content

    def test_multiple_conclusions(self, mm: MemoryManager, tmp_path):
        channel_id = "conclusion_multi"
        mm.append_discussion_conclusion(channel_id, "First conclusion.")
        mm.append_discussion_conclusion(channel_id, "Second conclusion.")

        section = mm.read_group_memory_section(channel_id, "Decisions")
        assert "First conclusion." in section
        assert "Second conclusion." in section


class TestLLMBasedSummarize:
    """Tests for enhanced _summarize_text with _llm_callback integration."""

    def test_no_callback_uses_truncation_fallback(self, mm: MemoryManager):
        """When _llm_callback is None, truncation fallback is used."""
        assert mm._llm_callback is None
        text = "A" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        assert result.endswith("A" * 1500)

    def test_valid_callback_response_used_with_prefix(self, mm: MemoryManager):
        """When _llm_callback returns a valid short response, it's used with timestamp prefix."""
        def mock_llm(prompt: str) -> str:
            return "Key facts: decided on REST API, action item: deploy by Friday."

        mm.set_llm_callback(mock_llm)
        text = "X" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        assert "Key facts: decided on REST API" in result

    def test_callback_returns_none_uses_truncation(self, mm: MemoryManager):
        """When _llm_callback returns None, truncation fallback is used."""
        def mock_llm(prompt: str) -> None:
            return None

        mm.set_llm_callback(mock_llm)
        text = "B" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        assert result.endswith("B" * 1500)

    def test_callback_returns_empty_string_uses_truncation(self, mm: MemoryManager):
        """When _llm_callback returns empty string, truncation fallback is used."""
        def mock_llm(prompt: str) -> str:
            return ""

        mm.set_llm_callback(mock_llm)
        text = "C" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        assert result.endswith("C" * 1500)

    def test_callback_returns_too_long_response_uses_truncation(self, mm: MemoryManager):
        """When _llm_callback returns response longer than max_output_chars, truncation fallback is used."""
        def mock_llm(prompt: str) -> str:
            return "Z" * 2000  # exceeds max_output_chars=1500

        mm.set_llm_callback(mock_llm)
        text = "D" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        assert result.endswith("D" * 1500)

    def test_callback_raises_exception_uses_truncation(self, mm: MemoryManager):
        """When _llm_callback raises an exception, truncation fallback is used."""
        def mock_llm(prompt: str) -> str:
            raise RuntimeError("LLM service unavailable")

        mm.set_llm_callback(mock_llm)
        text = "E" * 3000
        result = mm._summarize_text(text, max_output_chars=1500)
        assert result.startswith("[Context summarized at ")
        assert result.endswith("E" * 1500)

    def test_set_llm_callback_properly_sets_callback(self, mm: MemoryManager):
        """set_llm_callback properly stores the callback on the instance."""
        assert mm._llm_callback is None

        def my_callback(prompt: str) -> str:
            return "summary"

        mm.set_llm_callback(my_callback)
        assert mm._llm_callback is my_callback

    def test_summarize_context_integrates_with_llm_callback(self, tmp_path):
        """summarize_context uses the LLM callback when set."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_llm_integration"

        def mock_llm(prompt: str) -> str:
            return "LLM produced summary of agent context."

        mm.set_llm_callback(mock_llm)

        # Write a long context that exceeds the threshold
        long_context = "Important discussion point. " * 300  # ~8100 chars
        memory = SlockMemory(role="planner", active_context=long_context)
        _write_agent_memory(mm, agent_id, memory)

        result = mm.summarize_context(agent_id, threshold=4000)
        assert result is True

        after = mm.read_agent_memory(agent_id)
        assert "[Context summarized at " in after.active_context
        assert "LLM produced summary of agent context." in after.active_context
        # Role preserved
        assert after.role == "planner"


class TestDiscussionConclusionSyncToAgents:
    """Tests for MemoryManager.sync_discussion_conclusion_to_agents."""

    def test_conclusion_written_to_all_agents(self, tmp_path):
        """Conclusion is written to all agent L1 active_context files."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_ids = ["agent_a", "agent_b", "agent_c"]
        for aid in agent_ids:
            _write_agent_memory(mm, aid, SlockMemory(role="coder"))

        mm.sync_discussion_conclusion_to_agents(agent_ids, "We will use gRPC.")

        for aid in agent_ids:
            memory = mm.read_agent_memory(aid)
            assert "We will use gRPC." in memory.active_context
            assert "Discussion conclusion" in memory.active_context

    def test_trigger_reason_included_when_provided(self, tmp_path):
        """trigger_reason is included in the context entry when provided."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_reason"
        _write_agent_memory(mm, agent_id, SlockMemory(role="tester"))

        mm.sync_discussion_conclusion_to_agents(
            [agent_id], "Adopt TDD.", trigger_reason="consensus reached"
        )

        memory = mm.read_agent_memory(agent_id)
        assert "(consensus reached)" in memory.active_context
        assert "Adopt TDD." in memory.active_context

    def test_trigger_reason_omitted_when_empty(self, tmp_path):
        """trigger_reason is omitted from context entry when empty."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_no_reason"
        _write_agent_memory(mm, agent_id, SlockMemory(role="reviewer"))

        mm.sync_discussion_conclusion_to_agents(
            [agent_id], "Use TypeScript.", trigger_reason=""
        )

        memory = mm.read_agent_memory(agent_id)
        assert "Use TypeScript." in memory.active_context
        # No parenthesized reason
        assert "()" not in memory.active_context
        assert "Discussion conclusion:" in memory.active_context

    def test_conclusion_truncated_to_500_chars(self, tmp_path):
        """Conclusion is truncated to 500 characters in the synced entry."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_truncate"
        _write_agent_memory(mm, agent_id, SlockMemory(role="planner"))

        long_conclusion = "W" * 1000
        mm.sync_discussion_conclusion_to_agents([agent_id], long_conclusion)

        memory = mm.read_agent_memory(agent_id)
        # The conclusion portion should be at most 500 chars of the original
        assert "W" * 500 in memory.active_context
        assert "W" * 501 not in memory.active_context

    def test_failure_for_one_agent_does_not_prevent_others(self, tmp_path):
        """Failure for one agent doesn't prevent syncing to others."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        # Only create memory for agent_ok1 and agent_ok2, not agent_bad
        _write_agent_memory(mm, "agent_ok1", SlockMemory(role="coder"))
        _write_agent_memory(mm, "agent_ok2", SlockMemory(role="tester"))

        # Make agent_bad's directory read-only to force a write failure
        bad_dir = os.path.join(str(tmp_path / "slock"), "agents", "agent_bad")
        os.makedirs(bad_dir, exist_ok=True)
        bad_memory_path = os.path.join(bad_dir, "MEMORY.md")
        with open(bad_memory_path, "w", encoding="utf-8") as f:
            f.write("# Role\ncoder\n")
        os.chmod(bad_dir, 0o444)

        try:
            mm.sync_discussion_conclusion_to_agents(
                ["agent_ok1", "agent_bad", "agent_ok2"], "Important decision."
            )
        finally:
            # Restore permissions for cleanup
            os.chmod(bad_dir, 0o755)

        # The other agents should still have the conclusion
        mem1 = mm.read_agent_memory("agent_ok1")
        mem2 = mm.read_agent_memory("agent_ok2")
        assert "Important decision." in mem1.active_context
        assert "Important decision." in mem2.active_context

    def test_empty_agent_ids_is_noop(self, tmp_path):
        """Empty agent_ids list results in no-op (no errors, no writes)."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))

        # Should not raise
        mm.sync_discussion_conclusion_to_agents([], "Some conclusion.")


# ===========================================================================
# Task 27: Three-phase lock pattern and original_len > compressed_len
# ===========================================================================
class TestSummarizeContextThreePhase:
    """Tests verifying summarize_context three-phase execution and length invariants."""

    def test_original_len_saved_before_compression(self, tmp_path):
        """original_len is captured BEFORE summarization, so compressed result is always shorter."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_len_check"
        long_context = "X" * 5000
        memory = SlockMemory(role="coder", active_context=long_context)
        _write_agent_memory(mm, agent_id, memory)

        result = mm.summarize_context(agent_id, threshold=4000)
        assert result is True

        after = mm.read_agent_memory(agent_id)
        # Compressed must be strictly shorter than original
        assert len(after.active_context) < len(long_context)

    def test_llm_callback_not_held_under_lock(self, tmp_path):
        """LLM callback is invoked outside the lock (validated by concurrent access)."""

        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_lock_test"
        long_context = "Y" * 6000
        memory = SlockMemory(role="tester", active_context=long_context)
        _write_agent_memory(mm, agent_id, memory)

        lock_held_during_callback = [False]

        def spy_llm(prompt: str) -> str:
            # Try to acquire the lock; if we can, it means the lock is NOT held
            acquired = mm._lock.acquire(blocking=False)
            if not acquired:
                lock_held_during_callback[0] = True
            else:
                mm._lock.release()
            return "Summary from LLM"

        mm.set_llm_callback(spy_llm)
        mm.summarize_context(agent_id, threshold=4000)

        # The callback should have been able to acquire the lock (not held during callback)
        assert not lock_held_during_callback[0], "Lock was held during LLM callback — deadlock risk!"

    def test_summarize_context_preserves_role_and_knowledge(self, tmp_path):
        """Three-phase summarization preserves role and key_knowledge."""
        mm = MemoryManager(base_path=str(tmp_path / "slock"))
        agent_id = "agent_preserve"
        memory = SlockMemory(
            role="architect",
            key_knowledge="Important architectural decisions here.",
            active_context="Z" * 5000,
        )
        _write_agent_memory(mm, agent_id, memory)

        mm.summarize_context(agent_id, threshold=4000)

        after = mm.read_agent_memory(agent_id)
        assert after.role == "architect"
        assert after.key_knowledge == "Important architectural decisions here."


class TestMessageArchiveRotation:
    """AC13: messages.jsonl rotation when exceeding limits."""

    def test_rotate_on_line_count_exceeded(self, tmp_path):
        """File is rotated when line count exceeds 10000."""
        from src.slock_engine.memory_manager import MemoryManager

        mm = MemoryManager(base_path=str(tmp_path))
        channel_id = "test_rotate_channel"

        # Write 10001 messages
        for i in range(10001):
            mm.append_message_archive(
                channel_id,
                sender_type="user",
                content=f"msg {i}",
                agent_id="",
                agent_name="",
            )

        # Check that .old file exists
        archive_path = mm.message_archive_path(channel_id)
        old_path = archive_path + ".old"
        assert os.path.exists(old_path), ".old file should exist after rotation"
        # Current file should be small (only the last message after rotate)
        assert os.path.exists(archive_path)
        with open(archive_path) as f:
            current_lines = f.readlines()
        # After rotation, the new file has just 1 line (the one written after rotate)
        assert len(current_lines) <= 2

    def test_rotate_preserves_old_file(self, tmp_path):
        """Rotation overwrites existing .old file."""
        from src.slock_engine.memory_manager import MemoryManager

        mm = MemoryManager(base_path=str(tmp_path))
        channel_id = "test_rotate_overwrite"
        archive_path = mm.message_archive_path(channel_id)
        old_path = archive_path + ".old"

        # Create a fake .old file
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with open(old_path, "w") as f:
            f.write("old content\n")

        # Write enough to trigger rotation
        for i in range(10001):
            mm.append_message_archive(
                channel_id,
                sender_type="user",
                content=f"msg {i}",
            )

        # .old should be overwritten with the rotated content
        with open(old_path) as f:
            content = f.read()
        assert "old content" not in content
        assert "msg 0" in content  # First messages should be in old file


class TestTailRead:
    """AC13: read_conversation_replay uses tail-read."""

    def test_tail_read_returns_last_entries(self, tmp_path):
        """tail-read returns the correct last n_rounds*2 entries."""
        from src.slock_engine.memory_manager import MemoryManager

        mm = MemoryManager(base_path=str(tmp_path))
        channel_id = "test_tail_read"

        # Write 100 messages
        for i in range(100):
            sender = "user" if i % 2 == 0 else "agent"
            mm.append_message_archive(
                channel_id,
                sender_type=sender,
                content=f"message_{i}",
                agent_name="bot" if sender == "agent" else "",
            )

        # Read last 5 rounds (10 entries)
        result = mm.read_conversation_replay(channel_id, n_rounds=5)
        assert len(result) == 10
        # Should be the last 10 messages (90-99)
        assert "message_90" in result[0]["content"]
        assert "message_99" in result[-1]["content"]

    def test_tail_read_empty_file(self, tmp_path):
        """tail-read on non-existent file returns empty list."""
        from src.slock_engine.memory_manager import MemoryManager

        mm = MemoryManager(base_path=str(tmp_path))
        result = mm.read_conversation_replay("nonexistent_channel", n_rounds=5)
        assert result == []

    def test_tail_read_fewer_entries_than_requested(self, tmp_path):
        """tail-read with fewer entries than requested returns all available."""
        from src.slock_engine.memory_manager import MemoryManager

        mm = MemoryManager(base_path=str(tmp_path))
        channel_id = "test_few_entries"

        # Write only 3 messages
        for i in range(3):
            mm.append_message_archive(
                channel_id,
                sender_type="user",
                content=f"msg_{i}",
            )

        result = mm.read_conversation_replay(channel_id, n_rounds=5)
        assert len(result) == 3


# ===========================================================================
# Test Class: Rationale Retention in Summarization
# ===========================================================================


class TestRationaleRetention:
    """Tests for [RATIONALE] marker preservation during text summarization.

    Verifies that sections marked with [RATIONALE] are prioritized for
    preservation when context is compressed.
    """

    def test_rationale_sections_extracted_from_text(self, mm: MemoryManager):
        """[RATIONALE] sections are identified and extracted from text."""
        text = """Regular content line 1.
[RATIONALE] Key decision: use PostgreSQL for data persistence.
Regular content line 2.
[RATIONALE] Security requirement: all API calls must be authenticated.
Regular content line 3."""
        result = mm._summarize_text(text, max_output_chars=300)
        # Both rationale sections should be preserved
        assert "[RATIONALE] Key decision" in result
        assert "[RATIONALE] Security requirement" in result

    def test_rationale_preserved_before_regular_content(self, mm: MemoryManager):
        """When truncating, [RATIONALE] sections come before remaining content."""
        # Create text with rationale at the beginning and lots of filler at the end
        rationale = "[RATIONALE] Critical architecture decision: use event-driven pattern."
        filler = "X" * 500
        text = f"{rationale}\n{filler}"

        result = mm._summarize_text(text, max_output_chars=200)
        # Rationale should be at the start (after timestamp marker)
        assert "[RATIONALE] Critical architecture" in result
        # The filler should be truncated (not all 500 X's)
        assert result.count("X") < 400

    def test_rationale_with_multiline_content(self, mm: MemoryManager):
        """Multiline [RATIONALE] sections are preserved as a unit."""
        text = """Preliminary content.
[RATIONALE] We chose Redis over Memcached because:
1. Redis supports persistence
2. Redis has richer data structures
3. Redis supports pub/sub natively

This concludes the rationale.
More regular content here."""
        result = mm._summarize_text(text, max_output_chars=400)
        # The entire rationale block should be preserved
        assert "[RATIONALE] We chose Redis" in result
        assert "1. Redis supports persistence" in result
        assert "3. Redis supports pub/sub natively" in result

    def test_rationale_with_multi_paragraph_content(self, mm: MemoryManager):
        """Multi-paragraph [RATIONALE] sections preserve blank lines between paragraphs.

        Regression test: Previously, blank lines terminated RATIONALE extraction,
        causing multi-paragraph reasoning content to be truncated.
        RATIONALE should only terminate at next [RATIONALE] marker or EOF.
        """
        text = """Preliminary content.
[RATIONALE] Paragraph 1: Initial analysis of the problem.
We need to consider multiple factors.

Paragraph 2: Second part of the reasoning.
This continues the rationale across a blank line.

Paragraph 3: Final conclusion.
The decision is made based on all the above.

Regular content after rationale."""
        result = mm._summarize_text(text, max_output_chars=800)
        # All three paragraphs should be preserved in the rationale
        assert "[RATIONALE] Paragraph 1" in result
        assert "Paragraph 2: Second part" in result
        assert "Paragraph 3: Final conclusion" in result
        # The key assertion: Paragraph 2 and 3 should be part of the RATIONALE block,
        # not mixed with regular content. With the bug, they would appear after
        # the rationale ends at the first blank line.
        # Verify that "Regular content after rationale" comes AFTER all three paragraphs
        idx_p3 = result.find("Paragraph 3: Final conclusion")
        idx_regular = result.find("Regular content after rationale")
        assert idx_p3 != -1, "Paragraph 3 should be in result"
        assert idx_regular != -1, "Regular content should be in result"
        assert idx_p3 < idx_regular, "Paragraph 3 should come before regular content"

    def test_rationale_blank_line_not_terminator(self, mm: MemoryManager):
        """Blank lines do NOT terminate RATIONALE extraction — only next [RATIONALE] or EOF does."""
        text = """Before.
[RATIONALE] First paragraph.

Second paragraph with blank line separator.

Third paragraph.
[RATIONALE] Separate rationale block.
After."""
        result = mm._summarize_text(text, max_output_chars=500)
        # Should have 2 separate rationale sections
        assert "First paragraph" in result
        assert "Second paragraph" in result
        assert "Third paragraph" in result
        assert "Separate rationale block" in result
        # First, second, third paragraphs should all be in the FIRST rationale block
        # (before "Separate rationale block")
        idx_first = result.find("First paragraph")
        idx_second = result.find("Second paragraph")
        idx_third = result.find("Third paragraph")
        idx_separate = result.find("Separate rationale block")
        assert idx_first < idx_separate, "First paragraph should be in first rationale"
        assert idx_second < idx_separate, "Second paragraph should be in first rationale"
        assert idx_third < idx_separate, "Third paragraph should be in first rationale"

    def test_multiple_rationale_sections_all_preserved(self, mm: MemoryManager):
        """All [RATIONALE] sections are preserved when within budget."""
        text = """Content A
[RATIONALE] Rationale one: performance requirements demand caching.
Content B
[RATIONALE] Rationale two: security audit requires detailed logging.
Content C
[RATIONALE] Rationale three: scalability needs horizontal partitioning.
Content D"""
        result = mm._summarize_text(text, max_output_chars=500)
        assert "Rationale one" in result
        assert "Rationale two" in result
        assert "Rationale three" in result

    def test_rationale_exceeds_budget_truncated_gracefully(self, mm: MemoryManager):
        """When rationale alone exceeds budget, it's truncated but still included."""
        # Create a very long rationale
        long_rationale = "[RATIONALE] " + "detail " * 1000
        text = f"{long_rationale}\nSome other content."

        result = mm._summarize_text(text, max_output_chars=300)
        # Should still start with the rationale marker
        assert "[RATIONALE]" in result
        # Should be within the budget
        assert len(result) <= 300

    def test_no_rationale_falls_back_to_tail_truncation(self, mm: MemoryManager):
        """Text without [RATIONALE] uses standard tail truncation."""
        text = "A" * 100 + "B" * 100 + "C" * 100
        result = mm._summarize_text(text, max_output_chars=150)
        # Without rationale markers, should preserve the tail (mostly C's)
        assert result.count("C") > result.count("A")

    def test_sync_discussion_conclusion_adds_rationale_marker(self, mm: MemoryManager, tmp_path):
        """sync_discussion_conclusion_to_agents adds [RATIONALE] marker when rationale provided."""
        agent_id = "agent_with_rationale"
        memory = SlockMemory(role="coder", active_context="existing context")
        _write_agent_memory(mm, agent_id, memory)

        mm.sync_discussion_conclusion_to_agents(
            agent_ids=[agent_id],
            conclusion="Use Redis for caching layer.",
            trigger_reason="uncertainty:needs review",
            rationale="Redis was chosen for its persistence and pub/sub capabilities.",
        )

        after = mm.read_agent_memory(agent_id)
        assert "Discussion conclusion" in after.active_context
        assert "[RATIONALE] Redis was chosen" in after.active_context

    def test_sync_discussion_conclusion_without_rationale(self, mm: MemoryManager, tmp_path):
        """sync_discussion_conclusion_to_agents works without rationale parameter."""
        agent_id = "agent_no_rationale"
        memory = SlockMemory(role="reviewer", active_context="existing")
        _write_agent_memory(mm, agent_id, memory)

        mm.sync_discussion_conclusion_to_agents(
            agent_ids=[agent_id],
            conclusion="Approach looks good.",
            trigger_reason="rule:coder->reviewer",
        )

        after = mm.read_agent_memory(agent_id)
        assert "Discussion conclusion" in after.active_context
        # No rationale marker should be present
        assert "[RATIONALE]" not in after.active_context
