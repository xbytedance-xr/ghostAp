"""Regression tests for build_status_panel_card.

Verifies card structure, agent rendering, task preview, and action buttons
across various agent configurations (empty, single with task, mixed statuses).
"""

from __future__ import annotations

import json
import time

from src.slock_engine.card_templates.status import build_status_panel_card
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockTask, TaskStatus


def test_employee_runtime_status_action_uses_facade_and_pure_card(
    monkeypatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.autonomous.manager.cards import EmployeeRuntimeCardView
    from src.feishu.handlers.slock import SlockHandler

    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_admin")
    monkeypatch.setattr("src.thread.manager.get_current_is_p2p", lambda: True)
    facade = MagicMock()
    facade.get_employee_runtime_status.return_value = EmployeeRuntimeCardView(
        agent_id="agt_atlas",
        name="Atlas",
        emoji="🧭",
        role="reviewer",
        tool="codex",
        model="gpt-test",
        employee_state="active",
        bot_state="ready",
        bot_generation=2,
        actor_state="ready_cold",
        mailbox_depth=0,
        can_accept=True,
        identity_version=4,
        knowledge_generation=3,
    )
    handler = object.__new__(SlockHandler)
    handler.ctx = SimpleNamespace(
        employee_runtime_facade=facade,
        settings=SimpleNamespace(admin_user_ids=frozenset({"ou_admin"})),
    )
    handler.update_card = MagicMock()
    handler.send_text_to_chat = MagicMock()

    handler.handle_card_action(
        "om_status",
        "oc_dm",
        "employee_runtime_show_status",
        {"agent_id": "agt_atlas"},
    )

    facade.get_employee_runtime_status.assert_called_once_with("tenant_a", "agt_atlas")
    payload = handler.update_card.call_args.args[1]
    assert "Bot READY" in payload
    assert "管理员恢复动作" in payload


def test_employee_runtime_mutation_requires_admin_dm(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.feishu.handlers.slock import SlockHandler

    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_other")
    monkeypatch.setattr("src.thread.manager.get_current_is_p2p", lambda: True)
    facade = MagicMock()
    handler = object.__new__(SlockHandler)
    handler.ctx = SimpleNamespace(
        employee_runtime_facade=facade,
        settings=SimpleNamespace(admin_user_ids=frozenset({"ou_admin"})),
    )
    handler.send_text_to_chat = MagicMock()

    handler.handle_card_action(
        "om_status",
        "oc_dm",
        "employee_runtime_recycle_session",
        {"agent_id": "agt_atlas"},
    )

    facade.recycle_employee_session.assert_not_called()
    assert "仅允许配置管理员" in handler.send_text_to_chat.call_args.args[1]


def _make_agent(
    agent_id: str = "agent-1",
    name: str = "TestBot",
    emoji: str = "🤖",
    role: str = "coder",
    agent_type: str = "coco",
    model_name: str = "gpt-4",
) -> AgentIdentity:
    """Helper to create an AgentIdentity with minimal required fields."""
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        emoji=emoji,
        role=role,
        agent_type=agent_type,
        model_name=model_name,
    )


def _make_task(
    task_id: str = "task-1",
    content: str = "Fix the login bug",
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    claimed_by: str = "agent-1",
) -> SlockTask:
    """Helper to create a SlockTask with minimal required fields."""
    return SlockTask(
        task_id=task_id,
        content=content,
        status=status,
        claimed_by=claimed_by,
        claimed_at=time.time(),
        created_in="channel-1",
    )


class TestStatusPanelEmptyAgents:
    """Test case 1: Empty agent list produces valid card with empty-state text."""

    def test_status_panel_empty_agents(self) -> None:
        card = build_status_panel_card(
            agents=[],
            team_name="Ghost Team",
            channel_id="ch-001",
        )

        card_json = json.dumps(card, ensure_ascii=False)

        # Header should contain team name
        assert "Ghost Team" in card["header"]["title"]["content"]

        # Body should contain empty-state message
        assert "暂无已注册的 Agent" in card_json

        # Card structure must have required keys
        assert card["schema"] == "2.0"
        assert "body" in card
        assert "elements" in card["body"]

        # Action buttons (refresh/stop) should still be present even with no agents
        assert "slock_refresh_status" in card_json
        assert "slock_stop" in card_json


class TestStatusPanelSingleAgentWithTask:
    """Test case 2: Single RUNNING agent with an active task."""

    def test_status_panel_single_agent_with_task(self) -> None:
        agent = _make_agent(
            agent_id="agent-alpha",
            name="Alpha",
            emoji="🦊",
            role="coder",
        )
        task = _make_task(
            task_id="task-100",
            content="Implement the new authentication module for SSO integration",
            status=TaskStatus.IN_PROGRESS,
            claimed_by="agent-alpha",
        )

        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)],
            team_name="Dev Squad",
            channel_id="ch-dev",
            current_tasks={"agent-alpha": task},
        )

        card_json = json.dumps(card, ensure_ascii=False)

        # Header
        assert "Dev Squad" in card["header"]["title"]["content"]

        # Agent row should show name and status
        assert "Alpha" in card_json
        assert "运行中" in card_json  # RUNNING -> 运行中

        # Status icon for RUNNING is blue circle
        assert "\U0001f535" in card_json  # 🔵

        # Task content should be truncated to 20 chars with ellipsis
        # Original: "Implement the new authentication module for SSO integration" (59 chars)
        # Truncated: first 20 chars + "…"
        task_preview = task.content[:20]
        assert task_preview in card_json

        # Active count should be 1
        assert "**1** 活跃中" in card_json

        # Total count should be 1
        assert "**1** 个角色" in card_json

        # Action buttons
        assert "slock_refresh_status" in card_json
        assert "slock_stop" in card_json


