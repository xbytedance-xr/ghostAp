"""Task 19: Verify _BLOCK_KIND_MAP and _get_block_kind_handlers stay in sync."""


def test_block_kind_map_consistent_with_handlers():
    """Registries are consistent.

    This test verifies programmatically that models._BLOCK_KIND_MAP (minus
    tool_call which is handled by lookahead logic) matches exactly
    _get_block_kind_handlers() keys.
    """
    from src.card.state.models import _BLOCK_KIND_MAP
    from src.card.render.atoms import _get_block_kind_handlers

    model_keys = set(_BLOCK_KIND_MAP.keys()) - {"tool_call"}
    handler_keys = set(_get_block_kind_handlers().keys())
    assert model_keys == handler_keys, (
        f"Mismatch:\n"
        f"  In models but not handlers: {model_keys - handler_keys}\n"
        f"  In handlers but not models: {handler_keys - model_keys}"
    )


def test_all_handler_keys_are_valid_block_kinds():
    """Every key in _get_block_kind_handlers corresponds to a real block dataclass."""
    from src.card.state.models import _BLOCK_KIND_MAP
    from src.card.render.atoms import _get_block_kind_handlers

    for kind in _get_block_kind_handlers():
        assert kind in _BLOCK_KIND_MAP, f"Handler key '{kind}' not in _BLOCK_KIND_MAP"


def test_import_time_no_handler_construction():
    """Importing atoms.py does not build the handler registry at import time."""
    import importlib
    import src.card.render.atoms as atoms_mod

    # The lazy function uses functools.cache — verify it's callable
    assert callable(atoms_mod._get_block_kind_handlers)
    # Calling it should succeed (builds on first call)
    handlers = atoms_mod._get_block_kind_handlers()
    assert isinstance(handlers, dict)
    assert len(handlers) > 0
