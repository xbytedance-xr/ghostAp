"""Main card render entry point: state → RenderedCard list."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from src.card.render.atoms import RenderAtom, flatten_to_atoms
from src.card.render.budget import RenderBudget
from src.card.render.buttons import render_buttons
from src.card.render.footer import render_footer
from src.card.render.header import render_header
from src.card.render.pagination import paginate_atoms
from src.card.render.plan import render_plan_panel
from src.card.render.reasoning import render_reasoning_panel
from src.card.render.tools import render_tool_history_panel, render_tool_panel
from src.card.state.models import CardState, ContentBlock


@dataclass(frozen=True)
class ActiveElement:
    """Points to the streaming element that can be updated via element_content API."""

    element_id: str
    text: str


@dataclass(frozen=True)
class RenderedCard:
    """Output of render_card(): one per page."""

    card_json: dict = field(default_factory=dict)
    structure_signature: str = ""
    active_element: ActiveElement | None = None
    page_index: int = 0
    total_pages: int = 1


def render_card(
    state: CardState, budget: RenderBudget | None = None
) -> list[RenderedCard]:
    """Main entry: CardState → list[RenderedCard].

    Pipeline: flatten_to_atoms → paginate → assemble pages.
    Each page is a complete Feishu Schema 2.0 card JSON.
    """
    if budget is None:
        budget = RenderBudget()

    # 1. Flatten blocks to atoms
    atoms = flatten_to_atoms(state.blocks, budget)

    # 2. Paginate
    pages = paginate_atoms(atoms, budget)
    total_pages = len(pages)

    # 3. Compute structure signature (stable across text deltas)
    signature = compute_structure_signature(state)

    # 4. Assemble each page
    results: list[RenderedCard] = []
    for page_idx, page_atoms in enumerate(pages):
        # Render body elements from atoms
        body_elements = _render_atoms_to_elements(page_atoms, state, budget)

        # Append footer and buttons only on the last page
        if page_idx == total_pages - 1:
            body_elements.extend(render_footer(state))
            body_elements.extend(render_buttons(state))

        # Detect active element for streaming
        active_element = _find_active_element(page_atoms, state)

        # Determine streaming mode
        has_active_text = active_element is not None
        is_running = state.terminal == "running"
        streaming = has_active_text and is_running

        # Build the full card JSON
        card_json = _assemble_card_json(
            state=state,
            body_elements=body_elements,
            streaming=streaming,
            active_element=active_element,
        )

        results.append(
            RenderedCard(
                card_json=card_json,
                structure_signature=signature,
                active_element=active_element,
                page_index=page_idx,
                total_pages=total_pages,
            )
        )

    return results


def compute_structure_signature(state: CardState) -> str:
    """Compute MD5 of structural parts of the card.

    Structural = block kinds + statuses + tool names + terminal state.
    Excludes active text content (which changes via element_content streaming).
    This allows the delivery layer to skip full card updates when only text changed.
    """
    parts: list[str] = []
    for block in state.blocks:
        parts.append(f"{block.kind}:{block.block_id}:{block.status}")
        if block.kind == "tool_call":
            parts.append(f"tn:{block.tool_name}")
        if block.kind == "plan":
            parts.append(f"pc:{block.content}")
        elif block.kind == "reasoning":
            parts.append(f"rc:{block.content}")
    parts.append(f"terminal:{state.terminal}")
    parts.append(f"header:{state.header.title}:{state.header.template}")
    if state.header.subtitle is not None:
        parts.append(f"sub:{state.header.subtitle}")
    if state.footer.status is not None:
        parts.append(f"footer:{state.footer.status}")
    parts.append(f"buttons:{len(state.buttons)}")
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_atoms_to_elements(
    atoms: list[RenderAtom], state: CardState, budget: RenderBudget
) -> list[dict]:
    """Convert RenderAtoms to Feishu Schema 2.0 elements."""
    elements: list[dict] = []

    for atom in atoms:
        if atom.kind == "text":
            el = _render_text_element(atom, state)
            elements.append(el)
        elif atom.kind == "tool_panel":
            block = _find_block_by_id(state, atom.block_id)
            if block is not None:
                elements.append(render_tool_panel(block))
            else:
                # Fallback: render as markdown from atom content
                elements.append({"tag": "markdown", "content": atom.content})
        elif atom.kind == "tool_history":
            blocks = _find_tool_history_blocks(state, atom.block_id)
            if blocks:
                elements.append(render_tool_history_panel(blocks))
            else:
                elements.append({"tag": "markdown", "content": atom.content})
        elif atom.kind == "reasoning":
            block = _find_block_by_id(state, atom.block_id)
            if block is not None:
                elements.append(render_reasoning_panel(block, budget))
            else:
                elements.append({"tag": "markdown", "content": atom.content})
        elif atom.kind == "plan":
            block = _find_block_by_id(state, atom.block_id)
            if block is not None:
                elements.append(render_plan_panel(block))
            else:
                elements.append({"tag": "markdown", "content": atom.content})

    return elements


def _render_text_element(atom: RenderAtom, state: CardState) -> dict:
    """Render a text atom. If it's the active block, assign element_id for streaming."""
    block = _find_block_by_id(state, atom.block_id)
    element_id = None
    if block is not None and block.element_id and block.status == "active":
        element_id = block.element_id

    el: dict = {"tag": "markdown", "content": atom.content}
    if element_id:
        el["element_id"] = element_id
    return el


def _find_active_element(
    atoms: list[RenderAtom], state: CardState
) -> ActiveElement | None:
    """Find the active streaming text element on this page."""
    for atom in atoms:
        if atom.kind != "text":
            continue
        block = _find_block_by_id(state, atom.block_id)
        if block is not None and block.status == "active" and block.element_id:
            return ActiveElement(element_id=block.element_id, text=atom.content)
    return None


def _find_block_by_id(state: CardState, block_id: str) -> ContentBlock | None:
    """Find a ContentBlock by block_id."""
    for block in state.blocks:
        if block.block_id == block_id:
            return block
    return None


def _find_tool_history_blocks(
    state: CardState, first_block_id: str
) -> list[ContentBlock]:
    """Find consecutive completed tool_call blocks starting from first_block_id."""
    blocks: list[ContentBlock] = []
    found = False
    for block in state.blocks:
        if block.block_id == first_block_id:
            found = True
        if found:
            if block.kind == "tool_call" and block.status == "completed":
                blocks.append(block)
            else:
                if blocks:
                    break
    return blocks


def _assemble_card_json(
    state: CardState,
    body_elements: list[dict],
    streaming: bool,
    active_element: ActiveElement | None,
) -> dict:
    """Assemble a complete Feishu Schema 2.0 card JSON."""
    card: dict = {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": render_header(state),
        "body": {"elements": body_elements},
    }

    if streaming:
        card["config"]["streaming_mode"] = True

    return card
