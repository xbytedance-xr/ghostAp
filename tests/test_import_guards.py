"""Verify that public __all__ symbols in key modules are importable."""

import importlib

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
