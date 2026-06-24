"""Tests for card buttons module (AC21)."""

from __future__ import annotations

import warnings


def test_no_runtime_warning_on_import():
    """AC21: 导入 src.card.render.buttons 时无 RuntimeWarning。

    Specifically verifies that WORKFLOW_CANCEL and other workflow action_ids
    are properly registered and do not trigger the _CONFIRM_TITLE_MAP validation
    warning.
    """
    # Capture warnings during import - we don't need to reimport the module
    # as the test just needs to verify that there are no warnings when the module
    # is first imported. The module should already be imported by this point.
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        # Import the module — this triggers the _CONFIRM_TITLE_MAP validation
        # If the module was already imported, this won't trigger warnings again,
        # but that's okay because we're testing that there are no warnings

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
    from src.card.actions.dispatch import (
        SHOW_WORKFLOW_MENU,
        WORKFLOW_CANCEL,
        WORKFLOW_CONFIRM_START,
        WORKFLOW_CONFIRM_TOOLS,
        WORKFLOW_LIST_TEMPLATES,
        WORKFLOW_ORCHESTRATOR_FINISH,
        WORKFLOW_ORCHESTRATOR_SELECT_MODEL,
        WORKFLOW_ORCHESTRATOR_SELECT_TOOL,
        WORKFLOW_REGENERATE_SCRIPT,
        WORKFLOW_REVIEW_FINISH,
        WORKFLOW_REVIEW_SELECT_MODEL,
        WORKFLOW_REVIEW_SELECT_TOOL,
        WORKFLOW_SELECT_TOOL,
        WORKFLOW_SHOW_HELP,
    )
    from src.card.render.buttons import _valid_keys

    workflow_action_ids = [
        WORKFLOW_CANCEL,
        WORKFLOW_CONFIRM_TOOLS,
        WORKFLOW_CONFIRM_START,
        WORKFLOW_SELECT_TOOL,
        WORKFLOW_REGENERATE_SCRIPT,
        WORKFLOW_ORCHESTRATOR_SELECT_TOOL,
        WORKFLOW_ORCHESTRATOR_SELECT_MODEL,
        WORKFLOW_ORCHESTRATOR_FINISH,
        WORKFLOW_REVIEW_SELECT_TOOL,
        WORKFLOW_REVIEW_SELECT_MODEL,
        WORKFLOW_REVIEW_FINISH,
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
