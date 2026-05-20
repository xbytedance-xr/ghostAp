"""Tests for Slock Council orchestration and card rendering."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.slock_engine.models import AgentIdentity, CouncilStatus
from src.slock_engine.slash_commands import SlockCommandAction, is_slock_command, parse_slock_command


def _agent(agent_id: str, name: str, role: str = "coder") -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, name=name, role=role, agent_type="codex")


def _fake_engine(responses: dict[str, str]) -> MagicMock:
    engine = MagicMock()
    engine.chat_id = "chat-1"
    engine.root_path = "/tmp/project"
    engine.build_agent_prompt.side_effect = lambda agent, prompt: f"{agent.name}::{prompt}"
    engine.run_agent_session.side_effect = lambda agent, prompt, timeout=None: responses.get(
        agent.agent_id,
        f"{agent.name} answer",
    )
    engine.router.extract_skill_keywords.return_value = ["design"]
    engine.memory.record_skill_feedback.return_value = []
    return engine


def test_parse_council_commands():
    assert parse_slock_command("/council 评审这个方案").action == SlockCommandAction.COUNCIL
    assert parse_slock_command("/council 评审这个方案").args == "评审这个方案"
    assert parse_slock_command("/slock council 评审这个方案").action == SlockCommandAction.COUNCIL
    assert parse_slock_command("/slock council 评审这个方案").args == "评审这个方案"


def test_council_command_requires_managed_chat():
    manager = MagicMock()
    manager.is_managed_chat.return_value = True
    assert is_slock_command("/council 评审方案", chat_id="chat-1", manager=manager) is True

    manager.is_managed_chat.return_value = False
    assert is_slock_command("/council 评审方案", chat_id="chat-1", manager=manager) is False


def test_council_run_collects_anonymous_reviews_and_final_synthesis():
    from src.slock_engine.council_manager import CouncilManager

    agents = [
        _agent("coder-1", "Coder", "coder"),
        _agent("reviewer-1", "Reviewer", "reviewer"),
        _agent("architect-1", "Architect", "architect"),
    ]
    responses = {
        "coder-1": "实现方案: 使用现有 Slock 引擎扩展。",
        "reviewer-1": "FINAL RANKING:\n1. Response A\n2. Response C\n3. Response B",
        "architect-1": "最终建议: 采用三阶段 Council 流程。",
    }
    engine = _fake_engine(responses)
    stage_events: list[CouncilStatus] = []

    run = CouncilManager(engine=engine).run(
        "评审 Slock council 方案",
        participants=agents[:2],
        chairman=agents[2],
        on_stage=lambda current: stage_events.append(current.status),
        timeout=42,
    )

    assert run.status == CouncilStatus.COMPLETED
    assert [r.label for r in run.responses] == ["Response A", "Response B"]
    assert run.label_to_agent == {"Response A": "coder-1", "Response B": "reviewer-1"}
    assert run.reviews[0].reviewer_agent_id in {"coder-1", "reviewer-1"}
    assert run.reviews[0].parsed_ranking
    assert run.aggregate_rankings[0].label == "Response A"
    assert run.final_response
    assert CouncilStatus.STAGE1_DONE in stage_events
    assert CouncilStatus.STAGE2_DONE in stage_events
    assert CouncilStatus.COMPLETED in stage_events


def test_council_feedback_uses_aggregate_rank_scores():
    from src.slock_engine.council_manager import CouncilManager

    agents = [_agent("a1", "A"), _agent("a2", "B")]
    engine = _fake_engine(
        {
            "a1": "FINAL RANKING:\n1. Response A\n2. Response B",
            "a2": "FINAL RANKING:\n1. Response A\n2. Response B",
        }
    )

    run = CouncilManager(engine=engine).run("选择实现方案", participants=agents, timeout=10)

    assert run.aggregate_rankings[0].agent_id == "a1"
    feedback_calls = engine.memory.record_skill_feedback.call_args_list
    assert len(feedback_calls) == 2
    assert feedback_calls[0].kwargs["quality_score"] > feedback_calls[1].kwargs["quality_score"]


def test_build_council_card_shows_all_three_stages():
    from src.slock_engine.card_templates import build_council_card
    from src.slock_engine.council_manager import CouncilManager

    agents = [_agent("a1", "A"), _agent("a2", "B")]
    engine = _fake_engine(
        {
            "a1": "FINAL RANKING:\n1. Response A\n2. Response B",
            "a2": "FINAL RANKING:\n1. Response A\n2. Response B",
        }
    )
    run = CouncilManager(engine=engine).run("评审方案", participants=agents, timeout=10)

    card = build_council_card(run, channel_id="chat-1")
    blob = str(card)

    assert card["schema"] == "2.0"
    assert "独立意见" in blob
    assert "匿名互评" in blob
    assert "主席综合" in blob
    assert "Response A" in blob
