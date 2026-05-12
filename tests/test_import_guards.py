"""Verify that public __all__ symbols in key modules are importable."""

import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize("module_path", [
    "src.card.events",
    "src.card.session",
])
def test_all_symbols_importable(module_path: str) -> None:
    """Every name listed in __all__ must be importable from the module."""
    mod = importlib.import_module(module_path)
    all_names = getattr(mod, "__all__", None)
    assert all_names is not None, f"{module_path} has no __all__"
    assert len(all_names) > 0, f"{module_path}.__all__ is empty"

    missing: list[str] = []
    for name in all_names:
        if not hasattr(mod, name):
            missing.append(name)

    assert not missing, f"{module_path} is missing: {missing}"


@pytest.mark.parametrize("module_path", [
    "src.card.events",
    "src.card.session",
])
def test_all_no_duplicates(module_path: str) -> None:
    """__all__ should not contain duplicate entries."""
    mod = importlib.import_module(module_path)
    all_names = getattr(mod, "__all__", [])
    duplicates = [n for n in all_names if all_names.count(n) > 1]
    assert not duplicates, f"{module_path}.__all__ has duplicates: {set(duplicates)}"


def test_acp_manager_idle_health_calls_typed_telemetry_entry_without_type_ignores() -> None:
    """Task 25 guard: idle-health delegation should not need broad type ignores."""
    source = (Path(__file__).parent.parent / "src" / "acp" / "manager.py").read_text(encoding="utf-8")

    assert "classify_manager_idle_health" in source
    assert "_classify_idle_health_for_manager" not in source
    assert "type: ignore[attr-defined]" not in source
    assert "context=context,  # type: ignore[arg-type]" not in source


def test_card_builder_project_context_import_is_type_checking_only() -> None:
    """Task 27 guard: card builder must not runtime-import project context for annotations."""
    source = (Path(__file__).parent.parent / "src" / "card" / "builder.py").read_text(encoding="utf-8")

    assert "from __future__ import annotations" in source
    assert "from ..project.context import ProjectContext" not in source.split("if TYPE_CHECKING:", 1)[0]


def test_card_builder_implementations_do_not_runtime_import_project_context() -> None:
    root = Path(__file__).parent.parent
    offenders: list[str] = []
    for path in (root / "src" / "card" / "builders").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        runtime_section = source.split("if TYPE_CHECKING:", 1)[0]
        if "ProjectContext" in runtime_section and "import ProjectContext" in runtime_section:
            offenders.append(str(path.relative_to(root)))

    assert offenders == []


def test_domain_compat_entries_exist_for_spec_and_ttadk_utils() -> None:
    """Task 28 guard: domain packages expose compatibility entries while old utils stay importable."""
    spec_utils = importlib.import_module("src.spec_engine.utils")
    legacy_spec_utils = importlib.import_module("src.utils.spec_utils")
    ttadk_wrapper = importlib.import_module("src.ttadk.wrapper")
    legacy_ttadk_wrapper = importlib.import_module("src.utils.ttadk_wrapper")

    assert spec_utils.extract_json_blob is legacy_spec_utils.extract_json_blob
    assert spec_utils.parse_review_output_loose is legacy_spec_utils.parse_review_output_loose
    assert ttadk_wrapper.WrapperState is legacy_ttadk_wrapper.WrapperState
    assert ttadk_wrapper.pump_filtered_stream is legacy_ttadk_wrapper.pump_filtered_stream


def test_card_styles_has_no_wildcard_reexports_and_documents_ui_text_shim() -> None:
    """Refactoring-analysis guard: styles.py must stay explicit and UI_TEXT shim bounded."""
    source = (Path(__file__).parent.parent / "src" / "card" / "styles.py").read_text(encoding="utf-8")

    assert "import *" not in source
    assert "from src.card.ui_text import UI_TEXT" in source
    assert "removed after 2026-06-01" in source


def test_production_code_does_not_call_cardevent_worktree_compat_shims() -> None:
    """Refactoring-analysis guard: production paths import src.card.events.worktree factories directly."""
    root = Path(__file__).parent.parent / "src"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.parts[-3:] == ("card", "events", "factories.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if "CardEvent.worktree_" in text:
            offenders.append(str(path.relative_to(root.parent)))

    assert offenders == []


def test_error_diagnostics_do_not_live_in_generic_utils() -> None:
    """Feishu/card diagnostic security state must not be a process bearer token in utils."""
    root = Path(__file__).parent.parent

    assert not (root / "src" / "utils" / "error_diagnostics.py").exists()

    offenders: list[str] = []
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "utils.error_diagnostics" in text or "src.utils.error_diagnostics" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
