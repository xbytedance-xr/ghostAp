from __future__ import annotations

from src.autonomous.knowledge import DeterministicKnowledgeCompiler, EmployeeKnowledgeService
from tests.autonomous.knowledge_helpers import (
    build_knowledge_composition,
    close_knowledge_composition,
    seed_terminal,
)


class _CountingCompiler(DeterministicKnowledgeCompiler):
    def __init__(self) -> None:
        self.calls = 0

    def compile(self, source):
        self.calls += 1
        return super().compile(source)


class _BrokenCompiler:
    def compile(self, _source):
        raise RuntimeError("broken")


def test_anchored_ingest_queue_resumes_once_after_restart(tmp_path) -> None:
    writer, composition = build_knowledge_composition(tmp_path)
    original = composition.knowledge_service
    original._ensure_thread = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    terminal = seed_terminal(composition, "restart", "Recover this durable finding.")
    original.enqueue_terminal(terminal)
    original._closed = True  # noqa: SLF001

    compiler = _CountingCompiler()
    recovered = EmployeeKnowledgeService(
        writer=writer,
        data_composition=composition,
        authorizer=lambda _request, _source: True,
        compiler=compiler,
        agents_root=tmp_path / "agents",
    )
    composition.knowledge_service = recovered
    assert recovered.recover() == 1
    recovered.drain()
    assert compiler.calls == 1
    assert recovered.recover() == 0
    close_knowledge_composition(writer, composition)


def test_review_item_retry_is_anchored_and_recompiles_same_source(tmp_path) -> None:
    writer, composition = build_knowledge_composition(tmp_path)
    service = composition.knowledge_service
    service._compiler = _BrokenCompiler()  # noqa: SLF001
    ingest_id = service.enqueue_terminal(
        seed_terminal(composition, "review", "Retry this durable finding.")
    )
    service.drain()
    service._compiler = DeterministicKnowledgeCompiler()  # noqa: SLF001

    assert service.retry_review_item(
        ingest_id,
        tenant_key="tenant_1",
        agent_id="agt_alpha",
    ) == ingest_id
    service.drain()
    assert service.recover() == 0
    assert any(
        event.event_type == "knowledge.ingest.retry_requested"
        for frame in writer.replay()
        for event in frame.events
    )
    close_knowledge_composition(writer, composition)
