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
        appendix=(_atom("activity_digest", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=0, total_pages=2, body_slice=(layout.body[0],))
    assert [a.content for a in page] == ["BAN", "PROG", "BODY1"]


def test_assemble_middle_page_no_status_no_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "B1"), _atom("text", "B2"), _atom("text", "B3")),
        appendix=(_atom("activity_digest", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=1, total_pages=3, body_slice=(layout.body[1],))
    assert [a.content for a in page] == ["BAN", "B2"]


def test_assemble_last_page_includes_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(),
        body=(_atom("text", "B"),),
        appendix=(_atom("activity_digest", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=1, total_pages=2, body_slice=(layout.body[0],))
    assert [a.content for a in page] == ["BAN", "B", "HIST"]


def test_assemble_single_page_includes_status_and_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "B"),),
        appendix=(_atom("activity_digest", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=0, total_pages=1, body_slice=layout.body)
    assert [a.content for a in page] == ["BAN", "PROG", "B", "HIST"]


def test_empty_sticky_head_does_not_crash():
    layout = SectionLayout(sticky_head=(), status=(), body=(_atom("text", "B"),), appendix=())
    page = layout.assemble_for_page(page_idx=0, total_pages=1, body_slice=layout.body)
    assert [a.content for a in page] == ["B"]


def test_paginate_layout_single_page_when_under_budget():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "B"),),
        status=(),
        body=(_atom("text", "x" * 100),),
        appendix=(),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) == 1
    assert pages[0][0].kind == "phase_banner"
    assert pages[0][1].kind == "text"


def test_paginate_layout_multiple_pages_repeats_sticky():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    big_atom_a = _atom("text", "a" * 9000)
    big_atom_b = _atom("text", "b" * 9000)
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "STICKY"),),
        status=(),
        body=(big_atom_a, big_atom_b),
        appendix=(),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) >= 2
    for page in pages:
        assert any(a.kind == "phase_banner" for a in page), "sticky_head must be present on every page"


def test_paginate_layout_appendix_only_on_last_page():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    big_a = _atom("text", "a" * 9000)
    big_b = _atom("text", "b" * 9000)
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "S"),),
        status=(),
        body=(big_a, big_b),
        appendix=(_atom("activity_digest", "HIST"),),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) >= 2
    last_kinds = [a.kind for a in pages[-1]]
    earlier_kinds = [a.kind for p in pages[:-1] for a in p]
    assert "activity_digest" in last_kinds
    assert "activity_digest" not in earlier_kinds


def test_paginate_layout_status_only_on_first_page():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    big_a = _atom("text", "a" * 9000)
    big_b = _atom("text", "b" * 9000)
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "S"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(big_a, big_b),
        appendix=(),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) >= 2
    assert any(a.kind == "progress_bar" for a in pages[0])
    for p in pages[1:]:
        assert not any(a.kind == "progress_bar" for a in p)
