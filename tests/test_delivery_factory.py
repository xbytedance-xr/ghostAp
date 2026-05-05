"""Tests for src/card/delivery/factory.py — verify Settings injection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_create_card_delivery_injects_settings():
    """Factory must pass max_session_locks and session_lock_ttl from Settings."""
    mock_client = MagicMock()

    mock_settings = MagicMock()
    mock_settings.card.session_lock_max = 5000
    mock_settings.card.session_lock_ttl = 300.0

    with patch("src.config.get_settings", return_value=mock_settings):
        from src.card.delivery.factory import create_card_delivery
        delivery = create_card_delivery(mock_client)

    assert delivery._max_session_locks == 5000
    assert delivery._session_lock_ttl == 300.0


def test_create_card_delivery_uses_default_settings():
    """Factory should work with default settings (no env override)."""
    mock_client = MagicMock()

    # Use real settings (defaults)
    from src.card.delivery.factory import create_card_delivery
    delivery = create_card_delivery(mock_client)

    from src.config import get_settings
    settings = get_settings()
    assert delivery._max_session_locks == settings.card.session_lock_max
    assert delivery._session_lock_ttl == settings.card.session_lock_ttl


def test_invalid_max_session_locks_zero():
    """CardDelivery must reject max_session_locks=0 with ValueError."""
    from src.card.delivery.engine import CardDelivery

    mock_client = MagicMock()
    with pytest.raises(ValueError, match="max_session_locks must be > 0"):
        CardDelivery(client=mock_client, max_session_locks=0)


def test_invalid_session_lock_ttl_negative():
    """CardDelivery must reject session_lock_ttl=-1 with ValueError."""
    from src.card.delivery.engine import CardDelivery

    mock_client = MagicMock()
    with pytest.raises(ValueError, match="session_lock_ttl must be > 0"):
        CardDelivery(client=mock_client, session_lock_ttl=-1)


def test_invalid_eviction_interval_zero():
    """CardDelivery must reject eviction_interval=0 with ValueError."""
    from src.card.delivery.engine import CardDelivery

    mock_client = MagicMock()
    with pytest.raises(ValueError, match="eviction_interval must be > 0"):
        CardDelivery(client=mock_client, eviction_interval=0)


def test_invalid_eviction_interval_negative():
    """CardDelivery must reject eviction_interval=-1 with ValueError."""
    from src.card.delivery.engine import CardDelivery

    mock_client = MagicMock()
    with pytest.raises(ValueError, match="eviction_interval must be > 0"):
        CardDelivery(client=mock_client, eviction_interval=-1)
