"""Tests for knowledge conflict detection in discussions.

Verifies that discussion conclusions conflicting with an agent's
Key Knowledge trigger an escalation and skip the L1 sync.

WP-A: Knowledge Conflict Detection Upgrade
- LLM semantic conflict detection
- Human confirmation flow
- Conflict escalation card
"""

from unittest.mock import MagicMock

from src.slock_engine.card_templates import build_conflict_escalation_card
from src.slock_engine.discussion_manager import (
    _CONFLICT_PAIRS,
    DiscussionManager,
)


class TestKnowledgeConflictDetection:
    """Test suite for knowledge conflict detection."""

    def test_conflict_pairs_defined(self) -> None:
        """Conflict pairs should be defined with common antonyms."""
        assert len(_CONFLICT_PAIRS) > 0
        # Check some key pairs exist
        pair_strs = [f"{a}|{b}" for a, b in _CONFLICT_PAIRS]
        assert any("mysql" in p and "postgres" in p for p in pair_strs)
        assert any("allow" in p and "deny" in p for p in pair_strs)
        assert any("true" in p and "false" in p for p in pair_strs)

    def test_detect_conflict_mysql_vs_postgresql(self) -> None:
        """Should detect conflict when conclusion says MySQL but KK says PostgreSQL."""
        dm = DiscussionManager()

        conclusion = "We must use MySQL for this project."
        key_knowledge = "[DECISION] Database must be PostgreSQL."

        has_conflict, details, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is True
        assert "mysql" in details.lower()
        assert "postgres" in details.lower()
        assert needs_escalation is True

    def test_detect_conflict_allow_vs_deny(self) -> None:
        """Should detect conflict when conclusion says allow but KK says deny."""
        dm = DiscussionManager()

        conclusion = "Allow public access to the API."
        key_knowledge = "[DECISION] Deny all public API access."

        has_conflict, details, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is True
        assert "allow" in details.lower()
        assert "deny" in details.lower()
        assert needs_escalation is True

    def test_no_conflict_same_direction(self) -> None:
        """Should not detect conflict when both say the same thing."""
        dm = DiscussionManager()

        conclusion = "We must use PostgreSQL for this project."
        key_knowledge = "[DECISION] Database must be PostgreSQL."

        has_conflict, details, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is False
        assert details == ""
        assert needs_escalation is False

    def test_no_conflict_empty_inputs(self) -> None:
        """Should not detect conflict when inputs are empty."""
        dm = DiscussionManager()

        # Empty conclusion
        has_conflict, _, needs_escalation = dm._detect_knowledge_conflict(
            "", "Some knowledge"
        )
        assert has_conflict is False
        assert needs_escalation is False

        # Empty key_knowledge
        has_conflict, _, needs_escalation = dm._detect_knowledge_conflict(
            "Some conclusion", ""
        )
        assert has_conflict is False
        assert needs_escalation is False

        # Both empty
        has_conflict, _, needs_escalation = dm._detect_knowledge_conflict("", "")
        assert has_conflict is False
        assert needs_escalation is False

    def test_no_conflict_unrelated_content(self) -> None:
        """Should not detect conflict when content is unrelated."""
        dm = DiscussionManager()

        conclusion = "The API should return JSON responses."
        key_knowledge = "[DECISION] Use Python 3.10+ for all services."

        has_conflict, details, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is False
        assert needs_escalation is False

    def test_conflict_case_insensitive(self) -> None:
        """Conflict detection should be case-insensitive."""
        dm = DiscussionManager()

        conclusion = "We MUST use MYSQL for this project."
        key_knowledge = "[DECISION] database must be postgresql."

        has_conflict, _, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is True
        assert needs_escalation is True

    def test_conflict_true_vs_false(self) -> None:
        """Should detect conflict between true and false assertions."""
        dm = DiscussionManager()

        conclusion = "Feature flag is true: enable new UI."
        key_knowledge = "[DECISION] Feature flag should be false."

        has_conflict, details, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is True
        assert "true" in details.lower()
        assert "false" in details.lower()
        assert needs_escalation is True

    def test_needs_escalation_true_on_rule_conflict(self) -> None:
        """needs_escalation should be True when rule-based detection finds conflict."""
        dm = DiscussionManager()

        conclusion = "Enable the experimental feature."
        key_knowledge = "[DECISION] Disable all experimental features."

        has_conflict, _, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is True
        assert needs_escalation is True

    def test_needs_escalation_false_when_no_conflict(self) -> None:
        """needs_escalation should be False when no conflict is detected."""
        dm = DiscussionManager()

        # Use content that won't trigger rule-based detection
        # (avoiding http/https pair since "https" contains "http")
        conclusion = "The system should use Python 3.11 for all services."
        key_knowledge = "[DECISION] All backend services must run on Python 3.11."

        has_conflict, _, needs_escalation = dm._detect_knowledge_conflict(
            conclusion, key_knowledge
        )

        assert has_conflict is False
        assert needs_escalation is False


