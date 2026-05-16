from unittest.mock import MagicMock

from src.utils.registry import CleanupRegistry


def test_cleanup_registry_reverse_order():
    """Verify that cleanup functions are executed in reverse order of registration."""
    registry = CleanupRegistry("Test")
    order = []

    def cleanup1():
        order.append(1)

    def cleanup2():
        order.append(2)

    def cleanup3():
        order.append(3)

    registry.register("one", cleanup1)
    registry.register("two", cleanup2)
    registry.register("three", cleanup3)

    registry.cleanup()

    # Deterministic reverse order: 3 -> 2 -> 1
    assert order == [3, 2, 1]

def test_cleanup_registry_handles_exceptions():
    """Verify that a failure in one cleanup function doesn't stop others."""
    registry = CleanupRegistry("TestExc")
    order = []

    def cleanup1():
        order.append(1)

    def cleanup_fail():
        raise ValueError("Intentional cleanup failure")

    def cleanup2():
        order.append(2)

    registry.register("one", cleanup1)
    registry.register("fail", cleanup_fail)
    registry.register("two", cleanup2)

    # Should not raise exception
    registry.cleanup()

    # Other cleanups should still have run (in reverse order)
    assert order == [2, 1]

def test_cleanup_registry_idempotent():
    """Verify that calling cleanup multiple times only executes functions once."""
    registry = CleanupRegistry("Idempotent")
    mock_cb = MagicMock()

    registry.register("task", mock_cb)
    registry.cleanup()
    registry.cleanup()

    mock_cb.assert_called_once()

def test_cleanup_registry_register_after_cleanup():
    """Verify that registering a function after cleanup has already run triggers immediate execution."""
    registry = CleanupRegistry("PostCleanup")
    registry.cleanup()

    mock_cb = MagicMock()
    # Should be called immediately during registration because self._cleaned is True
    registry.register("late", mock_cb)

    mock_cb.assert_called_once()