class TestStatusPanelMultiAgentMixedStatus:
    """Test case 3: Multiple agents with mixed statuses (IDLE, RUNNING, THINKING)."""

    def test_status_panel_multi_agent_mixed_status(self) -> None:
        agents_data = [
            (
                _make_agent(agent_id="a1", name="Coder-Bot", emoji="💻", role="coder"),
                AgentStatus.IDLE,
            ),
            (
                _make_agent(agent_id="a2", name="Runner-Bot", emoji="🏃", role="reviewer"),
                AgentStatus.RUNNING,
            ),
            (
                _make_agent(agent_id="a3", name="Thinker-Bot", emoji="🧠", role="architect"),
                AgentStatus.THINKING,
            ),
        ]

        card = build_status_panel_card(
            agents=agents_data,
            team_name="Multi Team",
            channel_id="ch-multi",
        )

        card_json = json.dumps(card, ensure_ascii=False)

        # Header
        assert "Multi Team" in card["header"]["title"]["content"]

        # Total agent count = 3
        assert "**3** 个角色" in card_json

        # Active count = 2 (RUNNING + THINKING; IDLE is not active)
        assert "**2** 活跃中" in card_json

        # All agent names should appear
        assert "Coder-Bot" in card_json
        assert "Runner-Bot" in card_json
        assert "Thinker-Bot" in card_json

        # Status labels for each
        assert "空闲" in card_json      # IDLE
        assert "运行中" in card_json    # RUNNING
        assert "思考中" in card_json    # THINKING

        # Status icons
        assert "🟢" in card_json  # IDLE icon
        assert "🔵" in card_json  # RUNNING icon
        assert "🟡" in card_json  # THINKING icon

        # Action buttons must be present
        assert "slock_refresh_status" in card_json
        assert "slock_stop" in card_json
        assert "刷新" in card_json
        assert "全部停止" in card_json

        # Individual stop buttons for non-idle agents (RUNNING, THINKING)
        assert "slock_stop_agent" in card_json


