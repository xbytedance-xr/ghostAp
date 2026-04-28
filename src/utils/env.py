"""Environment variable utilities for subprocess spawning."""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional, Callable


# Keys that must be removed to avoid nested-session guard crashes
# when spawning ACP / CLI subprocesses from within a wrapper environment.
_GUARD_KEYS = ("CLAUDECODE",)

# Type alias for test environment checker function
TestEnvironmentChecker = Callable[[], bool]

# Global state for injected test environment checker
_test_environment_checker: Optional[TestEnvironmentChecker] = None
_test_environment_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock


def _default_is_test_environment() -> bool:
    """Default implementation for checking if we're running in a test environment.
    
    Returns:
        True if running in pytest or a test environment, False otherwise.
    """
    # Check for pytest-specific environment variables
    if os.getenv("PYTEST_CURRENT_TEST") is not None:
        return True
    
    # Check if pytest module is loaded
    if "pytest" in sys.modules:
        return True
    
    # Check for common test environment markers
    test_markers = ("TESTING", "TEST", "CI")
    for marker in test_markers:
        value = os.getenv(marker)
        if value is not None:
            lower_value = value.strip().lower()
            if lower_value not in ("", "0", "false", "no", "off"):
                return True
    
    return False


def is_test_environment() -> bool:
    """Check if we're running in a test environment.
    
    This function supports dependency injection: use `set_test_environment_checker()` 
    to replace the default implementation.
    
    Returns:
        True if running in pytest or a test environment, False otherwise.
    """
    with _test_environment_lock:
        if _test_environment_checker is not None:
            return _test_environment_checker()
    return _default_is_test_environment()


def set_test_environment_checker(checker: Optional[TestEnvironmentChecker]) -> None:
    """Set a custom test environment checker function.
    
    This allows for dependency injection to support different test environments
    (unit tests, integration tests, E2E tests).
    
    Args:
        checker: Custom function that returns True if in a test environment.
                 Pass None to restore the default implementation.
    """
    global _test_environment_checker
    with _test_environment_lock:
        _test_environment_checker = checker


def get_test_environment_checker() -> Optional[TestEnvironmentChecker]:
    """Get the current test environment checker function.
    
    Returns:
        The current checker function if set, None otherwise.
    """
    with _test_environment_lock:
        return _test_environment_checker


def build_clean_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return a copy of *base* (default: ``os.environ``) with guard keys removed.

    This centralises the ``os.environ.copy(); env.pop("CLAUDECODE", None)``
    pattern used across ACP, agent-session, and provider modules.
    """
    env = dict(base) if base is not None else os.environ.copy()
    for key in _GUARD_KEYS:
        env.pop(key, None)
    return env


def _reset_env_for_testing() -> None:
    """Reset the env module's global state for testing.

    This function is only allowed in test environments.
    """
    import sys

    if "pytest" not in sys.modules:
        raise RuntimeError(
            "_reset_env_for_testing() is only allowed in test environments."
        )

    global _test_environment_checker
    with _test_environment_lock:
        _test_environment_checker = None
