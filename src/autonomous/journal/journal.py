"""Compatibility facade for the canonical fenced autonomous journal."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .frame import JournalEvent, TransactionFrame
from .writer import JournalWriter as _CanonicalJournalWriter


@dataclass(frozen=True)
class JournalEntry:
    """Legacy event name retained while callers migrate to JournalEvent."""

    entry_type: str
    entity_id: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_event(self) -> JournalEvent:
        return JournalEvent(
            event_type=self.entry_type.replace("_", "."),
            aggregate_id=self.entity_id,
            payload=self.data,
            timestamp=self.timestamp,
        )


class JournalWriter(_CanonicalJournalWriter):
    """Canonical writer plus a temporary async legacy entrypoint."""

    async def commit_frame(
        self,
        entries: Sequence[JournalEntry],
    ) -> TransactionFrame:
        """Commit legacy entries through the one canonical transaction chain."""
        if not entries:
            raise ValueError("cannot commit an empty transaction")
        events = tuple(entry.to_event() for entry in entries)
        aggregate_ids = {event.aggregate_id for event in events}
        expected = {
            aggregate_id: self._aggregate_versions.get(aggregate_id, 0)
            for aggregate_id in aggregate_ids
        }
        return self.commit(events, expected).frame
