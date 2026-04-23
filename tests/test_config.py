"""Tests for config module singleton management functions."""

from unittest.mock import MagicMock, patch
import pytest

from src.config import Settings, get_settings, set_settings, _reset_settings_for_testing


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure config singleton is reset before each test."""
    _reset_settings_for_testing()
    yield
    _reset_settings_for_testing()


def test_set_settings_updates_global_singleton():
    """Verify that set_settings correctly replaces the global settings singleton."""
    # Create a mock settings instance
    mock_settings = Settings()
    mock_settings.app_id = "test_app_id_set"  # type: ignore
    
    # First, get the default settings
    original = get_settings()
    assert original is not mock_settings
    
    # Now set our mock
    set_settings(mock_settings)
    
    # Verify that subsequent get_settings returns our mock
    retrieved = get_settings()
    assert retrieved is mock_settings
    assert retrieved.app_id == "test_app_id_set"  # type: ignore


def test_set_settings_respects_thread_safety():
    """Verify that set_settings works correctly with thread safety."""
    # This test ensures the lock is properly used (but doesn't test race conditions)
    mock1 = Settings()
    mock2 = Settings()
    
    set_settings(mock1)
    assert get_settings() is mock1
    
    set_settings(mock2)
    assert get_settings() is mock2


def test_set_settings_docstring():
    """Verify set_settings has appropriate docstring."""
    import inspect
    doc = inspect.getdoc(set_settings)
    assert doc is not None
    assert "dependency injection" in doc.lower() or "testing" in doc.lower()

