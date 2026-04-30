"""Tests for src/card/render/atoms.py — RenderAtom and flatten_to_atoms."""

from src.card.render.atoms import RenderAtom, estimate_atom_size, flatten_to_atoms
from src.card.render.budget import RenderBudget
from src.card.state.models import ContentBlock


class TestFlattenToAtoms:
    """Tests for flatten_to_atoms function."""

    def test_text_block_to_atom(self) -> None:
        """text ContentBlock → atom with kind='text', splittable=True."""
        blocks = (
            ContentBlock(kind="text", block_id="b1", content="Hello world"),
        )
        budget = RenderBudget()
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.kind == "text"
        assert atom.splittable is True
        assert atom.block_id == "b1"
        assert atom.content == "Hello world"

    def test_tool_block_to_atom(self) -> None:
        """tool ContentBlock → atom with kind='tool_panel'."""
        blocks = (
            ContentBlock(
                kind="tool_call",
                block_id="t1",
                status="active",
                tool_name="read_file",
                tool_summary="Reading config",
            ),
        )
        budget = RenderBudget()
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.kind == "tool_panel"
        assert atom.splittable is False
        assert atom.block_id == "t1"
        assert "read_file" in atom.content

    def test_reasoning_block_to_atom(self) -> None:
        """reasoning → atom with kind='reasoning'."""
        blocks = (
            ContentBlock(kind="reasoning", block_id="r1", content="thinking..."),
        )
        budget = RenderBudget()
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.kind == "reasoning"
        assert atom.splittable is False
        assert atom.content == "thinking..."

    def test_tool_history_fold(self) -> None:
        """≥3 completed tools → single tool_history atom."""
        blocks = tuple(
            ContentBlock(
                kind="tool_call",
                block_id=f"t{i}",
                status="completed",
                tool_name=f"tool_{i}",
                tool_summary=f"done_{i}",
            )
            for i in range(4)
        )
        budget = RenderBudget(tool_history_fold_threshold=3)
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.kind == "tool_history"
        assert "tool_0" in atom.content
        assert "tool_3" in atom.content

    def test_tool_history_no_fold(self) -> None:
        """≤2 completed tools → individual atoms."""
        blocks = tuple(
            ContentBlock(
                kind="tool_call",
                block_id=f"t{i}",
                status="completed",
                tool_name=f"tool_{i}",
                tool_summary=f"done_{i}",
            )
            for i in range(2)
        )
        budget = RenderBudget(tool_history_fold_threshold=3)
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 2
        assert all(a.kind == "tool_panel" for a in atoms)

    def test_active_tool_never_folded(self) -> None:
        """Active tool stays independent even among completed tools."""
        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", status="completed", tool_name="a"),
            ContentBlock(kind="tool_call", block_id="t2", status="completed", tool_name="b"),
            ContentBlock(kind="tool_call", block_id="t3", status="completed", tool_name="c"),
            ContentBlock(kind="tool_call", block_id="t4", status="active", tool_name="running"),
        )
        budget = RenderBudget(tool_history_fold_threshold=3)
        atoms = flatten_to_atoms(blocks, budget)

        # First 3 completed → folded into 1 tool_history
        # Last active → separate tool_panel
        assert len(atoms) == 2
        assert atoms[0].kind == "tool_history"
        assert atoms[1].kind == "tool_panel"
        assert "running" in atoms[1].content

    def test_estimate_atom_size(self) -> None:
        """estimate > 0, reasonable range."""
        # Atom with content
        atom = RenderAtom(kind="text", content="Hello world", splittable=True)
        size = estimate_atom_size(atom)
        assert size > 0
        # Content is 11 bytes → estimate should be 11*3 + 100 = 133
        assert size == len("Hello world".encode("utf-8")) * 3 + 100

        # Atom with elements
        atom_with_elements = RenderAtom(
            kind="text",
            elements=[{"tag": "markdown", "content": "hello"}],
        )
        size2 = estimate_atom_size(atom_with_elements)
        assert size2 > 0
        # Should equal actual JSON serialization size
        import json
        expected = len(json.dumps(atom_with_elements.elements).encode("utf-8"))
        assert size2 == expected
