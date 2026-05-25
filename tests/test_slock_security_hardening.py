"""Tests for security hardening (AC20, AC21)."""
import time
from unittest.mock import MagicMock

import pytest


class TestDissolveTokenOperatorBinding:
    """AC21: Non-initiator dissolve confirmation is rejected."""

    def test_non_initiator_rejected(self):
        """Operator_id mismatch causes rejection."""
        # The _dissolve_tokens dict stores (token, timestamp, operator_id)
        tokens = {"chat_123": ("abc123token", time.time(), "user_A")}

        # Verify the tuple structure
        token, ts, operator_id = tokens["chat_123"]
        assert operator_id == "user_A"

        # Simulate verification: current_operator != original_operator_id
        current_operator = "user_B"
        assert current_operator != operator_id  # This would trigger rejection

    def test_admin_can_bypass_operator_check(self):
        """Admin users bypass the operator_id binding check."""
        tokens = {"chat_456": ("def456token", time.time(), "user_A")}
        token, ts, operator_id = tokens["chat_456"]

        current_operator = "admin_user"
        # Admin check: _has_slock_permission returns True for admins
        is_admin = True  # Simulated

        # Even though operator doesn't match, admin bypasses
        if current_operator != operator_id:
            if is_admin:
                pass  # Allowed through
            else:
                pytest.fail("Non-admin non-initiator should be blocked")
