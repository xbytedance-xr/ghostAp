"""Tests for SlockChannel model contract - bootstrap_failed field serialization."""


from src.slock_engine.models import SlockChannel


class TestSlockChannelBootstrapFailed:
    """Test SlockChannel bootstrap_failed field contract."""

    def test_bootstrap_failed_default_false(self):
        """SlockChannel should have bootstrap_failed=False by default."""
        channel = SlockChannel(channel_id="test_123")
        assert channel.bootstrap_failed is False

    def test_bootstrap_failed_can_be_set(self):
        """SlockChannel bootstrap_failed should be settable."""
        channel = SlockChannel(channel_id="test_123", bootstrap_failed=True)
        assert channel.bootstrap_failed is True

    def test_to_dict_includes_bootstrap_failed(self):
        """to_dict() should include bootstrap_failed field."""
        channel = SlockChannel(channel_id="test_123", bootstrap_failed=True)
        data = channel.to_dict()
        assert "bootstrap_failed" in data
        assert data["bootstrap_failed"] is True

    def test_to_dict_bootstrap_failed_false(self):
        """to_dict() should include bootstrap_failed=False when not set."""
        channel = SlockChannel(channel_id="test_123")
        data = channel.to_dict()
        assert data["bootstrap_failed"] is False

    def test_from_dict_reads_bootstrap_failed_true(self):
        """from_dict() should read bootstrap_failed=True from data."""
        data = {
            "channel_id": "test_123",
            "bootstrap_failed": True,
        }
        channel = SlockChannel.from_dict(data)
        assert channel.bootstrap_failed is True

    def test_from_dict_reads_bootstrap_failed_false(self):
        """from_dict() should read bootstrap_failed=False from data."""
        data = {
            "channel_id": "test_123",
            "bootstrap_failed": False,
        }
        channel = SlockChannel.from_dict(data)
        assert channel.bootstrap_failed is False

    def test_from_dict_default_bootstrap_failed_false(self):
        """from_dict() should default bootstrap_failed to False if missing."""
        data = {
            "channel_id": "test_123",
        }
        channel = SlockChannel.from_dict(data)
        assert channel.bootstrap_failed is False

    def test_round_trip_serialization(self):
        """bootstrap_failed should survive to_dict -> from_dict round trip."""
        original = SlockChannel(
            channel_id="test_123",
            name="Test Team",
            bootstrap_failed=True,
        )
        data = original.to_dict()
        restored = SlockChannel.from_dict(data)
        assert restored.bootstrap_failed is True
        assert restored.channel_id == original.channel_id
        assert restored.name == original.name
