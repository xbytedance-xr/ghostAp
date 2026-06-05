"""Tests for card buttons module (AC21)."""

from __future__ import annotations

import warnings

import pytest


def test_no_runtime_warning_on_import():
    """AC21: 导入 src.card.render.buttons 时无 RuntimeWarning。
    
    Specifically verifies that WORKFLOW_CANCEL and other workflow action_ids
    are properly registered and do not trigger the _CONFIRM_TITLE_MAP validation
    warning.
    """
    # Clear any previously imported modules to ensure fresh import
    import sys
    modules_to_remove = [
        key for key in sys.modules.keys()
        if key.startswith("src.card.render") or key.startswith("src.card.actions")
    ]
    for mod in modules_to_remove:
        del sys.modules[mod]
    
    # Capture warnings during import
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        
        # Import the module — this triggers the _CONFIRM_TITLE_MAP validation
        from src.card.render import buttons
        
        # Check for RuntimeWarnings about workflow_cancel
        workflow_warnings = [
            warning for warning in w
            if issubclass(warning.category, RuntimeWarning)
            and "workflow_cancel" in str(warning.message).lower()
        ]
        
        assert len(workflow_warnings) == 0, (
            f"Unexpected RuntimeWarning about workflow_cancel: "
            f"{[str(w.message) for w in workflow_warnings]}"
        )
        
        # Also check for any _CONFIRM_TITLE_MAP warnings
        confirm_map_warnings = [
            warning for warning in w
            if issubclass(warning.category, RuntimeWarning)
            and "_CONFIRM_TITLE_MAP" in str(warning.message)
        ]
        
        assert len(confirm_map_warnings) == 0, (
            f"Unexpected RuntimeWarning from _CONFIRM_TITLE_MAP: "
            f"{[str(w.message) for w in confirm_map_warnings]}"
        )


def test_workflow_action_ids_in_valid_keys():
    """AC21: Workflow action_ids 包含在 _valid_keys 集合中。"""
    from src.card.render.buttons import _valid_keys
    from src.card.actions.dispatch import (
        WORKFLOW_CANCEL,
        WORKFLOW_CONFIRM_TOOLS,
        WORKFLOW_CONFIRM_START,
        WORKFLOW_SELECT_TOOL,
        WORKFLOW_SELECT_BUDGET,
        WORKFLOW_REGENERATE_SCRIPT,
        SHOW_WORKFLOW_MENU,
        WORKFLOW_LIST_TEMPLATES,
        WORKFLOW_SHOW_HELP,
    )
    
    workflow_action_ids = [
        WORKFLOW_CANCEL,
        WORKFLOW_CONFIRM_TOOLS,
        WORKFLOW_CONFIRM_START,
        WORKFLOW_SELECT_TOOL,
        WORKFLOW_SELECT_BUDGET,
        WORKFLOW_REGENERATE_SCRIPT,
        SHOW_WORKFLOW_MENU,
        WORKFLOW_LIST_TEMPLATES,
        WORKFLOW_SHOW_HELP,
    ]
    
    for action_id in workflow_action_ids:
        assert action_id in _valid_keys, f"Workflow action_id {action_id} not in _valid_keys"


def test_import_with_error_on_warning():
    """AC21: 使用 -W error::RuntimeWarning 运行时导入不报错。"""
    # This test is more of a documentation — the actual runtime check
    # is done by running pytest with -W error::RuntimeWarning
    import subprocess
    import sys
    
    result = subprocess.run(
        [
            sys.executable, "-W", "error::RuntimeWarning",
            "-c", "from src.card.render import buttons; print('OK')"
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    
    assert result.returncode == 0, (
        f"Import failed with RuntimeWarning treated as error. "
        f"Stderr: {result.stderr}"
    )
    assert "OK" in result.stdout