class TestLLMSemanticConflictDetection:
    """Test suite for LLM-based semantic conflict detection."""

    def test_llm_detection_returns_false_without_engine(self) -> None:
        """LLM detection should return (False, "") when no engine is available."""
        dm = DiscussionManager()  # No engine provided

        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Conclusion text",
            "Key knowledge text",
        )

        assert has_conflict is False
        assert details == ""

    def test_llm_detection_returns_false_with_empty_inputs(self) -> None:
        """LLM detection should return (False, "") for empty inputs."""
        mock_engine = MagicMock()
        dm = DiscussionManager(engine=mock_engine)

        # Empty conclusion
        has_conflict, details = dm._detect_conflict_llm_semantic(
            "", "Key knowledge"
        )
        assert has_conflict is False
        assert details == ""

        # Empty key_knowledge
        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Conclusion", ""
        )
        assert has_conflict is False
        assert details == ""

    def test_llm_detection_fallback_on_timeout(self) -> None:
        """LLM detection should return (False, "") on timeout to allow rule-based fallback."""
        mock_engine = MagicMock()
        mock_engine.registry.list_agents.return_value = [MagicMock()]

        # Make run_agent_session_full raise TimeoutError immediately
        from concurrent.futures import TimeoutError as FutureTimeoutError

        def timeout_call(*args, **kwargs):
            raise FutureTimeoutError("simulated timeout")

        mock_engine.run_agent_session_full.side_effect = timeout_call

        dm = DiscussionManager(engine=mock_engine)

        # This should return quickly since we raise immediately
        import time
        start = time.time()
        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Test conclusion",
            "Test key knowledge",
        )
        elapsed = time.time() - start

        # Should return very quickly (no actual wait)
        assert elapsed < 2
        # Should return False to allow rule-based fallback
        assert has_conflict is False
        assert details == ""

    def test_llm_detection_fallback_on_exception(self) -> None:
        """LLM detection should return (False, "") on exception."""
        mock_engine = MagicMock()
        mock_engine.registry.list_agents.return_value = [MagicMock()]
        mock_engine.run_agent_session_full.side_effect = Exception("LLM service down")

        dm = DiscussionManager(engine=mock_engine)

        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Test conclusion",
            "Test key knowledge",
        )

        assert has_conflict is False
        assert details == ""

    def test_llm_detection_parses_json_response(self) -> None:
        """LLM detection should correctly parse JSON response indicating conflict."""
        mock_engine = MagicMock()
        mock_engine.registry.list_agents.return_value = [MagicMock()]

        mock_result = MagicMock()
        mock_result.text = '{"conflict": true, "reason": "语义冲突：结论建议使用同步API，但关键知识要求异步"}'
        mock_engine.run_agent_session_full.return_value = mock_result

        dm = DiscussionManager(engine=mock_engine)

        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Use sync API",
            "Must use async API",
        )

        assert has_conflict is True
        assert "语义冲突" in details

    def test_llm_detection_parses_json_no_conflict(self) -> None:
        """LLM detection should correctly parse JSON response indicating no conflict."""
        mock_engine = MagicMock()
        mock_engine.registry.list_agents.return_value = [MagicMock()]

        mock_result = MagicMock()
        mock_result.text = '{"conflict": false, "reason": ""}'
        mock_engine.run_agent_session_full.return_value = mock_result

        dm = DiscussionManager(engine=mock_engine)

        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Use HTTPS",
            "Must use secure connections",
        )

        assert has_conflict is False
        assert details == ""

    def test_llm_detection_handles_markdown_code_block(self) -> None:
        """LLM detection should extract JSON from markdown code blocks."""
        mock_engine = MagicMock()
        mock_engine.registry.list_agents.return_value = [MagicMock()]

        mock_result = MagicMock()
        mock_result.text = """```json
{"conflict": true, "reason": "检测到逻辑矛盾"}
```"""
        mock_engine.run_agent_session_full.return_value = mock_result

        dm = DiscussionManager(engine=mock_engine)

        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Conclusion",
            "Key knowledge",
        )

        assert has_conflict is True
        assert "逻辑矛盾" in details

    def test_llm_detection_fallback_keyword_check(self) -> None:
        """LLM detection should fall back to keyword check if JSON parsing fails."""
        mock_engine = MagicMock()
        mock_engine.registry.list_agents.return_value = [MagicMock()]

        mock_result = MagicMock()
        mock_result.text = "存在冲突，两段文本表达了相反的意思"
        mock_engine.run_agent_session_full.return_value = mock_result

        dm = DiscussionManager(engine=mock_engine)

        has_conflict, details = dm._detect_conflict_llm_semantic(
            "Conclusion",
            "Key knowledge",
        )

        assert has_conflict is True
        assert "冲突" in details


