"""Secret-free review item rendering."""

from __future__ import annotations

import json


def render_review_item(*, source_id: str, source_hash: str, issues: tuple[str, ...]) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "source_id": source_id,
            "source_hash": source_hash,
            "issues": sorted(set(issues)),
            "status": "review_required",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = ["render_review_item"]
