from __future__ import annotations

from src.autonomous.data.models import DataKind
from src.autonomous.data.ports import PublishEmployeeDocumentCommand
from src.autonomous.knowledge import (
    DeterministicKnowledgeCompiler,
    KnowledgeClaim,
    KnowledgeCompilation,
    KnowledgePageDraft,
    KnowledgeSource,
)
from src.autonomous.knowledge.query import parse_knowledge_page
from tests.autonomous.knowledge_helpers import (
    build_knowledge_composition,
    close_knowledge_composition,
    seed_terminal,
)


class _CountingCompiler(DeterministicKnowledgeCompiler):
    def __init__(self) -> None:
        self.calls = 0

    def compile(self, source: KnowledgeSource) -> KnowledgeCompilation:
        self.calls += 1
        return super().compile(source)


def test_same_source_hash_is_idempotent_and_changed_source_advances_generation(
    tmp_path,
) -> None:
    writer, composition = build_knowledge_composition(tmp_path)
    compiler = _CountingCompiler()
    service = composition.knowledge_service
    service._compiler = compiler  # noqa: SLF001
    first = seed_terminal(composition, "one", "Use bounded retries.\nVerify the result.")
    first_id = service.enqueue_terminal(first)
    assert service.enqueue_terminal(first) == first_id
    service.drain()
    assert compiler.calls == 1

    second = seed_terminal(composition, "two", "Use a checkpoint before retrying.")
    service.enqueue_terminal(second)
    service.drain()
    third = seed_terminal(composition, "three", "Record the source before publishing.")
    service.enqueue_terminal(third)
    service.drain()
    assert compiler.calls == 3
    pages = [
        item
        for item in composition.state.employee_documents.values()
        if item.kind is DataKind.KNOWLEDGE_PAGE
    ]
    assert sorted(item.version for item in pages) == [1, 1, 1]
    generations = []
    for page in pages:
        raw = composition.document_materializer.resolve_path(
            "agt_alpha", DataKind.KNOWLEDGE_PAGE, page.source_id
        ).absolute.read_text(encoding="utf-8")
        fields, _body = parse_knowledge_page(raw)
        generations.append(fields["knowledge_generation"])
    assert sorted(generations) == [1, 2, 3]
    index = [
        item
        for item in composition.state.employee_documents.values()
        if item.kind is DataKind.KNOWLEDGE_INDEX
    ]
    assert [item.version for item in index] == [1, 2, 3]
    close_knowledge_composition(writer, composition)


def test_wiki_lint_detects_broken_wikilink(tmp_path) -> None:
    writer, composition = build_knowledge_composition(tmp_path)

    class _BrokenLinkCompiler:
        def compile(self, source):
            return KnowledgeCompilation(
                (
                    KnowledgePageDraft(
                        page_id="linked-page",
                        title="Linked page",
                        kind="decision",
                        claims=(KnowledgeClaim("A source-linked decision.", (source.source_id,)),),
                        links=("missing-page",),
                    ),
                )
            )

    service = composition.knowledge_service
    service._compiler = _BrokenLinkCompiler()  # noqa: SLF001
    terminal = seed_terminal(composition, "lint", "A source-linked decision.")
    service.enqueue_terminal(terminal)
    service.drain()
    report = service.lint("tenant_1", "agt_alpha")
    assert "broken_wikilink" in {issue.code for issue in report.issues}
    close_knowledge_composition(writer, composition)


def test_wiki_lint_detects_schema_duplicate_orphan_source_and_size_failures(
    tmp_path,
) -> None:
    writer, composition = build_knowledge_composition(tmp_path)
    service = composition.knowledge_service
    terminal = seed_terminal(composition, "lint-matrix", "Verified reusable finding.")
    service.enqueue_terminal(terminal)
    service.drain()
    page = next(
        item
        for item in composition.state.employee_documents.values()
        if item.kind is DataKind.KNOWLEDGE_PAGE
    )
    original_path = composition.document_materializer.resolve_path(
        "agt_alpha", DataKind.KNOWLEDGE_PAGE, page.source_id
    ).absolute
    original = original_path.read_text(encoding="utf-8")
    history = next(iter(composition.state.history_records.values()))
    source_hash = str(history.blob_ref["payload_hash"])
    common = dict(
        agent_id="agt_alpha",
        tenant_key="tenant_1",
        owner_principal_id="ou_owner",
        kind=DataKind.KNOWLEDGE_PAGE,
        content_type="text/markdown",
    )
    composition.publish_document(
        PublishEmployeeDocumentCommand(
            **common,
            source_id="duplicate-source",
            content=original.encode(),
            idempotency_key="duplicate",
        )
    )
    orphan = original.replace(page.source_id, "orphan-page").replace(
        source_hash, "f" * 64
    )
    composition.publish_document(
        PublishEmployeeDocumentCommand(
            **common,
            source_id="orphan-page",
            content=orphan.encode(),
            idempotency_key="orphan",
        )
    )
    composition.publish_document(
        PublishEmployeeDocumentCommand(
            **common,
            source_id="malformed-page",
            content=b"# missing frontmatter\n",
            idempotency_key="malformed",
        )
    )
    agents = tmp_path / "agents" / "agt_alpha" / "workspace" / "AGENTS.md"
    agents.write_text("x" * 8193, encoding="utf-8")
    report = service.lint("tenant_1", "agt_alpha")
    codes = {issue.code for issue in report.issues}
    assert {
        "duplicate_page_id",
        "orphan_page",
        "source_hash_mismatch",
        "frontmatter_schema",
        "stale_index",
        "agents_too_large",
    } <= codes
    close_knowledge_composition(writer, composition)