class TestConflictEscalationCard:
    """Test suite for conflict escalation card template."""

    def test_card_structure(self) -> None:
        """Conflict escalation card should have correct structure."""
        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details="结论使用 'mysql'，但 Key Knowledge 包含 'postgresql'",
            conclusion="We should use MySQL for the database.",
            key_knowledge="[DECISION] Database must be PostgreSQL.",
            channel_id="test_channel",
            thread_id="test_thread_123",
        )

        # Check basic card structure
        assert card["schema"] == "2.0"
        assert card["config"]["wide_screen_mode"] is True
        assert card["header"]["template"] == "orange"
        assert "冲突" in card["header"]["title"]["content"]

    def test_card_contains_conflict_details(self) -> None:
        """Card should display conflict details."""
        conflict_details = "结论使用 'mysql'，但 Key Knowledge 包含 'postgresql'"
        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details=conflict_details,
            conclusion="MySQL conclusion",
            key_knowledge="PostgreSQL knowledge",
        )

        # Find conflict details in elements
        found_conflict = False
        for element in card["body"]["elements"]:
            if element.get("tag") == "markdown":
                content = element.get("content", "")
                if conflict_details in content:
                    found_conflict = True
                    break

        assert found_conflict, "Conflict details not found in card"

    def test_card_has_accept_and_reject_buttons(self) -> None:
        """Card should have both accept and reject buttons."""
        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details="Test conflict",
            conclusion="Test conclusion",
            key_knowledge="Test knowledge",
            channel_id="test_channel",
            thread_id="test_thread",
        )

        # Find buttons in elements
        accept_found = False
        reject_found = False

        def find_buttons(elements):
            nonlocal accept_found, reject_found
            for element in elements:
                if element.get("tag") == "button":
                    text = element.get("text", {}).get("content", "")
                    value = element.get("value", {})
                    if "接受" in text or value.get("decision") == "accept":
                        accept_found = True
                    if "拒绝" in text or value.get("decision") == "reject":
                        reject_found = True
                elif "elements" in element:
                    find_buttons(element["elements"])
                elif "columns" in element:
                    for col in element["columns"]:
                        if "elements" in col:
                            find_buttons(col["elements"])

        find_buttons(card["body"]["elements"])

        assert accept_found, "Accept button not found"
        assert reject_found, "Reject button not found"

    def test_card_button_values_contain_action(self) -> None:
        """Card buttons should have correct action value."""
        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details="Test conflict",
            conclusion="Test conclusion",
            key_knowledge="Test knowledge",
            channel_id="test_channel",
            thread_id="test_thread_123",
        )

        # Find action values in buttons
        def check_button_values(elements):
            for element in elements:
                if element.get("tag") == "button":
                    value = element.get("value", {})
                    if value.get("action") == "slock_conflict_resolve":
                        # Check that decision is either accept or reject
                        decision = value.get("decision")
                        assert decision in ("accept", "reject"), f"Invalid decision: {decision}"
                        # Check thread_id is included
                        assert value.get("thread_id") == "test_thread_123"
                        # Check agent_name is included
                        assert value.get("agent_name") == "TestAgent"
                elif "elements" in element:
                    check_button_values(element["elements"])
                elif "columns" in element:
                    for col in element["columns"]:
                        if "elements" in col:
                            check_button_values(col["elements"])

        check_button_values(card["body"]["elements"])

    def test_card_contains_collapsible_panels(self) -> None:
        """Card should have collapsible panels for conclusion and key knowledge."""
        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details="Test conflict",
            conclusion="Test conclusion that is quite long and should be in a collapsible panel",
            key_knowledge="Test key knowledge that is also quite detailed",
        )

        collapsible_panels = [
            e for e in card["body"]["elements"]
            if e.get("tag") == "collapsible_panel"
        ]

        # Should have at least 2 collapsible panels (conclusion and key knowledge)
        assert len(collapsible_panels) >= 2


