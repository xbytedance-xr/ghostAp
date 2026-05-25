"""Payload truncation utilities for Feishu card content.

This module re-exports from src.card.shared.truncation for backward
compatibility. New code should import directly from src.card.shared.truncation.
"""

from __future__ import annotations

from src.card.shared.truncation import (
    FEISHU_CARD_TABLE_LIMIT,
    check_and_truncate_payload,
    count_markdown_table_blocks,
    count_tagged_nodes,
)

__all__ = [
    "FEISHU_CARD_TABLE_LIMIT",
    "check_and_truncate_payload",
    "count_markdown_table_blocks",
    "count_tagged_nodes",
]
