"""Verify graceful_shutdown drains in-flight deliveries BEFORE shutting down instances."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_drain_called_before_shutdown_all():
    """drain_in_flight must be called before shutdown_all to ensure in-flight work completes."""
    call_order: list[str] = []

    mock_registry = MagicMock()
    mock_registry.drain_in_flight = MagicMock(side_effect=lambda **kw: call_order.append("drain"))
    mock_registry.shutdown_all = MagicMock(side_effect=lambda: call_order.append("shutdown_all"))

    with (
        patch("src.utils.shutdown._shutdown_in_progress", False),
        patch("src.utils.shutdown._shutdown_lock", __import__("threading").Lock()),
        patch("src.utils.shutdown.run_all_cleanups", return_value=MagicMock(__await__=lambda s: iter([None]))),
        patch("src.utils.shutdown.fire_hooks"),
        patch("src.card.delivery.registry.delivery_registry", mock_registry),
        pytest.raises(SystemExit),
    ):
        from src.utils.shutdown import _reset_shutdown_state
        _reset_shutdown_state()

        from src.utils.shutdown import graceful_shutdown
        graceful_shutdown(exit_code=0)

    assert call_order == ["drain", "shutdown_all"], (
        f"Expected drain before shutdown_all, got: {call_order}"
    )


def test_timer_scheduler_shutdown_between_drain_and_delivery_shutdown():
    """TimerScheduler.shutdown must be called after drain and before delivery_registry.shutdown_all."""
    call_order: list[str] = []

    mock_registry = MagicMock()
    mock_registry.drain_in_flight = MagicMock(side_effect=lambda **kw: call_order.append("drain"))
    mock_registry.shutdown_all = MagicMock(side_effect=lambda: call_order.append("shutdown_all"))

    mock_scheduler = MagicMock()
    mock_scheduler.shutdown = MagicMock(side_effect=lambda timeout=2.0: call_order.append("timer_shutdown"))

    with (
        patch("src.utils.shutdown._shutdown_in_progress", False),
        patch("src.utils.shutdown._shutdown_lock", __import__("threading").Lock()),
        patch("src.utils.shutdown.run_all_cleanups", return_value=MagicMock(__await__=lambda s: iter([None]))),
        patch("src.utils.shutdown.fire_hooks"),
        patch("src.card.delivery.registry.delivery_registry", mock_registry),
        patch("src.card.timers.scheduler.get_timer_scheduler", return_value=mock_scheduler),
        pytest.raises(SystemExit),
    ):
        from src.utils.shutdown import _reset_shutdown_state
        _reset_shutdown_state()

        from src.utils.shutdown import graceful_shutdown
        graceful_shutdown(exit_code=0)

    assert call_order == ["drain", "timer_shutdown", "shutdown_all"], (
        f"Expected drain → timer_shutdown → shutdown_all, got: {call_order}"
    )