# ---------------------------------------------------------------------------
# Integration tests: show_slock_status handler method
# ---------------------------------------------------------------------------


class _FakeChannel:
    """Minimal stand-in for SlockChannel."""

    def __init__(self, channel_id: str = "ch-int", team_name: str = "IntegrationTeam"):
        self.channel_id = channel_id
        self.team_name = team_name
        self.name = "test-channel"


class _FakeRegistry:
    """Minimal registry returning a fixed agent list."""

    def __init__(self, agents: list[AgentIdentity]):
        self._agents = agents

    def list_agents(self, channel_id: str = "") -> list[AgentIdentity]:
        return self._agents


class _FakeMemory:
    """Minimal stand-in for MemoryManager."""

    def read_skill_profiles(self, agent_id: str) -> list:
        return []


class _FakeEngine:
    """Minimal stand-in for SlockEngine used to test show_slock_status."""

    def __init__(
        self,
        agents: list[AgentIdentity],
        statuses: dict[str, AgentStatus],
        tasks: list[SlockTask] | None = None,
        channel: _FakeChannel | None = None,
    ):
        self.registry = _FakeRegistry(agents)
        self._statuses = statuses
        self.tasks = tasks or []
        self.channel = channel or _FakeChannel()
        self.memory = _FakeMemory()

    def get_agent_status(self, agent_id: str) -> AgentStatus | None:
        return self._statuses.get(agent_id)


class _FakeEngineManager:
    """Returns a fixed engine or None."""

    def __init__(self, engine: _FakeEngine | None):
        self._engine = engine

    def get_activated_engine(self, chat_id: str) -> _FakeEngine | None:
        return self._engine


class TestShowSlockStatusIntegration:
    """Integration tests for SlockHandler.show_slock_status via mock objects."""

    def _invoke_show_status(self, engine: _FakeEngine | None) -> str:
        """Invoke show_slock_status and capture the card JSON output.

        We patch the handler minimally: replace _get_engine_manager, reply_card,
        and get_engine_name without importing the full handler dependency tree.
        """
        captured: list[str] = []

        class _StubHandler:
            """Minimal stub of SlockHandler with only the methods show_slock_status needs."""

            def _get_engine_manager(self_inner):
                return _FakeEngineManager(engine)

            def get_engine_name(self_inner, chat_id, project_id=None):
                return "slock"

            def reply_card(self_inner, message_id: str, card_content: str):
                captured.append(card_content)

        # Import the actual method and bind it to the stub
        from src.feishu.handlers.slock import SlockHandler

        # Call the real method with the stub as self
        stub = _StubHandler()
        SlockHandler.show_slock_status(stub, message_id="msg-1", chat_id="chat-1")

        assert len(captured) == 1, "Expected exactly one reply_card call"
        return captured[0]

    def test_engine_not_activated_returns_info_card(self) -> None:
        """When no engine is active, reply with a hint card."""
        # For no-engine case, we need CardBuilder.build_info_card to work.
        # Just test that it calls reply_card without raising.
        captured: list[str] = []

        class _StubNoEngine:
            def _get_engine_manager(self_inner):
                return _FakeEngineManager(None)

            def get_engine_name(self_inner, chat_id, project_id=None):
                return "slock"

            def reply_card(self_inner, message_id: str, card_content: str):
                captured.append(card_content)

        from src.feishu.handlers.slock import SlockHandler

        stub = _StubNoEngine()
        SlockHandler.show_slock_status(stub, message_id="msg-1", chat_id="chat-1")

        assert len(captured) == 1
        output = captured[0]
        # Should contain the "no active team" hint
        assert "没有活跃的 Slock 协作团队" in output or "Slock 状态" in output

    def test_engine_activated_returns_status_panel(self) -> None:
        """When engine is active, reply with the new status panel card (3 sections)."""
        agent = _make_agent(agent_id="int-a1", name="IntBot", emoji="🛠", role="coder")
        task = _make_task(
            task_id="int-t1",
            content="Write integration tests for card rendering",
            status=TaskStatus.IN_PROGRESS,
            claimed_by="int-a1",
        )
        engine = _FakeEngine(
            agents=[agent],
            statuses={"int-a1": AgentStatus.RUNNING},
            tasks=[task],
            channel=_FakeChannel(team_name="IntTeam"),
        )

        output = self._invoke_show_status(engine)
        card = json.loads(output)

        # Section 1: Team overview
        assert "IntTeam" in card["header"]["title"]["content"]
        assert "**1** 个角色" in output

        # Section 2: Agent status row
        assert "IntBot" in output
        assert "运行中" in output

        # Section 3: Action buttons
        assert "slock_refresh_status" in output
        assert "slock_stop" in output


