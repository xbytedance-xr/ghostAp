"""Unit tests for src/card/render/payload_truncator.py.

Covers boundary conditions: empty payload, at threshold, over threshold,
large Unicode, and node count exceeded.
"""
from __future__ import annotations

import json

import pytest

from src.card.render.payload_truncator import (
    check_and_truncate_payload,
    count_markdown_table_blocks,
    count_tagged_nodes,
)


def _collect_tags(obj):
    tags = []
    if isinstance(obj, dict):
        tag = obj.get("tag")
        if isinstance(tag, str):
            tags.append(tag)
        for value in obj.values():
            tags.extend(_collect_tags(value))
    elif isinstance(obj, list):
        for item in obj:
            tags.extend(_collect_tags(item))
    return tags


class TestCountTaggedNodes:
    """Tests for count_tagged_nodes helper."""

    def test_empty_dict(self):
        assert count_tagged_nodes({}) == 0

    def test_single_tagged(self):
        assert count_tagged_nodes({"tag": "div"}) == 1

    def test_nested_tagged(self):
        obj = {"tag": "div", "children": [{"tag": "span"}, {"tag": "text"}]}
        assert count_tagged_nodes(obj) == 3

    def test_list_of_tagged(self):
        obj = [{"tag": "a"}, {"tag": "b"}, {"no_tag": "c"}]
        assert count_tagged_nodes(obj) == 2


class TestMarkdownTableGuard:
    """Tests for Feishu markdown table limit guard."""

    def test_count_markdown_table_blocks_ignores_code_fences(self):
        text = (
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n\n"
            "```text\n"
            "| C | D |\n"
            "|---|---|\n"
            "| 3 | 4 |\n"
            "```\n"
        )

        assert count_markdown_table_blocks(text) == 1

    def test_markdown_tables_over_feishu_limit_are_neutralized(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = {
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "\n\n".join([table] * 6)}
                ]
            },
        }
        content = json.dumps(card, ensure_ascii=False)

        result = check_and_truncate_payload(content, max_size=1_000_000)
        parsed = json.loads(result)
        markdown = parsed["body"]["elements"][0]["content"]

        assert result != content
        assert count_markdown_table_blocks(markdown) == 0
        assert markdown.count("```text") == 6
        assert "表格数量超过飞书卡片限制" in result

    def test_markdown_tables_at_feishu_limit_are_preserved(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = {
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "\n\n".join([table] * 5)}
                ]
            },
        }
        content = json.dumps(card, ensure_ascii=False)

        result = check_and_truncate_payload(content, max_size=1_000_000)

        assert result == content


