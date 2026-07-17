"""Executable evidence for the pre-actor employee runtime contract.

These tests intentionally describe the v1 gap.  Later implementation stages
replace the assertions with the persistent actor contract instead of silently
changing what "READY" means.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.engine import SlockEngine
from src.slock_engine.models import AgentIdentity


def test_v1_employee_tasks_create_and_close_one_session_per_prompt(tmp_path) -> None:
    """A ready Channel does not imply a reusable model session in v1."""

    sessions = []

    def create_session(**_kwargs):
        session = MagicMock()
        result = MagicMock()
        result.text = f"result-{len(sessions) + 1}"
        session.send_prompt.return_value = result
        sessions.append(session)
        return session

    engine = SlockEngine(
        chat_id="oc_contract",
        root_path=str(tmp_path),
        engine_name="RuntimeContract",
    )
    agent = AgentIdentity(
        agent_id="agt_contract",
        name="Contract Employee",
        agent_type="coco",
        owner_group="oc_contract",
    )

    with (
        patch("src.slock_engine.engine.create_engine_session", side_effect=create_session),
        patch("src.slock_engine.engine.close_session_safely") as close_session,
    ):
        assert engine._run_acp_session(agent, "first") == "result-1"  # noqa: SLF001
        assert engine._run_acp_session(agent, "second") == "result-2"  # noqa: SLF001

    assert len(sessions) == 2
    assert close_session.call_args_list == [
        ((sessions[0],),),
        ((sessions[1],),),
    ]
    assert engine._agent_sessions == {}  # noqa: SLF001
