"""Two-stage, schema-bound knowledge compilation helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

from .models import (
    KnowledgeClaim,
    KnowledgeCompilation,
    KnowledgePageDraft,
    KnowledgeSource,
)

_SECRET = re.compile(
    r"(?i)(?:app[_-]?secret|api[_-]?key|password|access[_-]?token|credential[_-]?ref)\s*[:=]\s*\S+"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_HIDDEN = re.compile(r"(?is)<(?:think|analysis)>.*?</(?:think|analysis)>")
_INJECTION = re.compile(
    r"(?im)^\s*(?:ignore (?:all |previous )?instructions|system prompt|developer message)\b.*$"
)


class KnowledgeCompilerPort(Protocol):
    def compile(self, source: KnowledgeSource) -> KnowledgeCompilation: ...


def redact_knowledge_source(text: str) -> str:
    value = _HIDDEN.sub("[redacted-hidden-reasoning]", text)
    value = _SECRET.sub("[redacted-secret]", value)
    value = _EMAIL.sub("[redacted-pii]", value)
    value = _INJECTION.sub("[ignored-untrusted-instruction]", value)
    return value


class DeterministicKnowledgeCompiler:
    """Safe default compiler; production may inject an LLM implementation."""

    def compile(self, source: KnowledgeSource) -> KnowledgeCompilation:
        clean = redact_knowledge_source(source.text)
        lines = [
            line.strip(" -*\t")
            for line in clean.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        claims = tuple(
            KnowledgeClaim(line[:2_000], (source.source_id,))
            for line in lines[:8]
            if len(line) >= 3
        )
        if not claims:
            claims = (KnowledgeClaim("Task completed without reusable textual findings.", (source.source_id,)),)
        page_id = "source-" + hashlib.sha256(source.source_id.encode()).hexdigest()[:16]
        return KnowledgeCompilation(
            (
                KnowledgePageDraft(
                    page_id=page_id,
                    title=f"Learning from {source.source_id}",
                    kind="task_learning",
                    claims=claims,
                ),
            )
        )


__all__ = [
    "DeterministicKnowledgeCompiler",
    "KnowledgeCompilerPort",
    "redact_knowledge_source",
]
