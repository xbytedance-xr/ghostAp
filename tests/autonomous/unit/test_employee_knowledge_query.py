from __future__ import annotations

import pytest

from src.autonomous.knowledge import AuthorizedKnowledgeQuery
from tests.autonomous.knowledge_helpers import (
    build_knowledge_composition,
    close_knowledge_composition,
    seed_terminal,
)


def test_query_reads_index_then_pages_and_returns_source_citations(tmp_path) -> None:
    writer, composition = build_knowledge_composition(tmp_path)
    terminal = seed_terminal(
        composition,
        "query",
        "Checkpoint recovery prevents duplicate effects.\nAlways verify the Journal anchor.",
    )
    composition.knowledge_service.enqueue_terminal(terminal)
    composition.knowledge_service.drain()
    result = composition.knowledge_service.query(
        AuthorizedKnowledgeQuery(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            requester_principal_id="ou_owner",
            query="checkpoint duplicate effects",
        )
    )
    assert result.hits
    assert result.hits[0].citation.page_id == result.hits[0].page_id
    assert result.hits[0].citation.source_ids[0].startswith("hist_")
    close_knowledge_composition(writer, composition)


def test_unauthorized_query_is_denied_before_blob_read(tmp_path, monkeypatch) -> None:
    writer, composition = build_knowledge_composition(tmp_path)
    terminal = seed_terminal(composition, "denied", "Private reusable finding.")
    composition.knowledge_service.enqueue_terminal(terminal)
    composition.knowledge_service.drain()
    reads = []
    monkeypatch.setattr(
        composition.service,
        "read_blob",
        lambda _ref: reads.append("read") or b"",
    )
    with pytest.raises(PermissionError):
        composition.knowledge_service.query(
            AuthorizedKnowledgeQuery(
                tenant_key="tenant_1",
                agent_id="agt_alpha",
                requester_principal_id="ou_attacker",
                query="private",
            )
        )
    assert reads == []
    close_knowledge_composition(writer, composition)
