"""Delivery binding: tracks which message/card corresponds to each page."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class PageBinding:
    """Binding for a single card page."""

    message_id: str = ""
    card_id: str = ""
    signature: str = ""
    last_text: str = ""
    page_index: int = 0


@dataclass
class DeliveryBinding:
    """Binding for all pages of a session."""

    session_id: str = ""
    chat_id: str = ""
    pages: dict[int, PageBinding] = field(default_factory=dict)
    segment_index: int = 0


class BindingStore:
    """Thread-safe store for session → DeliveryBinding mappings."""

    def __init__(self) -> None:
        self._bindings: dict[str, DeliveryBinding] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> DeliveryBinding | None:
        """Get binding for a session."""
        with self._lock:
            return self._bindings.get(session_id)

    def create(self, session_id: str, chat_id: str) -> DeliveryBinding:
        """Create a new binding for a session."""
        with self._lock:
            binding = DeliveryBinding(session_id=session_id, chat_id=chat_id)
            self._bindings[session_id] = binding
            return binding

    def set_page(
        self,
        session_id: str,
        page_index: int,
        message_id: str,
        card_id: str,
        signature: str,
        last_text: str = "",
    ) -> None:
        """Set or update a page binding."""
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            binding.pages[page_index] = PageBinding(
                message_id=message_id,
                card_id=card_id,
                signature=signature,
                last_text=last_text,
                page_index=page_index,
            )

    def update_text(self, session_id: str, page_index: int, text: str) -> None:
        """Update the last_text for a page (after element_content push)."""
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            page = binding.pages.get(page_index)
            if page is not None:
                page.last_text = text

    def update_signature(self, session_id: str, page_index: int, signature: str) -> None:
        """Update the signature for a page (after card.update)."""
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            page = binding.pages.get(page_index)
            if page is not None:
                page.signature = signature

    def remove(self, session_id: str) -> DeliveryBinding | None:
        """Remove and return the binding for a session."""
        with self._lock:
            return self._bindings.pop(session_id, None)

    def page_count(self, session_id: str) -> int:
        """Get the number of pages for a session."""
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return 0
            return len(binding.pages)
