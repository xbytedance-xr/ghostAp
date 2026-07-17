"""Committed employee Wiki integrity lint."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..data.models import DataKind
from ..journal.blob_store import BlobRef
from .models import KnowledgeLintIssue, KnowledgeLintReport
from .query import parse_knowledge_page


def lint_employee_knowledge(
    *,
    data_composition: object,
    tenant_key: str,
    agent_id: str,
    agents_root: str | Path | None = None,
) -> KnowledgeLintReport:
    state = data_composition.service.rebuild_projection()
    issues: list[KnowledgeLintIssue] = []
    pages: dict[str, tuple[dict[str, object], str]] = {}
    source_hashes: dict[str, str] = {}
    index_body = ""
    history_hashes = {
        item.record_id: str(item.blob_ref.get("payload_hash", ""))
        for item in state.history_records.values()
        if item.tenant_key == tenant_key
        and item.agent_id == agent_id
        and not item.tombstoned
    }
    for document in state.employee_documents.values():
        key = (document.tenant_key, document.agent_id, document.kind.value, document.source_id)
        if (
            document.tenant_key != tenant_key
            or document.agent_id != agent_id
            or document.tombstoned
            or state.latest_employee_document.get(key) != document.document_id
        ):
            continue
        if document.kind not in {DataKind.KNOWLEDGE_PAGE, DataKind.KNOWLEDGE_INDEX}:
            continue
        raw = data_composition.service.read_blob(BlobRef.from_dict(document.blob_ref))
        if hashlib.sha256(raw).hexdigest() != document.content_hash:
            issues.append(KnowledgeLintIssue("hash_mismatch", document.source_id))
            continue
        if document.kind is DataKind.KNOWLEDGE_INDEX:
            index_body = raw.decode("utf-8")
            continue
        try:
            fields, body = parse_knowledge_page(raw.decode("utf-8"))
        except Exception:
            issues.append(KnowledgeLintIssue("frontmatter_schema", document.source_id))
            continue
        page_id = str(fields["page_id"])
        if page_id in pages:
            issues.append(KnowledgeLintIssue("duplicate_page_id", page_id))
        pages[page_id] = (fields, body)
        for source_id, source_hash in zip(
            fields["source_ids"], fields["source_hashes"], strict=False
        ):
            expected_hash = history_hashes.get(str(source_id))
            if expected_hash is None:
                issues.append(KnowledgeLintIssue("missing_source", page_id, str(source_id)))
            elif expected_hash != source_hash:
                issues.append(
                    KnowledgeLintIssue("source_hash_mismatch", page_id, str(source_id))
                )
            previous = source_hashes.setdefault(str(source_id), str(source_hash))
            if previous != source_hash:
                issues.append(KnowledgeLintIssue("contradiction", page_id, str(source_id)))
    for page_id, (fields, body) in pages.items():
        for linked in re.findall(r"\[\[([^\]]+)\]\]", body):
            if linked not in pages:
                issues.append(KnowledgeLintIssue("broken_wikilink", page_id, linked))
        if page_id not in index_body:
            issues.append(KnowledgeLintIssue("orphan_page", page_id))
        if not fields["source_ids"] or len(fields["source_ids"]) != len(fields["source_hashes"]):
            issues.append(KnowledgeLintIssue("missing_source", page_id))
    if pages and not index_body:
        issues.append(KnowledgeLintIssue("stale_index"))
    elif pages:
        indexed = set(re.findall(r"\[\[([^\]]+)\]\]", index_body))
        if indexed != set(pages):
            issues.append(KnowledgeLintIssue("stale_index"))
    if agents_root is not None:
        agents = Path(agents_root) / agent_id / "workspace" / "AGENTS.md"
        if agents.exists() and agents.stat().st_size > 8192:
            issues.append(KnowledgeLintIssue("agents_too_large"))
    return KnowledgeLintReport(tenant_key, agent_id, tuple(issues))


__all__ = ["lint_employee_knowledge"]
