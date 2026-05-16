"""Tests for DeliveryRegistry: instance tracking, shutdown, drain, and atexit."""

from unittest.mock import MagicMock, patch

from src.card.delivery.registry import DeliveryRegistry


class _FakeDelivery:
    """Minimal stand-in for CardDelivery with _shutdown/_drain methods."""

    def __init__(self):
        self._shutdown_called = False
        self._drain_called = False

    def _shutdown(self):
        self._shutdown_called = True

    def _drain(self, timeout: float = 5.0) -> bool:
        self._drain_called = True
        return True


class TestRegisterUnregister:
    """Instance registration and unregistration."""

    def test_register_adds_instance(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        reg.register(d)
        assert d in reg.instances

    def test_unregister_removes_instance(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        reg.register(d)
        reg.unregister(d)
        assert d not in reg.instances

    def test_unregister_nonexistent_no_error(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        reg.unregister(d)  # should not raise

    def test_explicit_unregister_required(self):
        """With regular set, instances persist until explicitly unregistered."""
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        reg.register(d)
        assert len(reg.instances) == 1
        # Deleting the local reference does NOT auto-remove (no WeakSet)
        ref = d
        del d
        assert len(reg.instances) == 1
        # Explicit unregister removes it
        reg.unregister(ref)
        assert len(reg.instances) == 0


class TestShutdownAll:
    """shutdown_all shuts down all instances once."""

    def test_shutdown_all_calls_shutdown_on_each(self):
        reg = DeliveryRegistry()
        d1, d2 = _FakeDelivery(), _FakeDelivery()
        reg.register(d1)
        reg.register(d2)
        reg.shutdown_all()
        assert d1._shutdown_called
        assert d2._shutdown_called

    def test_shutdown_all_idempotent(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        d._shutdown = MagicMock()
        reg.register(d)
        reg.shutdown_all()
        reg.shutdown_all()
        d._shutdown.assert_called_once()

    def test_shutdown_done_flag(self):
        reg = DeliveryRegistry()
        assert not reg.shutdown_done
        reg.shutdown_all()
        assert reg.shutdown_done

    def test_shutdown_tolerates_exception(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        d._shutdown = MagicMock(side_effect=RuntimeError("boom"))
        reg.register(d)
        # Should not raise
        reg.shutdown_all()
        assert reg.shutdown_done


class TestDrainInFlight:
    """drain_in_flight drains all instances."""

    def test_drain_success(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        reg.register(d)
        assert reg.drain_in_flight(timeout=1.0) is True
        assert d._drain_called

    def test_drain_returns_false_on_failure(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        d._drain = MagicMock(return_value=False)
        reg.register(d)
        assert reg.drain_in_flight(timeout=1.0) is False


class TestInstallAtexit:
    """install_atexit registers handler idempotently."""

    def test_install_atexit_idempotent(self):
        reg = DeliveryRegistry()
        with patch("src.card.delivery.registry.atexit.register") as mock_reg:
            reg.install_atexit()
            reg.install_atexit()
            mock_reg.assert_called_once()
            # Verify the callback drains then shuts down
            callback = mock_reg.call_args[0][0]
            with patch.object(reg, "drain_in_flight") as mock_drain, \
                 patch.object(reg, "shutdown_all") as mock_shutdown:
                callback()
                mock_drain.assert_called_once_with(timeout=5)
                mock_shutdown.assert_called_once()

    def test_install_atexit_sets_flag(self):
        reg = DeliveryRegistry()
        with patch("src.card.delivery.registry.atexit.register"):
            reg.install_atexit()
            assert reg._atexit_installed


class TestReset:
    """reset() restores clean state for test isolation."""

    def test_reset_clears_instances(self):
        reg = DeliveryRegistry()
        d = _FakeDelivery()
        reg.register(d)
        reg.reset()
        assert len(reg.instances) == 0

    def test_reset_clears_shutdown_flag(self):
        reg = DeliveryRegistry()
        reg.shutdown_all()
        assert reg.shutdown_done
        reg.reset()
        assert not reg.shutdown_done