class TestCheckAndTruncatePayload:
    """Tests for check_and_truncate_payload."""

    def test_empty_payload(self):
        """Empty JSON object should pass through unchanged."""
        content = json.dumps({})
        result = check_and_truncate_payload(content)
        assert result == content

    def test_at_threshold_not_truncated(self):
        """Payload exactly at byte budget should not be truncated."""
        # Create a card just under the limit
        max_size = 1000  # Use small budget for test
        # Body that fits exactly
        body_text = "x" * 500
        card = {
            "body": {"elements": [{"tag": "markdown", "content": body_text}]}
        }
        content = json.dumps(card, ensure_ascii=False)
        result = check_and_truncate_payload(content, max_size=max_size)
        # Should not be truncated since size < max_size
        assert result == content

    def test_over_threshold_truncated(self):
        """Payload exceeding byte budget should be truncated."""
        max_size = 500
        # Create a payload that's clearly over the limit
        big_text = "A" * 10000
        card = {
            "body": {"elements": [{"tag": "markdown", "content": big_text}]}
        }
        content = json.dumps(card, ensure_ascii=False)
        assert len(content.encode("utf-8")) > max_size

        result = check_and_truncate_payload(content, max_size=max_size)
        # Result should be smaller or a fallback card
        assert len(result.encode("utf-8")) <= max_size or "已截断" in result

    def test_large_unicode_characters(self):
        """Payload with large multi-byte Unicode characters should handle correctly."""
        max_size = 500
        # Each emoji is 4 bytes in UTF-8
        emoji_text = "🎉" * 3000  # 12000 bytes
        card = {
            "body": {"elements": [{"tag": "markdown", "content": emoji_text}]}
        }
        content = json.dumps(card, ensure_ascii=False)
        assert len(content.encode("utf-8")) > max_size

        result = check_and_truncate_payload(content, max_size=max_size)
        # Should produce valid JSON
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_node_count_exceeded(self):
        """Payload with too many tagged nodes should be truncated."""
        # Create a card with many nodes (over the 180 budget)
        elements = [{"tag": "div", "content": f"item {i}"} for i in range(200)]
        card = {
            "body": {"elements": elements}
        }
        content = json.dumps(card, ensure_ascii=False)
        assert count_tagged_nodes(card) > 180

        # Use a large max_size so only node count triggers
        result = check_and_truncate_payload(content, max_size=1_000_000)
        # Should have been modified (truncated or warning added)
        assert result != content

    def test_node_count_guard_returns_card_under_feishu_limit(self):
        """Node overflow fallback must not itself exceed Feishu's 200-element cap."""
        elements = [
            {
                "tag": "collapsible_panel",
                "header": {
                    "title": {"tag": "markdown", "content": f"tool {idx}"},
                    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined"},
                },
                "elements": [{"tag": "markdown", "content": f"detail {idx}"}],
            }
            for idx in range(55)
        ]
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "Overflow"}},
            "body": {"elements": elements},
        }
        content = json.dumps(card, ensure_ascii=False)
        assert count_tagged_nodes(card) > 200

        result = check_and_truncate_payload(content, max_size=1_000_000)
        parsed = json.loads(result)

        assert count_tagged_nodes(parsed) <= 180
        assert len(result.encode("utf-8")) <= 30 * 1024

    def test_valid_json_always_returned(self):
        """Result should always be valid JSON regardless of input size."""
        big_text = "Z" * 50000
        card = {
            "header": {"title": {"tag": "plain_text", "content": "Test"}},
            "body": {"elements": [{"tag": "markdown", "content": big_text}]},
        }
        content = json.dumps(card, ensure_ascii=False)
        result = check_and_truncate_payload(content, max_size=1000)
        # Must be valid JSON
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_engine_type_hint_in_warning(self):
        """Truncation warning should include engine-specific command hint when first pass fits."""
        # Create a payload with a large content field that triggers truncation
        # but first-pass result still fits the budget
        big_text = "X" * 9000  # >8000 so truncate_recursive will cut it
        card = {
            "body": {"elements": [{"tag": "markdown", "content": big_text}]}
        }
        content = json.dumps(card, ensure_ascii=False)
        # Set max_size smaller than original but large enough for truncated version
        result = check_and_truncate_payload(content, max_size=9000, engine_type="deep")
        # Should mention /deep in truncation warning note
        assert "/deep" in result

    def test_truncation_warning_uses_schema_v2_safe_element(self):
        """Truncation warning must not use deprecated Schema V1 note elements."""
        big_text = "X" * 9000
        card = {
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": big_text}]},
        }
        content = json.dumps(card, ensure_ascii=False)

        result = check_and_truncate_payload(content, max_size=9000, engine_type="deep")
        parsed = json.loads(result)

        assert "note" not in _collect_tags(parsed)
        assert "/deep" in result

    def test_aggressive_fallback_card_keeps_schema_v2_identity(self):
        """Aggressive truncation fallback must remain a Schema V2 card."""
        big_text = "X" * 50000
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "Test"}},
            "body": {"elements": [{"tag": "markdown", "content": big_text}]},
        }
        content = json.dumps(card, ensure_ascii=False)

        result = check_and_truncate_payload(content, max_size=1000)
        parsed = json.loads(result)

        assert parsed["schema"] == "2.0"
        assert parsed["config"]["update_multi"] is True
        assert "note" not in _collect_tags(parsed)