class TestDangerousOperationProtection:
    """Verify '全部停止' button is wrapped in a collapsible_panel (dangerous op protection)."""

    def test_stop_button_in_collapsible_panel(self) -> None:
        agents_data = [
            (
                _make_agent(agent_id="dp-1", name="Worker", emoji="⚒️", role="coder"),
                AgentStatus.RUNNING,
            ),
        ]

        card = build_status_panel_card(
            agents=agents_data,
            team_name="DangerTest",
            channel_id="ch-danger",
        )

        elements = card["body"]["elements"]
        card_json = json.dumps(card, ensure_ascii=False)

        # "全部停止" should still be in the card but within a collapsible panel
        assert "全部停止" in card_json

        # Find the collapsible_panel that contains the stop button
        stop_panel = None
        for el in elements:
            if el.get("tag") == "collapsible_panel":
                panel_json = json.dumps(el, ensure_ascii=False)
                if "slock_stop" in panel_json:
                    stop_panel = el
                    break

        assert stop_panel is not None, "Stop button should be inside a collapsible_panel"
        assert stop_panel["expanded"] is False, "Dangerous panel should be collapsed by default"
        assert "单独停止" in json.dumps(stop_panel["header"], ensure_ascii=False)

    def test_thinking_uses_grey_bg(self) -> None:
        """THINKING status uses grey background (three-tier visual hierarchy)."""
        from src.slock_engine.card_templates.common import STATUS_BG_STYLE_MAP

        assert STATUS_BG_STYLE_MAP[AgentStatus.THINKING] == "grey"


class TestStatusPanelTasksSummary:
    """Test case: tasks_summary dict is rendered in the card body."""

    def test_tasks_summary_rendered_in_card(self) -> None:
        """When tasks_summary is passed, the card JSON contains summary numbers and labels."""
        tasks_summary = {
            "total": 6,
            "todo": 3,
            "in_progress": 2,
            "in_review": 0,
            "done": 1,
        }

        # Minimal agent list (one idle agent) to satisfy the non-empty branch
        agent = _make_agent(agent_id="ts-1", name="SummaryBot", emoji="📝", role="coder")
        card = build_status_panel_card(
            agents=[(agent, AgentStatus.IDLE)],
            team_name="SummaryTeam",
            channel_id="ch-summary",
            tasks_summary=tasks_summary,
        )

        card_json = json.dumps(card, ensure_ascii=False)

        # The task summary line should contain total and per-status counts
        assert "6" in card_json  # total
        assert "待办" in card_json
        assert "3" in card_json  # todo count
        assert "进行中" in card_json
        assert "2" in card_json  # in_progress count
        assert "审查中" in card_json
        assert "0" in card_json  # in_review count
        assert "已完成" in card_json
        assert "1" in card_json  # done count

        # Payload size must stay under Feishu card limit (30 KB)
        payload_bytes = len(card_json.encode("utf-8"))
        assert payload_bytes < 30720, f"Card payload {payload_bytes} bytes exceeds 30720 limit"