class TestConflictResolutionHandling:
    """Test suite for conflict resolution action handling."""

    def test_accept_decision_value_structure(self) -> None:
        """Accept decision should have correct value structure."""
        card = build_conflict_escalation_card(
            agent_name="CoderAgent",
            conflict_details="Test conflict",
            conclusion="Test conclusion",
            key_knowledge="Test knowledge",
            channel_id="channel_1",
            thread_id="thread_abc",
        )

        # Find accept button
        def find_accept_button(elements):
            for element in elements:
                if element.get("tag") == "button":
                    value = element.get("value", {})
                    if value.get("decision") == "accept":
                        return value
                elif "elements" in element:
                    result = find_accept_button(element["elements"])
                    if result:
                        return result
                elif "columns" in element:
                    for col in element["columns"]:
                        if "elements" in col:
                            result = find_accept_button(col["elements"])
                            if result:
                                return result
            return None

        accept_value = find_accept_button(card["body"]["elements"])
        assert accept_value is not None
        assert accept_value["action"] == "slock_conflict_resolve"
        assert accept_value["decision"] == "accept"
        assert accept_value["thread_id"] == "thread_abc"
        assert accept_value["agent_name"] == "CoderAgent"
        assert accept_value["channel_id"] == "channel_1"

    def test_reject_decision_value_structure(self) -> None:
        """Reject decision should have correct value structure."""
        card = build_conflict_escalation_card(
            agent_name="ReviewerAgent",
            conflict_details="Test conflict",
            conclusion="Test conclusion",
            key_knowledge="Test knowledge",
            channel_id="channel_2",
            thread_id="thread_xyz",
        )

        # Find reject button
        def find_reject_button(elements):
            for element in elements:
                if element.get("tag") == "button":
                    value = element.get("value", {})
                    if value.get("decision") == "reject":
                        return value
                elif "elements" in element:
                    result = find_reject_button(element["elements"])
                    if result:
                        return result
                elif "columns" in element:
                    for col in element["columns"]:
                        if "elements" in col:
                            result = find_reject_button(col["elements"])
                            if result:
                                return result
            return None

        reject_value = find_reject_button(card["body"]["elements"])
        assert reject_value is not None
        assert reject_value["action"] == "slock_conflict_resolve"
        assert reject_value["decision"] == "reject"
        assert reject_value["thread_id"] == "thread_xyz"
        assert reject_value["agent_name"] == "ReviewerAgent"
        assert reject_value["channel_id"] == "channel_2"

    def test_card_truncates_long_content(self) -> None:
        """Card should truncate very long content."""
        long_conclusion = "A" * 1000
        long_knowledge = "B" * 1000

        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details="Test conflict details",
            conclusion=long_conclusion,
            key_knowledge=long_knowledge,
        )

        # Check that the card was created without error
        assert card is not None
        assert card["schema"] == "2.0"

    def test_card_with_optional_params(self) -> None:
        """Card should work with optional parameters omitted."""
        card = build_conflict_escalation_card(
            agent_name="TestAgent",
            conflict_details="Test conflict",
            conclusion="Test conclusion",
            key_knowledge="Test knowledge",
            # channel_id and thread_id omitted
        )

        assert card is not None
        assert card["schema"] == "2.0"

        # Buttons should still have action
        def check_action(elements):
            for element in elements:
                if element.get("tag") == "button":
                    value = element.get("value", {})
                    if value.get("action") == "slock_conflict_resolve":
                        return True
                elif "elements" in element:
                    if check_action(element["elements"]):
                        return True
                elif "columns" in element:
                    for col in element["columns"]:
                        if "elements" in col and check_action(col["elements"]):
                            return True
            return False

        assert check_action(card["body"]["elements"])
