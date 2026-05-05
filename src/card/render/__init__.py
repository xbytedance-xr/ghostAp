"""Pure render functions for card components.

Public API:
- render_card(state, budget) → list[RenderedCard]
- compute_structure_signature(state) → str
- RenderBudget, RenderedCard, ActiveElement
"""

from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card, compute_structure_signature
from src.card.types import ActiveElement, RenderedCard

__all__ = [
    "RenderBudget",
    "RenderedCard",
    "ActiveElement",
    "render_card",
    "compute_structure_signature",
]
