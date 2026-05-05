"""Shared card pipeline types.

This module hosts data-classes used across multiple card pipeline layers
(delivery, session, render, handler) to maintain a strict unidirectional
dependency chain:  delivery → session → render → handler.

All layers import shared types from here instead of cross-importing
from each other.
"""

from __future__ import annotations

import copy as _copy_mod
from dataclasses import dataclass, field

_deepcopy = _copy_mod.deepcopy


@dataclass(frozen=True)
class ActiveElement:
    """Points to the streaming element that can be updated via element_content API."""

    element_id: str
    text: str


@dataclass(frozen=True)
class RenderedCard:
    """Output of render_card(): one per page."""

    _card_json: dict = field(default_factory=dict)
    structure_signature: str = ""
    content_hash: str = ""  # Hash of frequently-changing content (progress, criteria, banner)
    active_element: ActiveElement | None = None
    page_index: int = 0
    total_pages: int = 1

    @property
    def _raw_payload(self) -> dict:
        """Preferred internal access to card payload. See docs/2026-04-30-card-refactor-design.md."""
        return self._card_json

    def to_feishu_json(self, *, copy: bool = True) -> dict:
        """Serialize to Feishu Schema 2.0 card JSON.

        Args:
            copy: If True (default), returns a deep copy to prevent callers
                  from mutating internal state. If False, returns the internal
                  dict directly — caller MUST NOT mutate the returned dict.
                  Use copy=False only on internal read-only paths (e.g. streaming
                  element updates) for performance.
        """
        if copy:
            return _deepcopy(self._card_json)
        return self._card_json
