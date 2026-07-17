"""Index-first full-text knowledge query with source citations."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from ..data.models import DataKind
from ..journal.blob_store import BlobRef
from .models import (
    AuthorizedKnowledgeQuery,
    KnowledgeCitation,
    KnowledgeQueryHit,
    KnowledgeQueryResult,
)

KnowledgeAuthorizer = Callable[[AuthorizedKnowledgeQuery, str], bool]


def parse_knowledge_page(content: str) -> tuple[dict[str, object], str]:
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        raise ValueError("knowledge page frontmatter is missing")
    frontmatter, body = content[4:].split("\n---\n", 1)
    fields: dict[str, object] = {}
    for line in frontmatter.splitlines():
        if ": " not in line:
            raise ValueError("invalid knowledge frontmatter")
        key, raw = line.split(": ", 1)
        fields[key] = json.loads(raw)
    required = {
        "schema_version",
        "page_id",
        "kind",
        "title",
        "source_ids",
        "source_hashes",
        "confidence",
        "status",
        "knowledge_generation",
        "updated_at",
    }
    if set(fields) != required or fields["schema_version"] != 1:
        raise ValueError("invalid knowledge frontmatter schema")
    return fields, body


class EmployeeKnowledgeQuery:
    def __init__(self, *, data_composition: object, authorizer: KnowledgeAuthorizer) -> None:
        self._data = data_composition
        self._authorize = authorizer

    def query(self, request: AuthorizedKnowledgeQuery) -> KnowledgeQueryResult:
        if not isinstance(request, AuthorizedKnowledgeQuery):
            raise TypeError("request must be AuthorizedKnowledgeQuery")
        if not self._authorize(request, "knowledge_index"):
            raise PermissionError("knowledge query denied")
        state = self._data.service.rebuild_projection()
        index_id = state.latest_employee_document.get(
            (
                request.tenant_key,
                request.agent_id,
                DataKind.KNOWLEDGE_INDEX.value,
                DataKind.KNOWLEDGE_INDEX.value,
            )
        )
        if not index_id:
            return KnowledgeQueryResult(())
        index_document = state.employee_documents[index_id]
        index_raw = self._data.service.read_blob(
            BlobRef.from_dict(index_document.blob_ref)
        ).decode("utf-8")
        indexed_pages = set(
            re.findall(r"\[\[([a-z0-9][a-z0-9_-]{0,127})\]\]", index_raw)
        )
        page_documents = [
            item
            for item in state.employee_documents.values()
            if item.tenant_key == request.tenant_key
            and item.agent_id == request.agent_id
            and item.kind is DataKind.KNOWLEDGE_PAGE
            and item.source_id in indexed_pages
            and not item.tombstoned
            and state.latest_employee_document.get(
                (item.tenant_key, item.agent_id, item.kind.value, item.source_id)
            )
            == item.document_id
        ]
        requested_sources = set(request.source_ids)
        terms = set(_tokens(request.query))
        hits: list[KnowledgeQueryHit] = []
        parsed_pages: dict[str, tuple[dict[str, object], str]] = {}
        for document in page_documents:
            if not self._authorize(request, document.source_id):
                raise PermissionError("knowledge source denied")
            raw = self._data.service.read_blob(BlobRef.from_dict(document.blob_ref))
            fields, body = parse_knowledge_page(raw.decode("utf-8"))
            source_ids = tuple(fields["source_ids"])
            if requested_sources and requested_sources.isdisjoint(source_ids):
                continue
            parsed_pages[str(fields["page_id"])] = (fields, body)
            score = len(terms & set(_tokens(str(fields["title"]) + " " + body)))
            if score:
                hits.append(
                    KnowledgeQueryHit(
                        page_id=str(fields["page_id"]),
                        title=str(fields["title"]),
                        excerpt=" ".join(body.split())[:500],
                        score=score,
                        citation=KnowledgeCitation(str(fields["page_id"]), source_ids),
                    )
                )
        linked = set()
        for hit in hits:
            _fields, body = parsed_pages[hit.page_id]
            linked.update(re.findall(r"\[\[([a-z0-9][a-z0-9_-]{0,127})\]\]", body))
        for page_id in sorted(linked):
            if page_id not in parsed_pages or any(hit.page_id == page_id for hit in hits):
                continue
            fields, body = parsed_pages[page_id]
            hits.append(
                KnowledgeQueryHit(
                    page_id=page_id,
                    title=str(fields["title"]),
                    excerpt=" ".join(body.split())[:500],
                    score=0,
                    citation=KnowledgeCitation(page_id, tuple(fields["source_ids"])),
                )
            )
        hits.sort(key=lambda item: (-item.score, item.page_id))
        return KnowledgeQueryResult(tuple(hits[: request.limit]))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w\u4e00-\u9fff]+", value.casefold()))


__all__ = ["EmployeeKnowledgeQuery", "KnowledgeAuthorizer", "parse_knowledge_page"]
