"""Regression tests for autonomous Slock multi-agent collaboration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.intent_router import IntentResult
from src.slock_engine.models import AgentIdentity, SlockTask
from src.slock_engine.slash_commands import SlockCommandAction


def _make_handler():
    from src.feishu.handlers.slock import SlockHandler

    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.slock_autonomous_task_planning_enabled = True
    ctx.settings.slock_nli_timeout = 2.5
    ctx.settings.slock_nli_confidence_threshold = 0.6
    ctx.settings.slock_reply_mode = "direct"
    ctx.slock_engine_manager = MagicMock()

    handler = SlockHandler(ctx)
    handler.reply_card = MagicMock(return_value="plan-card-001")
    handler.reply_text = MagicMock()
    handler.send_card_to_chat = MagicMock()
    handler._check_assign_rate_limit = MagicMock(return_value=True)
    handler._execute_routed_message = MagicMock()
    handler._intent_router = MagicMock()
    handler._intent_router.fast_classify.return_value = IntentResult(
        action=SlockCommandAction.UNKNOWN,
        confidence=0.0,
        params={},
    )
    return handler


def _make_engine_for_plan():
    engine = MagicMock()
    engine.engine_name = "Slock"
    engine.root_path = "/tmp/ghostap"
    engine.channel = MagicMock()
    engine.channel.channel_id = "chat-slock"
    engine.registry.find_by_name.return_value = None
    engine.registry.list_agents.return_value = []
    engine.add_task.return_value = SlockTask(
        task_id="task-autonomous",
        content="实现登录功能并补充测试",
        created_in="chat-slock",
    )

    chain = MagicMock()
    chain.roles = ["planner", "coder", "reviewer", "tester"]
    engine._chain_manager.find_chain_for_task.return_value = chain

    plan = MagicMock()
    plan.plan_id = "plan-autonomous"
    plan.steps = []
    engine._collaboration_orchestrator.create_plan.return_value = plan
    engine._progress_tracker.set_overview_message_id = MagicMock()
    return engine


def test_plain_task_message_starts_collaboration_plan_instead_of_single_agent_route():
    handler = _make_handler()
    engine = _make_engine_for_plan()
    handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

    with patch(
        "src.slock_engine.card_templates.progress.build_collaboration_plan_card",
        return_value={"schema": "2.0", "header": {"title": {"content": "plan"}}},
    ):
        handler.handle_message("msg-001", "chat-slock", "实现登录功能并补充测试", None)

    engine.add_task.assert_called_once_with("实现登录功能并补充测试")
    engine._collaboration_orchestrator.create_plan.assert_called_once()
    handler.reply_card.assert_called_once()
    handler._execute_routed_message.assert_not_called()


def test_autonomous_collaboration_keeps_explicit_mention_on_direct_route():
    handler = _make_handler()
    engine = _make_engine_for_plan()
    agent = AgentIdentity(agent_id="agent-coder", name="Coder", role="coder")
    engine.registry.find_by_name.return_value = agent
    handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

    handler.handle_message("msg-002", "chat-slock", "@Coder 实现登录功能", None)

    handler._execute_routed_message.assert_called_once_with(
        engine, "msg-002", "chat-slock", "@Coder 实现登录功能", None, agent
    )
    engine.add_task.assert_not_called()


def test_autonomous_collaboration_does_not_steal_shell_like_text():
    handler = _make_handler()
    engine = _make_engine_for_plan()
    handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

    handler.handle_message("msg-003", "chat-slock", "pytest tests/ -q", None)

    engine.add_task.assert_not_called()
    handler._execute_routed_message.assert_called_once_with(
        engine, "msg-003", "chat-slock", "pytest tests/ -q", None, target_agent=None
    )


def test_discussion_partner_selection_uses_engine_router_skill_keywords():
    from src.slock_engine.discussion_manager import DiscussionManager

    reviewer = AgentIdentity(agent_id="agent-reviewer", name="Reviewer", role="reviewer")
    security = AgentIdentity(agent_id="agent-security", name="Security", role="security")
    initiator = AgentIdentity(agent_id="agent-coder", name="Coder", role="coder")

    engine = MagicMock()
    engine.registry.list_agents.return_value = [initiator, reviewer, security]
    engine._router.extract_skill_keywords.return_value = ["security"]

    manager = DiscussionManager(engine=engine)

    selected = manager._find_best_discussion_partner(
        initiator,
        "需要检查登录鉴权 security risk",
        channel_id="chat-slock",
    )

    assert selected == "agent-security"


def test_collaboration_task_dispatch_passes_visible_callbacks_to_task_execution():
    from src.slock_engine.engine import SlockEngine

    engine = SlockEngine.__new__(SlockEngine)
    engine._progress_tracker = MagicMock()
    engine._card_send_fn = MagicMock(return_value="card-msg")
    engine._card_update_fn = MagicMock(return_value=True)
    engine._mouthpiece = MagicMock()
    engine._mouthpiece.format_card.return_value = {
        "schema": "2.0",
        "header": {"title": {"content": "Coder"}},
    }
    engine.execute_task = MagicMock(return_value="done")

    task = SlockTask(
        task_id="task-step",
        content="实现登录功能",
        created_in="chat-slock",
    )
    agent = AgentIdentity(agent_id="agent-coder", name="Coder", role="coder")

    SlockEngine._dispatch_collaboration_task(engine, task, agent)

    engine.execute_task.assert_called_once()
    callbacks = engine.execute_task.call_args.args[2]
    callbacks.on_card_send({"schema": "2.0"})
    callbacks.on_agent_done(agent, "step result")

    assert engine._card_send_fn.call_count == 2
    engine._mouthpiece.format_card.assert_called_once_with(
        agent,
        "step result",
        channel_id="chat-slock",
        task_id="task-step",
    )
