"""Tests for set_ttadk_manager function for dependency injection/testing."""

from unittest.mock import MagicMock

import pytest

from src.ttadk.manager import (
    TTADKManager,
    _reset_ttadk_manager_for_testing,
    get_ttadk_manager,
    set_ttadk_manager,
)


@pytest.fixture(autouse=True)
def reset_ttadk_singleton():
    """Ensure TTADKManager singleton is reset before each test."""
    _reset_ttadk_manager_for_testing()
    yield
    _reset_ttadk_manager_for_testing()


def test_set_ttadk_manager_updates_global_singleton():
    """Verify that set_ttadk_manager correctly replaces the global TTADKManager singleton."""
    # Create a mock manager instance
    mock_manager = MagicMock(spec=TTADKManager)
    mock_manager.get_current_tool.return_value = "mock_tool"

    # First, get the default manager
    original = get_ttadk_manager()
    assert original is not mock_manager

    # Now set our mock
    set_ttadk_manager(mock_manager)

    # Verify that subsequent get_ttadk_manager returns our mock
    retrieved = get_ttadk_manager()
    assert retrieved is mock_manager
    assert retrieved.get_current_tool() == "mock_tool"


def test_set_ttadk_manager_docstring():
    """Verify set_ttadk_manager has appropriate docstring."""
    import inspect
    doc = inspect.getdoc(set_ttadk_manager)
    assert doc is not None
    assert "dependency injection" in doc.lower() or "testing" in doc.lower()


def test_set_ttadk_manager_with_real_instance():
    """Verify that set_ttadk_manager can set a real TTADKManager instance."""
    custom_manager = TTADKManager(default_tool="gemini")

    set_ttadk_manager(custom_manager)

    retrieved = get_ttadk_manager()
    assert retrieved is custom_manager
    assert retrieved.get_current_tool() == "gemini"

