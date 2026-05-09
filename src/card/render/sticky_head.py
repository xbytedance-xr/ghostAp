"""sticky_head builder: phase_banner + task_list compact + activity_summary compact."""

from __future__ import annotations

from src.card.render.atoms import RenderAtom, estimate_atom_size
from src.card.render.banner_computer import compute_banner
from src.card.render.task_list import render_task_list_panel
from src.card.render.tools import render_activity_summary_panel
from src.card.state.models import CardMetadata, CardState

STICKY_HEAD_MAX_NODES = 25


def build_sticky_head(
    state: CardState,
    metadata: CardMetadata,
    *,
    _force_total_nodes: int | None = None,
) -> tuple[RenderAtom, ...]:
    """Build the sandwich anchor reused on every page.

    Returns phase_banner, then optional task_list and activity_summary. When the
    sticky head exceeds the hard node cap, right-most optional atoms are dropped.
    """
    atoms: list[RenderAtom] = []

    runtime = getattr(state, "runtime_stats", None)
    banner_text = compute_banner(metadata, runtime) if runtime is not None else "🤖 Programming · 进行中 · 0s"
    banner_atom = RenderAtom(
        kind="phase_banner",
        content=banner_text,
        node_count=1,
        block_id="_phase_banner",
    )
    banner_atom.byte_size = estimate_atom_size(banner_atom)
    atoms.append(banner_atom)

    task_list = getattr(state, "task_list", None)
    if task_list is None:
        task_list = next((b for b in getattr(state, "blocks", ()) if getattr(b, "kind", "") == "task_list"), None)
    if task_list is not None and getattr(task_list, "tasks", None):
        panel = _render_task_list_compact(task_list)
        if panel is not None:
            tl_atom = RenderAtom(
                kind="task_list",
                elements=[panel],
                node_count=8,
                block_id="_sticky_task_list",
                content="",
            )
            tl_atom.byte_size = estimate_atom_size(tl_atom)
            atoms.append(tl_atom)

    activity = getattr(state, "activity", None)
    blocks = getattr(state, "blocks", ())
    has_activity = bool(getattr(activity, "has_data", False)) or any(getattr(b, "kind", "") == "tool_call" for b in blocks)
    if has_activity:
        panel = _render_activity_summary_compact(blocks)
        if panel is not None:
            act_atom = RenderAtom(
                kind="activity_summary",
                elements=[panel],
                node_count=4,
                block_id="_sticky_activity",
                content="",
            )
            act_atom.byte_size = estimate_atom_size(act_atom)
            atoms.append(act_atom)

    total = _force_total_nodes if _force_total_nodes is not None else sum(a.node_count for a in atoms)
    while total > STICKY_HEAD_MAX_NODES and len(atoms) > 1:
        atoms.pop()
        total = sum(a.node_count for a in atoms)

    return tuple(atoms)


def _render_task_list_compact(task_list) -> dict | None:
    """Render task list in compact mode, tolerating older renderer signature."""
    try:
        return render_task_list_panel(task_list, compact=True)
    except TypeError:
        return render_task_list_panel(task_list)


def _render_activity_summary_compact(blocks) -> dict | None:
    """Render activity summary in compact mode, tolerating older renderer signature."""
    try:
        return render_activity_summary_panel(blocks, compact=True)
    except TypeError:
        return render_activity_summary_panel(blocks)
