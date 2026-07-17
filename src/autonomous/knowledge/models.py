"""Frozen schemas for source-linked employee knowledge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class KnowledgeConfidence(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    source_id: str
    source_hash: str
    tenant_key: str
    agent_id: str
    owner_principal_id: str
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeClaim:
    text: str
    source_ids: tuple[str, ...]
    confidence: KnowledgeConfidence = KnowledgeConfidence.OBSERVED

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        if not self.text or len(self.text) > 2_000:
            raise ValueError("invalid knowledge claim")
        if not self.source_ids or any(not item for item in self.source_ids):
            raise ValueError("knowledge claim requires sources")


@dataclass(frozen=True, slots=True)
class KnowledgePageDraft:
    page_id: str
    title: str
    kind: str
    claims: tuple[KnowledgeClaim, ...]
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", self.page_id) is None:
            raise ValueError("invalid knowledge page id")
        if not self.title or len(self.title) > 200 or not self.claims:
            raise ValueError("invalid knowledge page draft")
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "links", tuple(self.links))


@dataclass(frozen=True, slots=True)
class KnowledgeCompilation:
    pages: tuple[KnowledgePageDraft, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "pages", tuple(self.pages))
        if not self.pages:
            raise ValueError("knowledge compilation is empty")


@dataclass(frozen=True, slots=True)
class AuthorizedKnowledgeQuery:
    tenant_key: str
    agent_id: str
    requester_principal_id: str
    query: str
    source_ids: tuple[str, ...] = ()
    limit: int = 5

    def __post_init__(self) -> None:
        if not all((self.tenant_key, self.agent_id, self.requester_principal_id)):
            raise ValueError("knowledge query authority is required")
        if not self.query or not 1 <= self.limit <= 20:
            raise ValueError("invalid knowledge query")
        object.__setattr__(self, "source_ids", tuple(self.source_ids))


@dataclass(frozen=True, slots=True)
class KnowledgeCitation:
    page_id: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeQueryHit:
    page_id: str
    title: str
    excerpt: str
    score: int
    citation: KnowledgeCitation


@dataclass(frozen=True, slots=True)
class KnowledgeQueryResult:
    hits: tuple[KnowledgeQueryHit, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeLintIssue:
    code: str
    page_id: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgeLintReport:
    tenant_key: str
    agent_id: str
    issues: tuple[KnowledgeLintIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


__all__ = [
    "AuthorizedKnowledgeQuery",
    "KnowledgeCitation",
    "KnowledgeClaim",
    "KnowledgeCompilation",
    "KnowledgeConfidence",
    "KnowledgeLintIssue",
    "KnowledgeLintReport",
    "KnowledgePageDraft",
    "KnowledgeQueryHit",
    "KnowledgeQueryResult",
    "KnowledgeSource",
]
