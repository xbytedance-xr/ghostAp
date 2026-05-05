"""Tests that all backward-compatible shim modules emit DeprecationWarning."""

from __future__ import annotations

import importlib
import sys
import warnings
from typing import Any

import pytest

# Each entry: (shim_module_path, canonical_module_path, list_of_names)
GETATTR_SHIMS = [
    ("src.card.session_config", "src.card.session.config", ["SessionCallbacks", "SessionConfig"]),
    ("src.card.session_factory", "src.card.session.factory", ["CardSessionFactory"]),
    ("src.card.session_rotator", "src.card.session.rotator", ["SessionRotator"]),
    ("src.card.static_session", "src.card.session.static", ["StaticCardSession"]),
    ("src.card.delivery_tracker", "src.card.delivery.tracker", ["DeliveryTracker", "PendingAction"]),
    ("src.card.action_dispatch", "src.card.actions.dispatch", ["build_worktree_action_registry"]),
    ("src.card.action_router", "src.card.actions.router", ["ActionRouter"]),
    ("src.card.timer_manager", "src.card.timers.manager", ["SessionTimerManager", "_MAX_TTL_RETRIES"]),
    ("src.card.timer_scheduler", "src.card.timers.scheduler", ["TimerHandle", "TimerScheduler", "get_timer_scheduler", "_reset_global_scheduler"]),
]


def _fresh_import(module_path: str) -> Any:
    """Force re-import to trigger __getattr__ on each access."""
    # Remove cached module so __getattr__ fires again
    sys.modules.pop(module_path, None)
    return importlib.import_module(module_path)


class TestGetAttrShims:
    """Verify __getattr__-based shims emit DeprecationWarning on attribute access."""

    @pytest.mark.parametrize("shim_path,canonical,names", GETATTR_SHIMS, ids=[s[0] for s in GETATTR_SHIMS])
    def test_deprecation_warning_on_access(self, shim_path: str, canonical: str, names: list[str]):
        """Each exported name from a shim triggers a DeprecationWarning."""
        mod = _fresh_import(shim_path)
        for name in names:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                # Access the attribute — should trigger __getattr__
                val = getattr(mod, name)
                assert val is not None

            dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(dep_warnings) >= 1, f"No DeprecationWarning for {shim_path}.{name}"

            msg = str(dep_warnings[0].message)
            # Warning message should mention the canonical path
            assert canonical in msg, f"Warning does not mention canonical path: {msg}"
            assert "2026-06-01" in msg, f"Warning does not mention deadline: {msg}"

    @pytest.mark.parametrize("shim_path,canonical,names", GETATTR_SHIMS, ids=[s[0] for s in GETATTR_SHIMS])
    def test_stacklevel_points_to_caller(self, shim_path: str, canonical: str, names: list[str]):
        """stacklevel=2 should make the warning point to this test file, not the shim."""
        mod = _fresh_import(shim_path)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            getattr(mod, names[0])

        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert dep_warnings, f"No DeprecationWarning for {shim_path}.{names[0]}"
        # The warning's filename should be THIS test file, not the shim module
        assert "test_shim_deprecation" in dep_warnings[0].filename

    @pytest.mark.parametrize("shim_path,canonical,names", GETATTR_SHIMS, ids=[s[0] for s in GETATTR_SHIMS])
    def test_nonexistent_attr_raises(self, shim_path: str, canonical: str, names: list[str]):
        """Accessing a non-existent attribute should raise AttributeError."""
        mod = _fresh_import(shim_path)
        with pytest.raises(AttributeError, match="has no attribute"):
            getattr(mod, "__nonexistent_xyz__")


class TestActionIdsLazyWarn:
    """Verify action_ids.py (__getattr__ shim) emits warning on access, not import."""

    def test_no_warning_on_import(self):
        """Importing action_ids alone should NOT trigger a DeprecationWarning."""
        sys.modules.pop("src.card.action_ids", None)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("src.card.action_ids")

        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) == 0, f"Unexpected DeprecationWarning on import: {dep_warnings}"

    def test_deprecation_on_access(self):
        """Accessing a name from action_ids triggers DeprecationWarning."""
        mod = _fresh_import("src.card.action_ids")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            val = getattr(mod, "APPROVE_ACTION")
            assert val is not None

        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1, "No DeprecationWarning when accessing action_ids.APPROVE_ACTION"

        msg = str(dep_warnings[0].message)
        assert "src.card.actions.dispatch" in msg
        assert "2026-06-01" in msg
        assert "deprecated in v0.1.0" in msg

    def test_nonexistent_attr_raises(self):
        """Accessing a non-existent attribute should raise AttributeError."""
        mod = _fresh_import("src.card.action_ids")
        with pytest.raises(AttributeError, match="has no attribute"):
            getattr(mod, "__nonexistent_xyz__")
