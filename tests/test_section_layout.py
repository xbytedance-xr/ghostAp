"""SectionLayout model tests."""
from __future__ import annotations

from src.card.render.atoms import RenderAtom
from src.card.render.layout import SectionLayout


def _atom(kind: str, content: str = "x", nodes: int = 1) -> RenderAtom:
    return RenderAtom(kind=kind, content=content, node_count=nodes)  # type: ignore[arg-type]


def test_assemble_first_page_includes_status():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "BODY1"), _atom("text", "BODY2")),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=0, total_pages=2, body_slice=(layout.body[0],))
    assert [a.content for a in page] == ["BAN", "PROG", "BODY1"]


def test_assemble_middle_page_no_status_no_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "B1"), _atom("text", "B2"), _atom("text", "B3")),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=1, total_pages=3, body_slice=(layout.body[1],))
    assert [a.content for a in page] == ["BAN", "B2"]


def test_assemble_last_page_includes_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(),
        body=(_atom("text", "B"),),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=1, total_pages=2, body_slice=(layout.body[0],))
    assert [a.content for a in page] == ["BAN", "B", "HIST"]


def test_assemble_single_page_includes_status_and_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "B"),),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=0, total_pages=1, body_slice=layout.body)
    assert [a.content for a in page] == ["BAN", "PROG", "B", "HIST"]


def test_empty_sticky_head_does_not_crash():
    layout = SectionLayout(sticky_head=(), status=(), body=(_atom("text", "B"),), appendix=())
    page = layout.assemble_for_page(page_idx=0, total_pages=1, body_slice=layout.body)
    assert [a.content for a in page] == ["B"]
