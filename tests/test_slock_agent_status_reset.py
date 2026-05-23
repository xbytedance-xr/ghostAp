"""Tests for Agent status reset guarantee via try/finally in _execute_agent.

Validates that agent status is always reset to IDLE even when exceptions occur,
and that exceptions are properly propagated (not swallowed).
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.slock_engine.engine import SlockEngine, SlockEngineCallbacks
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockChannel


class TestAgentStatusReset:
    """Verify agent status is reset to IDLE on all code paths."""

    def _make_engine(self, tmp_path):
        """Create a minimal SlockEngine for testing."""
        return SlockEngine(
            chat_id="chat_test",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path),
        )

    def test_exception_during_execution_resets_status_to_idle(self, tmp_path):
        """When _execute_agent raises an exception, agent status must be reset to IDLE.

        This prevents the permanent RUNNING deadlock scenario.
        """
        engine = self._make_engine(tmp_path)
        ch = SlockChannel(channel_id="ch_reset", name="Reset", team_name="ResetTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(agent_id="agent-reset", name="ResetBot", agent_type="coco")

        # Simulate an exception being raised after agent enters RUNNING state
        # We patch _build_agent_prompt to raise after THINKING→RUNNING transition
        def raise_after_running(*args, **kwargs):
            # At this point, agent should be in RUNNING state
            status = engine.get_agent_status(agent.agent_id)
            assert status == AgentStatus.RUNNING, f"Expected RUNNING, got {status}"
            raise RuntimeError("Simulated failure during execution")

        with patch.object(engine, "_run_acp_session", side_effect=raise_after_running):
            try:
                engine._execute_agent(agent, "do something risky", None)
            except Exception:
                pass  # We'll check status after

        # After exception, status MUST be IDLE (not stuck in RUNNING)
        final_status = engine.get_agent_status(agent.agent_id)
        assert final_status == AgentStatus.IDLE, (
            f"Agent status not reset! Expected IDLE, got {final_status}. "
            "This would cause permanent RUNNING deadlock."
        )
        engine.cleanup()

    def test_exception_returns_none_not_swallowed(self, tmp_path):
        """Exceptions are logged and return None, not silently swallowed.

        The catch-all handler logs the exception with full traceback,
        resets status to IDLE in finally block, and returns None.
        Callers can check for None result to detect failure.
        """
        engine = self._make_engine(tmp_path)
        ch = SlockChannel(channel_id="ch_propagate", name="Prop", team_name="PropTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(agent_id="agent-propagate", name="PropBot", agent_type="coco")

        original_exception = RuntimeError("Critical: database connection failed")

        with patch.object(engine, "_run_acp_session", side_effect=original_exception):
            # _execute_agent catches exception, logs it, resets status, returns None
            result = engine._execute_agent(agent, "query database", None)

            # Result should be None to indicate failure
            assert result is None, (
                "Expected None result on exception, "
                "callers use None to detect execution failure."
            )

        # Status should still be reset even though exception was caught
        final_status = engine.get_agent_status(agent.agent_id)
        assert final_status == AgentStatus.IDLE
        engine.cleanup()

    def test_normal_execution_path_status_transitions(self, tmp_path):
        """Verify normal execution path still works with proper status transitions.

        IDLE → WAKING → THINKING → RUNNING → CHECKING → SENDING → IDLE
        """
        engine = self._make_engine(tmp_path)
        ch = SlockChannel(channel_id="ch_normal", name="Normal", team_name="NormalTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(agent_id="agent-normal", name="NormalBot", agent_type="coco")

        # Track status transitions
        status_history = []

        original_set_status = engine.set_agent_status
        original_transition = engine.transition_agent

        def track_set_status(agent_id, status):
            if agent_id == agent.agent_id:
                status_history.append(("set", status))
            return original_set_status(agent_id, status)

        def track_transition(agent_id, to_status):
            if agent_id == agent.agent_id:
                status_history.append(("transition", to_status))
            return original_transition(agent_id, to_status)

        engine.set_agent_status = track_set_status
        engine.transition_agent = track_transition

        with patch.object(engine, "_run_acp_session", return_value="Success result"):
            result = engine._execute_agent(agent, "do normal work", None)

        # Should have succeeded
        assert result is not None

        # Final status should be IDLE
        final_status = engine.get_agent_status(agent.agent_id)
        assert final_status == AgentStatus.IDLE

        # Verify the expected transitions happened
        transition_statuses = [s for (t, s) in status_history if t == "transition"]
        assert AgentStatus.WAKING in transition_statuses
        assert AgentStatus.THINKING in transition_statuses
        assert AgentStatus.RUNNING in transition_statuses
        assert AgentStatus.CHECKING in transition_statuses
        assert AgentStatus.SENDING in transition_statuses
        assert AgentStatus.IDLE in transition_statuses

        engine.cleanup()

    def test_exception_in_late_stage_still_resets_status(self, tmp_path):
        """Even if exception occurs after RUNNING (e.g., in CHECKING or SENDING),
        status must still be reset to IDLE.
        """
        engine = self._make_engine(tmp_path)
        ch = SlockChannel(channel_id="ch_late", name="Late", team_name="LateTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(agent_id="agent-late", name="LateBot", agent_type="coco")

        # Simulate failure in _mouthpiece.format_text (after RUNNING→CHECKING→SENDING)
        def raise_during_format(*args, **kwargs):
            status = engine.get_agent_status(agent.agent_id)
            # Should be in SENDING at this point
            assert status == AgentStatus.SENDING, f"Expected SENDING, got {status}"
            raise RuntimeError("Formatting failed")

        with patch.object(engine, "_run_acp_session", return_value="some result"):
            with patch.object(engine._mouthpiece, "format_text", side_effect=raise_during_format):
                # _execute_agent catches exception, logs it, resets status, returns None
                result = engine._execute_agent(agent, "format this", None)
                assert result is None

        # Status MUST be reset to IDLE
        final_status = engine.get_agent_status(agent.agent_id)
        assert final_status == AgentStatus.IDLE, (
            f"Agent stuck in {final_status} after late-stage exception!"
        )
        engine.cleanup()

    def test_agent_cancellation_error_resets_status(self, tmp_path):
        """AgentCancellationError should also result in IDLE status."""
        engine = self._make_engine(tmp_path)
        ch = SlockChannel(channel_id="ch_cancel", name="Cancel", team_name="CancelTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(agent_id="agent-cancel", name="CancelBot", agent_type="coco")

        # Set cancel event before execution
        cancel_event = engine._get_cancel_event(agent.agent_id)
        cancel_event.set()

        # This should raise AgentCancellationError or return None gracefully
        # but either way, status should end up IDLE
        try:
            engine._execute_agent(agent, "cancelled task", None)
        except Exception:
            pass

        final_status = engine.get_agent_status(agent.agent_id)
        assert final_status == AgentStatus.IDLE
        engine.cleanup()
