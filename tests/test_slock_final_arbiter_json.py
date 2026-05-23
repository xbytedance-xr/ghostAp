"""Unit tests for Final Arbiter strict JSON output in DiscussionManager.

Tests cover:
- Valid JSON response parsing (conclusion, key_points, decision)
- Invalid JSON response fallback to plain text
- Missing fields in JSON (fault tolerance)
- JSON extraction from code blocks
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.discussion_manager import DiscussionManager
from src.slock_engine.models import (
    AgentIdentity,
    DiscussionConfig,
    DiscussionMessage,
    DiscussionStatus,
    DiscussionThread,
)

# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "agent-001",
    name: str = "TestAgent",
    role: str = "coder",
) -> AgentIdentity:
    """Create a test AgentIdentity."""
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        role=role,
    )


def _make_thread(
    participants: Optional[list[str]] = None,
    config: Optional[DiscussionConfig] = None,
    messages: Optional[list[DiscussionMessage]] = None,
    status: DiscussionStatus = DiscussionStatus.ACTIVE,
    total_tokens_used: int = 0,
) -> DiscussionThread:
    """Create a test DiscussionThread ready for Final Arbiter."""
    cfg = config or DiscussionConfig(max_rounds=3)
    return DiscussionThread(
        thread_id=str(uuid.uuid4()),
        channel_id="channel-test",
        participants=participants or ["agent-001", "agent-002"],
        messages=messages or [],
        status=status,
        config=cfg,
        trigger_reason="test",
        total_tokens_used=total_tokens_used,
    )


def _make_message(
    sender: str = "agent-001",
    receiver: str = "agent-002",
    content: str = "test message",
    round_num: int = 1,
    token_count: int = 10,
) -> DiscussionMessage:
    """Create a test DiscussionMessage."""
    return DiscussionMessage(
        message_id=str(uuid.uuid4()),
        sender_agent_id=sender,
        receiver_agent_id=receiver,
        content=content,
        round_num=round_num,
        timestamp=time.time(),
        token_count=token_count,
    )


def _make_engine_with_agents(agents: dict[str, AgentIdentity]) -> MagicMock:
    """Create a mock engine with a registry containing agents."""
    from src.slock_engine.protocols import DiscussionEngineProtocol

    engine = MagicMock(spec=DiscussionEngineProtocol)
    registry = MagicMock()
    registry.agents = agents
    registry.list_agents.return_value = list(agents.values())
    registry.find_by_name.side_effect = lambda name: next(
        (a for a in agents.values() if a.name == name), None
    )
    engine.registry = registry
    return engine


@pytest.fixture
def manager_with_engine() -> DiscussionManager:
    """Discussion manager with a mocked engine containing agents."""
    coder = _make_agent(agent_id="coder-001", name="Coder", role="coder")
    reviewer = _make_agent(agent_id="reviewer-001", name="Reviewer", role="reviewer")

    agents = {
        "coder-001": coder,
        "reviewer-001": reviewer,
    }
    engine = _make_engine_with_agents(agents)
    config = DiscussionConfig(
        max_rounds=3,
        token_budget=50000,
        trigger_rules=["coder->reviewer"],
        convergence_threshold=0.85,
    )
    return DiscussionManager(engine=engine, config=config)


# ===========================================================================
# Test Class: Final Arbiter JSON Parsing
# ===========================================================================


class TestFinalArbiterJsonParsing:
    """Tests for _run_final_arbiter strict JSON output and parsing."""

    def test_valid_json_response_parsed_correctly(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 1: Valid JSON response is parsed correctly.

        When agent returns valid JSON with all required fields,
        the conclusion should be extracted from the 'conclusion' field.
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        valid_json_response = json.dumps({
            "conclusion": "经过讨论，团队决定采用方案A。该方案在性能和可维护性之间取得了最佳平衡。",
            "key_points": ["方案A性能提升30%", "方案B维护成本较高", "团队一致同意方案A"],
            "decision": "采用方案A",
        })

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(valid_json_response, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        assert thread.conclusion is not None
        assert "方案A" in thread.conclusion
        assert "性能提升" not in thread.conclusion  # Should be from conclusion field, not key_points
        assert thread.total_tokens_used == 150

    def test_invalid_json_response_falls_back_to_plain_text(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 2: Invalid JSON response falls back to plain text.

        When agent returns plain text instead of JSON,
        the raw response should be used as the conclusion (backward compatibility).
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        plain_text_response = (
            "经过讨论，团队决定采用方案A。该方案在性能和可维护性之间取得了最佳平衡。"
        )

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(plain_text_response, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        assert thread.conclusion == plain_text_response
        assert thread.total_tokens_used == 150

    def test_json_with_missing_fields_fallback_handling(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 3: JSON with missing fields uses fallback logic.

        When JSON is valid but missing 'conclusion' field,
        should fall back to 'decision' or 'key_points'.
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        # Missing 'conclusion' field, only has 'decision'
        json_missing_conclusion = json.dumps({
            "key_points": ["方案A性能更好", "方案B更便宜"],
            "decision": "采用方案A",
        })

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(json_missing_conclusion, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        # Should use 'decision' field as fallback
        assert thread.conclusion == "采用方案A"
        assert thread.total_tokens_used == 150

    def test_json_with_only_key_points_joins_them(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 3b: JSON with only key_points joins them.

        When only 'key_points' is available, join them with semicolons.
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        json_only_key_points = json.dumps({
            "key_points": ["要点1", "要点2", "要点3"],
        })

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(json_only_key_points, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        assert "要点1" in thread.conclusion
        assert "要点2" in thread.conclusion
        assert "要点3" in thread.conclusion

    def test_json_in_code_block_extracted_correctly(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 4: JSON wrapped in markdown code blocks is extracted.

        When agent returns JSON inside ```json ... ``` blocks,
        the JSON should still be parsed correctly.
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        response_with_code_block = '''```json
{
    "conclusion": "代码块中的JSON结论",
    "key_points": ["测试1", "测试2"],
    "decision": "通过"
}
```'''

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(response_with_code_block, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        assert thread.conclusion == "代码块中的JSON结论"

    def test_json_with_leading_trailing_text_extracted(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 5: JSON with surrounding text is extracted.

        When agent adds explanation text before/after JSON,
        the JSON should still be extracted and parsed.
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        response_with_surrounding_text = '''这是我的分析：

{"conclusion": "最终结论文本", "key_points": ["a", "b"], "decision": "同意"}

希望这个结论有帮助。'''

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(response_with_surrounding_text, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        assert thread.conclusion == "最终结论文本"

    def test_empty_json_object_falls_back_to_raw(
        self, manager_with_engine: DiscussionManager
    ):
        """Test case 6: Empty JSON object falls back to raw response.

        When JSON is valid but contains no useful fields,
        fall back to the raw response text.
        """
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        empty_json = "{}"

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=(empty_json, 150)
        ):
            manager_with_engine._run_final_arbiter(thread)

        # Should fall back to raw response (which is "{}")
        assert thread.conclusion == "{}"

    def test_prompt_requires_json_format(self, manager_with_engine: DiscussionManager):
        """Verify the prompt requires strict JSON output format."""
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )

        captured_prompt = None

        def capture_prompt(agent_id, prompt, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return (json.dumps({"conclusion": "ok", "key_points": [], "decision": "ok"}), 100)

        with patch.object(
            manager_with_engine, "_execute_agent_turn", side_effect=capture_prompt
        ):
            manager_with_engine._run_final_arbiter(thread)

        assert captured_prompt is not None
        # Verify prompt contains JSON schema requirements
        assert "conclusion" in captured_prompt
        assert "key_points" in captured_prompt
        assert "decision" in captured_prompt
        assert "JSON" in captured_prompt
        assert "ONLY with valid JSON" in captured_prompt or "Respond ONLY" in captured_prompt

    def test_no_participants_skips_arbiter(self, manager_with_engine: DiscussionManager):
        """Edge case: Thread with no participants should skip arbiter."""
        thread = _make_thread(
            participants=[],
        )
        original_conclusion = thread.conclusion

        manager_with_engine._run_final_arbiter(thread)

        # Should not change anything
        assert thread.conclusion == original_conclusion

    def test_empty_response_handled_gracefully(self, manager_with_engine: DiscussionManager):
        """Edge case: Empty response from agent is handled gracefully."""
        thread = _make_thread(
            participants=["coder-001", "reviewer-001"],
        )
        original_conclusion = thread.conclusion

        with patch.object(
            manager_with_engine, "_execute_agent_turn", return_value=("", 0)
        ):
            manager_with_engine._run_final_arbiter(thread)

        # Should keep original conclusion (None or existing)
        assert thread.conclusion == original_conclusion
