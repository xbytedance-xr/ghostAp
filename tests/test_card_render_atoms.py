"""Tests for src/card/render/atoms.py — RenderAtom and flatten_to_atoms."""

from src.card.render.atoms import RenderAtom, estimate_atom_size, flatten_to_atoms, invalidate_atom_handlers
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


class TestUnknownBlockKindPlaceholder:
    """Unknown block kinds should render a placeholder instead of silently skipping."""

    def test_unknown_kind_produces_warning_banner(self):
        """An unregistered block kind produces a warning_banner placeholder atom."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeBlock:
            kind: str = "totally_unknown_kind"
            block_id: str = "fake_1"
            content: str = "should not render"
            element_id: str | None = None
            status: str = "active"

        blocks = (FakeBlock(),)
        budget = RenderBudget()
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.kind == "warning_banner"
        assert atom.block_id == "fake_1"
        assert "部分内容暂时无法渲染" in atom.content
        assert atom.byte_size > 0

    def test_unknown_kind_mixed_with_known_blocks(self):
        """Unknown blocks produce placeholders without breaking known block rendering."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class WeirdBlock:
            kind: str = "alien_block"
            block_id: str = "weird_1"
            content: str = ""
            element_id: str | None = None
            status: str = "active"

        blocks = (
            ContentBlock(kind="text", block_id="b1", content="Hello"),
            WeirdBlock(),
            ContentBlock(kind="text", block_id="b2", content="World"),
        )
        budget = RenderBudget()
        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 3
        assert atoms[0].kind == "text"
        assert atoms[0].content == "Hello"
        assert atoms[1].kind == "warning_banner"
        assert atoms[1].block_id == "weird_1"
        assert atoms[2].kind == "text"
        assert atoms[2].content == "World"

    def test_unknown_kind_logs_warning(self, caplog):
        """Unknown block kind should emit a warning log."""
        import logging
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class MysteryBlock:
            kind: str = "mystery"
            block_id: str = "m1"
            content: str = ""
            element_id: str | None = None
            status: str = "active"

        blocks = (MysteryBlock(),)
        budget = RenderBudget()
        with caplog.at_level(logging.WARNING):
            flatten_to_atoms(blocks, budget)

        assert "unknown block kind" in caplog.text
        assert "mystery" in caplog.text


class TestEdgeCases:
    """Edge case tests for flatten_to_atoms."""

    def test_empty_blocks_returns_empty_list(self):
        """Empty tuple input returns empty list with no errors."""
        budget = RenderBudget()
        atoms = flatten_to_atoms((), budget)

        assert atoms == []
        assert isinstance(atoms, list)

    def test_oversized_content_block_budget_handling(self):
        """A single text block with byte_size exceeding budget still produces a valid atom.

        flatten_to_atoms does not truncate — it marks byte_size correctly,
        leaving truncation to the upstream pagination layer.
        """
        # Create a very large content block
        large_content = "x" * 100_000  # ~100KB, well over default 27KB budget
        blocks = (
            ContentBlock(kind="text", block_id="big", content=large_content),
        )
        budget = RenderBudget()  # default byte_budget=27*1024

        atoms = flatten_to_atoms(blocks, budget)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.kind == "text"
        assert atom.block_id == "big"
        assert atom.content == large_content
        # byte_size should be correctly calculated (larger than budget)
        assert atom.byte_size > budget.byte_budget
        # No exception raised — atoms layer does not truncate
        assert atom.splittable is True


class TestInvalidateAtomHandlers:
    """Tests for invalidate_atom_handlers()."""

    def test_invalidate_resets_cache_to_none(self):
        """After invalidate, _block_kind_handlers is None and rebuilds on next access."""
        import src.card.render.atoms as atoms_mod

        # Ensure cache is populated first
        blocks = (ContentBlock(kind="text", block_id="x", content="hi"),)
        flatten_to_atoms(blocks, RenderBudget())
        assert atoms_mod._block_kind_handlers is not None

        # Invalidate
        invalidate_atom_handlers()
        assert atoms_mod._block_kind_handlers is None

        # Next call rebuilds
        flatten_to_atoms(blocks, RenderBudget())
        assert atoms_mod._block_kind_handlers is not None


class TestAtomKindHandlers:
    """Tests for specific AtomKind handler dispatch in flatten_to_atoms."""

    def test_phase_banner_atom_kind_recognized(self):
        """phase_banner is a valid AtomKind and has a renderer."""
        from typing import get_args

        from src.card.render.atoms import AtomKind
        from src.card.render.renderer import _ATOM_RENDERERS

        kinds = set(get_args(AtomKind))
        assert "phase_banner" in kinds, "phase_banner must be in AtomKind"
        assert "phase_banner" in _ATOM_RENDERERS, "phase_banner must have a renderer"

        atom = RenderAtom(kind="phase_banner", content="🧠 Deep · 执行中 · 1m23s", node_count=1)
        atom.byte_size = estimate_atom_size(atom)
        assert atom.byte_size > 0

    def test_criteria_block_to_criteria_panel_atom(self):
        """criteria block → criteria_panel atom."""
        from src.card.state.models import CriteriaBlock

        blocks = (CriteriaBlock(block_id="c1", content="- [x] 标准1\n- [ ] 标准2"),)
        atoms = flatten_to_atoms(blocks, RenderBudget())

        assert len(atoms) == 1
        assert atoms[0].kind == "criteria_panel"
        assert atoms[0].block_id == "c1"
        assert "标准1" in atoms[0].content
        assert atoms[0].splittable is False

    def test_phase_block_to_phase_panel_atom(self):
        """phase block → phase_panel atom."""
        from src.card.state.models import PhaseBlock

        blocks = (PhaseBlock(block_id="p1", content="Build 阶段", phase_name="build"),)
        atoms = flatten_to_atoms(blocks, RenderBudget())

        assert len(atoms) == 1
        assert atoms[0].kind == "phase_panel"
        assert atoms[0].block_id == "p1"
        assert "Build" in atoms[0].content
        assert atoms[0].splittable is False

    def test_worktree_block_to_worktree_panel_atom(self):
        """worktree_units block → worktree_panel atom."""
        from src.card.state.models import WorktreeUnitsBlock

        blocks = (WorktreeUnitsBlock(block_id="wt1", content="进度信息"),)
        atoms = flatten_to_atoms(blocks, RenderBudget())

        assert len(atoms) == 1
        assert atoms[0].kind == "worktree_panel"
        assert atoms[0].block_id == "wt1"
        assert atoms[0].splittable is False

    def test_engine_cmd_in_unknown_block_placeholder(self):
        """Unknown block kind placeholder shows load error text."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class AlienBlock:
            kind: str = "alien_xyz"
            block_id: str = "a1"
            content: str = ""
            element_id: str | None = None
            status: str = "active"

        budget = RenderBudget(engine_cmd="/deep")
        atoms = flatten_to_atoms((AlienBlock(),), budget)

        assert len(atoms) == 1
        assert atoms[0].kind == "warning_banner"
        assert "部分内容暂时无法渲染" in atoms[0].content
