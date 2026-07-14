"""Tests for slock role creation with parameter parsing.

Covers:
- AC6: /new-role Coder --tool codex --model o3-pro --emoji 🔧 creates correct AgentIdentity
- AC7: /new-role SimpleAgent (no params) uses defaults
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.model_selection import DEFAULT_MODEL_OPTION_VALUE


@pytest.fixture(autouse=True)
def _bypass_slock_permission():
    """Bypass permission gate for all role creation tests in this module."""
    with patch(
        "src.feishu.handlers.slock.SlockHandler._check_slock_permission",
        return_value=True,
    ):
        yield


def _collect_card_values(node):
    if isinstance(node, dict):
        values = []
        if isinstance(node.get("value"), dict):
            values.append(node["value"])
        for value in node.values():
            values.extend(_collect_card_values(value))
        return values
    if isinstance(node, list):
        values = []
        for item in node:
            values.extend(_collect_card_values(item))
        return values
    return []


class TestCreateRoleWithParams:
    """AC6: Parameterized role creation."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        # Capture the registered agent
        engine.registry.register = MagicMock()
        return engine

    def test_create_role_with_all_params(self):
        """AC6: --tool codex --model o3-pro --emoji 🔧 sets fields correctly."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", 'Coder --tool codex --model o3-pro --emoji 🔧')

        # Verify the registered agent has correct fields
        call_args = engine.registry.register.call_args
        agent = call_args[0][0]  # First positional arg

        assert agent.name == "Coder"
        assert agent.agent_type == "codex"
        assert agent.model_name == "o3-pro"
        assert agent.emoji == "🔧"

    def test_create_role_records_workspace_and_notes_paths(self):
        """AgentIdentity persists the per-agent workspace and notes paths from the spec."""
        handler = self._make_handler()
        engine = self._make_engine()
        engine.memory.initialize_agent_workspace.return_value = {
            "memory_path": "/tmp/slock/agents/codex-default-Coder/MEMORY.md",
            "notes_path": "/tmp/slock/agents/codex-default-Coder/NOTES.md",
            "workspace_path": "/tmp/slock/agents/codex-default-Coder/workspace",
        }

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Coder --tool codex")

        agent = engine.registry.register.call_args[0][0]
        assert agent.memory_path.endswith("MEMORY.md")
        assert agent.notes_path.endswith("NOTES.md")
        assert agent.workspace_path.endswith("workspace")

    def test_create_role_with_prompt(self):
        """--prompt sets system_prompt field."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Writer --tool claude --prompt 'You are a writer'")

        call_args = engine.registry.register.call_args
        agent = call_args[0][0]

        assert agent.name == "Writer"
        assert agent.agent_type == "claude"
        assert agent.system_prompt == "You are a writer"

    def test_create_role_partial_params(self):
        """Only some params provided — others use defaults."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Reviewer --tool gemini")

        call_args = engine.registry.register.call_args
        agent = call_args[0][0]

        assert agent.name == "Reviewer"
        assert agent.agent_type == "gemini"
        assert agent.model_name == ""  # default
        assert agent.emoji == "👨‍💻"  # pick_unique_emoji assigns coder pool first entry

    def test_create_role_ttadk_is_rejected(self):
        """TTADK is no longer a supported Slock role creation tool."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Bridge --tool ttadk")

        engine.registry.register.assert_not_called()
        handler.reply_text.assert_called_once()
        assert "ttadk" in handler.reply_text.call_args[0][1]


class TestCreateRoleDefaults:
    """AC7: Default role creation without parameters."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.registry.register = MagicMock()
        engine.memory.agent_memory_path.return_value = "/tmp/slock/agents/agent/MEMORY.md"
        engine.memory.write_agent_memory = MagicMock()
        return engine

    def test_create_role_name_only_shows_tool_selection_card(self):
        """`/new-role SimpleAgent` starts the Feishu tool/model selection flow."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch(
            "src.workflow_engine.tool_registry.get_available_tools",
            return_value={
                "traex": "默认编程工具",
                "coco": "默认协作工具",
                "codex": "代码实现",
                "aiden": "AI 编程助手",
                "claude": "评审与长文",
                "gemini": "多模态与代码",
            },
        ):
            handler.create_role("msg_1", "chat_test", "SimpleAgent")

        engine.registry.register.assert_not_called()
        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        card_text = json.dumps(card, ensure_ascii=False)
        assert "选择工具" in card_text
        assert "SimpleAgent" in card_text
        values = _collect_card_values(card)
        assert any(v.get("action") == "slock_new_role_select_tool" and v.get("tool_name") == "traex" for v in values)
        assert any(v.get("action") == "slock_new_role_select_tool" and v.get("tool_name") == "coco" for v in values)
        assert any(v.get("action") == "slock_new_role_select_tool" and v.get("tool_name") == "codex" for v in values)
        assert not any(v.get("action") == "slock_new_role_select_tool" and v.get("tool_name") == "ttadk" for v in values)

    def test_create_role_name_only_filters_unavailable_tools_from_card(self):
        """The Slock tool picker only shows tools available in this environment."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch(
            "src.workflow_engine.tool_registry.get_available_tools",
            return_value={"traex": "默认编程工具", "coco": "协作工具", "ttadk": "CLI 桥接"},
        ):
            handler.create_role("msg_1", "chat_test", "SimpleAgent")

        card = json.loads(handler.reply_card.call_args[0][1])
        values = _collect_card_values(card)
        tool_names = {v.get("tool_name") for v in values if v.get("action") == "slock_new_role_select_tool"}
        assert tool_names == {"traex", "coco"}

    def test_global_hire_name_only_preserves_flag_in_tool_card(self):
        handler = self._make_handler()
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager = MagicMock(return_value=manager)

        with (
            patch(
                "src.workflow_engine.tool_registry.get_available_tools",
                return_value={"codex": "代码实现"},
            ),
            patch("src.thread.manager.get_current_sender_id", return_value="ou_admin"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
        ):
            handler.create_role(
                "msg_1", "chat_test", "Atlas", global_hire=True
            )

        card = json.loads(handler.reply_card.call_args[0][1])
        values = _collect_card_values(card)
        tool_values = [
            value
            for value in values
            if value.get("action") == "slock_new_role_select_tool"
        ]
        assert tool_values
        assert all(value.get("global_hire") is True for value in tool_values)

    def test_global_hire_tool_card_requires_admin_main_bot_dm(self):
        handler = self._make_handler()
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_other"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
        ):
            handler.create_role("msg_1", "chat_test", "Atlas", global_hire=True)

        handler.reply_card.assert_not_called()
        assert "仅允许配置管理员" in handler.reply_text.call_args.args[1]

    def test_select_tool_shows_model_selection_card_with_slock_action(self):
        """Tool choice reuses ACP model discovery but keeps the Slock create-role action."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        models = [
            SimpleNamespace(
                name="gpt-5",
                description="fast",
                reasoning_efforts=("high", "xhigh"),
                adapted_reasoning_effort="high",
                selection_variants=(),
                is_default=True,
            )
        ]
        with patch("src.feishu.handlers.slock.fetch_acp_models", return_value=models):
            handler.handle_new_role_select_tool(
                "msg_1",
                "chat_test",
                {"role_name": "SimpleAgent", "tool_name": "codex"},
            )

        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        card_text = json.dumps(card, ensure_ascii=False)
        assert "SimpleAgent" in card_text
        assert "codex" in card_text
        assert "gpt-5" in card_text
        values = _collect_card_values(card)
        assert any(
            v.get("action") == "slock_new_role_select_model"
            and v.get("tool_name") == "codex"
            and v.get("model_name") == "gpt-5/high"
            and v.get("role_name") == "SimpleAgent"
            for v in values
        )
        assert {
            "slock_new_role_select_model_group",
            "slock_new_role_select_model_effort",
        }.issubset({v.get("action") for v in values})

    def test_model_cascade_change_repaints_slock_flow_without_creating_role(self):
        """Changing effort keeps the employee payload and waits for confirmation."""
        handler = self._make_handler()
        handler.update_card = MagicMock(return_value=True)
        handler.create_role = MagicMock()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)
        models = [
            SimpleNamespace(
                name="gpt-5",
                description="fast",
                reasoning_efforts=("high", "xhigh"),
                adapted_reasoning_effort="high",
                selection_variants=(),
                is_default=True,
            )
        ]

        with patch("src.feishu.handlers.slock.fetch_acp_models", return_value=models):
            handler.handle_new_role_model_cascade_select(
                "msg_1",
                "chat_test",
                {
                    "action": "slock_new_role_select_model_effort",
                    "role_name": "SimpleAgent",
                    "tool_name": "codex",
                    "model_group": "gpt-5",
                    "model_profile": "standard",
                    "_option": "xhigh",
                },
            )

        handler.create_role.assert_not_called()
        handler.update_card.assert_called_once()
        card = json.loads(handler.update_card.call_args[0][1])
        values = _collect_card_values(card)
        assert any(
            value.get("action") == "slock_new_role_select_model"
            and value.get("model_name") == "gpt-5/xhigh"
            and value.get("role_name") == "SimpleAgent"
            for value in values
        )

    def test_select_ttadk_tool_is_rejected_without_acp_models(self):
        """Stale TTADK callbacks are rejected and do not fetch ACP models."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch("src.feishu.handlers.slock.fetch_acp_models") as mock_fetch:
            handler.handle_new_role_select_tool(
                "msg_1",
                "chat_test",
                {"role_name": "Bridge", "tool_name": "ttadk"},
            )

        mock_fetch.assert_not_called()
        engine.registry.register.assert_not_called()
        handler.reply_text.assert_called_once()
        assert "ttadk" in handler.reply_text.call_args[0][1]

    def test_select_model_creates_role(self):
        """Model choice is the point where the role is actually created."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_new_role_select_model(
            "msg_1",
            "chat_test",
            {"role_name": "SimpleAgent", "tool_name": "codex", "model_name": "gpt-5"},
        )

        agent = engine.registry.register.call_args[0][0]
        assert agent.name == "SimpleAgent"
        assert agent.agent_type == "codex"
        assert agent.model_name == "gpt-5"
        assert agent.agent_id == "codex:gpt-5:SimpleAgent"
        assert "Core Directives" in agent.system_prompt
        engine.memory.write_agent_memory.assert_called_once()

    def test_legacy_role_keeps_composite_model_variant(self):
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_new_role_select_model(
            "msg_1",
            "chat_test",
            {
                "role_name": "LegacyAgent",
                "tool_name": "traex",
                "model_name": "gpt-5.6-sol/max/xhigh",
                "model_group": "gpt-5.6-sol",
                "model_profile": "max",
                "model_effort": "xhigh",
            },
        )

        agent = engine.registry.register.call_args.args[0]
        assert agent.model_name == "gpt-5.6-sol/max/xhigh"

    def test_global_hire_final_model_selection_preserves_flag(self):
        handler = self._make_handler()
        handler.create_role = MagicMock()

        handler.handle_new_role_select_model(
            "msg_1",
            "chat_test",
            {
                "role_name": "Atlas",
                "tool_name": "traex",
                "model_name": "gpt-5.6-sol/max/xhigh",
                "model_group": "gpt-5.6-sol",
                "model_profile": "max",
                "model_effort": "xhigh",
                "global_hire": True,
            },
        )

        handler.create_role.assert_called_once_with(
            "msg_1",
            "chat_test",
            "Atlas --tool traex --model gpt-5.6-sol --profile max --effort xhigh",
            None,
            global_hire=True,
        )

    def test_global_hire_without_department_service_fails_closed(self):
        handler = self._make_handler()
        handler.ctx.employee_hire_service = None
        handler.ctx.employee_hire_readiness = None
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})
        handler._get_engine_manager = MagicMock()
        handler._get_global_registry = MagicMock()

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_admin"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
            patch("src.thread.manager.get_current_tenant_key", return_value="tenant_a"),
        ):
            handler.handle_new_role_select_model(
                "msg_1",
                "chat_test",
                {
                    "role_name": "Atlas",
                    "tool_name": "traex",
                    "model_name": "gpt-5.6-sol/max/xhigh",
                    "model_group": "gpt-5.6-sol",
                    "model_profile": "max",
                    "model_effort": "xhigh",
                    "global_hire": True,
                },
            )

        handler._get_engine_manager.assert_not_called()
        handler._get_global_registry.assert_not_called()
        assert "独立飞书智能体" in handler.reply_text.call_args.args[1]
        assert "安全门禁" in handler.reply_text.call_args.args[1]
        assert "readiness" in handler.reply_text.call_args.args[1]

    def test_global_hire_reports_specific_runtime_readiness_blockers(self):
        handler = self._make_handler()
        handler.ctx.employee_hire_service = None
        handler.ctx.employee_hire_readiness = MagicMock(
            return_value=SimpleNamespace(
                ready=False,
                blockers=("visible_employee_limit", "release_evidence"),
            )
        )
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_admin"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
            patch("src.thread.manager.get_current_tenant_key", return_value="tenant_a"),
        ):
            handler.create_role(
                "msg_1",
                "chat_test",
                "Atlas --tool codex --model gpt-5 --profile standard --effort high",
                global_hire=True,
            )

        message = handler.reply_text.call_args.args[1]
        assert "安全门禁" in message
        assert "autonomous_visible_employee_limit=0" in message
        assert "QA release evidence" in message

    def test_global_hire_cascade_projects_one_runtime_model_selection(self, tmp_path):
        from src.autonomous.journal.anchor import FileAnchor
        from src.autonomous.journal.projections import ProjectionState
        from src.autonomous.journal.writer import JournalWriter
        from src.autonomous.provisioning.hire_service import ProductionEmployeeHireService
        from src.autonomous.workforce.registry import ProjectedAgentRegistry

        handler = self._make_handler()
        writer = JournalWriter.open(
            tmp_path / "journal",
            anchor=FileAnchor(tmp_path / "anchor.json"),
            hmac_key=b"cascade-hire-test-hmac-key-32bytes",
            writer_epoch=1,
        )
        projection = ProjectionState()
        service = ProductionEmployeeHireService(
            writer,
            projection,
            visible_employee_limit=1,
            release_evidence_ready=True,
            credential_keyring_ready=True,
        )
        handler.ctx.employee_hire_service = service
        handler.ctx.employee_hire_readiness = service.readiness
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})
        handler._get_engine_manager = MagicMock()
        handler._get_global_registry = MagicMock()

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_admin"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
            patch("src.thread.manager.get_current_tenant_key", return_value="tenant_a"),
        ):
            handler.handle_new_role_select_model(
                "msg_1",
                "chat_test",
                {
                    "role_name": "Atlas",
                    "tool_name": "traex",
                    "model_name": "gpt-5.6-sol/max/xhigh",
                    "model_group": "gpt-5.6-sol",
                    "model_profile": "max",
                    "model_effort": "xhigh",
                    "global_hire": True,
                },
            )

        employee = next(iter(projection.employees.values()))
        assert employee.name == "Atlas"
        assert employee.tool == "traex"
        assert employee.model == "gpt-5.6-sol"
        assert employee.profile == "max"
        assert employee.effort == "xhigh"
        identity = ProjectedAgentRegistry(
            projection,
            storage_base_path=str(tmp_path / "slock"),
        ).as_slock_identity("tenant_a", employee.agent_id)
        assert identity is not None
        assert identity.model_name == "gpt-5.6-sol/max/xhigh"
        assert identity.model_profile == "max"
        assert identity.reasoning_effort == "xhigh"
        handler._get_engine_manager.assert_not_called()
        handler._get_global_registry.assert_not_called()
        service.close()

    @pytest.mark.parametrize(
        "readiness_provider",
        [
            None,
            lambda: None,
            lambda: SimpleNamespace(ready=False, blockers=()),
            lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
        ],
        ids=["missing", "malformed", "not-ready-empty", "probe-error"],
    )
    def test_global_hire_readiness_is_fail_closed(self, readiness_provider):
        handler = self._make_handler()
        service = MagicMock()
        handler.ctx.employee_hire_service = service
        handler.ctx.employee_hire_readiness = readiness_provider
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_admin"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
            patch("src.thread.manager.get_current_tenant_key", return_value="tenant_a"),
        ):
            handler.create_role(
                "msg_1",
                "chat_test",
                "Atlas --tool codex --model gpt-5/high --effort high",
                global_hire=True,
            )

        service.start_hire.assert_not_called()
        assert "安全门禁" in handler.reply_text.call_args.args[1]

    def test_global_fire_requires_admin_main_bot_dm(self):
        handler = self._make_handler()
        service = MagicMock()
        handler.ctx.employee_fire_service = service
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_other"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
        ):
            handler.fire_employee("msg_1", "chat_dm", "Atlas")

        service.start_fire.assert_not_called()
        assert "仅允许配置管理员" in handler.reply_text.call_args.args[1]

    def test_global_fire_dispatches_production_service_and_discloses_manual_app_cleanup(self):
        from src.autonomous.provisioning.fire_state import FirePhase

        handler = self._make_handler()
        service = MagicMock()
        service.start_fire.return_value = SimpleNamespace(
            phase=FirePhase.ARCHIVED,
            error_code="",
        )
        handler.ctx.employee_fire_service = service
        handler.ctx.settings.admin_user_ids = frozenset({"ou_admin"})

        with (
            patch("src.thread.manager.get_current_sender_id", return_value="ou_admin"),
            patch("src.thread.manager.get_current_is_p2p", return_value=True),
            patch("src.thread.manager.get_current_tenant_key", return_value="tenant_a"),
        ):
            handler.fire_employee("msg_1", "chat_dm", "Atlas --drain")

        request = service.start_fire.call_args.args[0]
        assert request.employee == "Atlas"
        assert request.tenant_key == "tenant_a"
        assert request.drain is True
        message = handler.reply_text.call_args.args[1]
        assert "手动停用或删除" in message
        assert "未声称已删除" in message

    def test_unassigned_task_claim_competition_tries_next_agent_after_failed_claim(self):
        """Automatic assignment broadcasts the claim chance through ranked candidates."""
        from src.feishu.handlers.slock import SlockHandler
        from src.slock_engine.models import AgentIdentity

        handler = SlockHandler(MagicMock())
        handler.reply_text = MagicMock()
        handler._submit_task_execution = MagicMock()

        task = SimpleNamespace(task_id="task-1", content="please review this")
        reviewer = AgentIdentity(agent_id="reviewer", name="Reviewer", owner_group="chat_test")
        coder = AgentIdentity(agent_id="coder", name="Coder", owner_group="chat_test")
        engine = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.add_task.return_value = task
        engine.registry.list_agents.return_value = [reviewer, coder]
        engine.router.rank_agents_for_claim.return_value = [reviewer, coder]
        engine.claim_task.side_effect = [False, True]

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)
        handler._check_assign_rate_limit = MagicMock(return_value=True)

        handler.assign_task("msg_1", "chat_test", "please review this", "")

        assert engine.claim_task.call_args_list[0].args == ("task-1", "reviewer")
        assert engine.claim_task.call_args_list[1].args == ("task-1", "coder")
        handler._submit_task_execution.assert_called_once()
        assert handler._submit_task_execution.call_args.args[2] == coder

    def test_select_default_model_does_not_persist_ui_sentinel(self):
        """Default model selection keeps the sentinel UI-only."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_new_role_select_model(
            "msg_1",
            "chat_test",
            {
                "role_name": "SimpleAgent",
                "tool_name": "coco",
                "model_name": DEFAULT_MODEL_OPTION_VALUE,
                "use_default_model": True,
            },
        )

        agent = engine.registry.register.call_args[0][0]
        assert agent.agent_id == "coco:default:SimpleAgent"
        assert agent.model_name == ""

    def test_create_role_empty_name_shows_usage(self):
        """Empty name shows usage message."""
        handler = self._make_handler()
        handler.create_role("msg_1", "chat_test", "")
        handler.reply_text.assert_called_once()
        assert "用法" in handler.reply_text.call_args[0][1]

    def test_create_role_no_engine_shows_error(self):
        """No active engine shows activation prompt."""
        handler = self._make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "TestAgent")
        handler.reply_text.assert_called_once()
        assert "激活" in handler.reply_text.call_args[0][1]


class TestCreateRoleWithRoleParam:
    """Test --role parameter: explicit role, auto-inference from tool_type, override priority."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.registry.register = MagicMock()
        return engine

    def test_explicit_role_param(self):
        """AC-4: /new-role Alpha --role coder --tool codex sets role='coder', card_color='blue'."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Alpha --role coder --tool codex")

        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "coder"
        assert agent.card_color == "blue"

    def test_role_inferred_from_codex(self):
        """AC-5: /new-role Beta --tool codex (no --role) infers role='coder'."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Beta --tool codex")

        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "coder"
        assert agent.card_color == "blue"

    def test_role_inferred_from_claude(self):
        """--tool claude infers role='reviewer'."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Gamma --tool claude")

        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "reviewer"
        assert agent.card_color == "orange"

    def test_role_inferred_from_coco(self):
        """--tool coco infers role='writer'."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Delta --tool coco")

        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "writer"
        assert agent.card_color == "green"


class TestRoleInfoDetails:
    """Role info should expose memory summary and historical task stats."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.registry.register = MagicMock()
        return engine

    def test_role_info_includes_memory_summary_and_task_stats(self):
        from src.slock_engine.models import AgentIdentity, SlockMemory, SlockTask, TaskStatus

        handler = self._make_handler()
        agent = AgentIdentity(
            agent_id="codex:default:Coder",
            name="Coder",
            emoji="🔧",
            agent_type="codex",
            role="coder",
            owner_group="chat_test",
        )
        done_task = SlockTask(content="Done task", claimed_by=agent.agent_id, status=TaskStatus.DONE)
        active_task = SlockTask(content="Active task", claimed_by=agent.agent_id, status=TaskStatus.IN_PROGRESS)
        engine = MagicMock()
        engine.registry.find_by_name.return_value = agent
        engine.get_agent_status.return_value = "idle"
        engine.channel.channel_id = "chat_test"
        engine.tasks = [done_task, active_task]
        engine.memory.read_agent_memory.return_value = SlockMemory(
            role="Backend coder",
            key_knowledge="Python service conventions",
            active_context="Working on login",
        )
        engine.memory.read_skill_profiles.return_value = []

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.show_role_info("msg_info", "chat_test", "Coder")

        # Current implementation renders a card (not plain text)
        handler.reply_card.assert_called_once()
        import json
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        elements = card["body"]["elements"]
        # Collect all markdown content including inside collapsible panels
        all_content = ""
        for e in elements:
            if e.get("tag") == "markdown":
                all_content += " " + e.get("content", "")
            elif e.get("tag") == "collapsible_panel":
                for inner in e.get("elements", []):
                    if inner.get("tag") == "markdown":
                        all_content += " " + inner.get("content", "")
        # Verify memory key_knowledge is shown in collapsible panel
        assert "Python service conventions" in all_content
        # Verify current task is shown
        assert "Active task" in all_content
        # Verify history task is shown
        assert "Done task" in all_content

    def test_create_role_from_onboarding_template(self):
        """`--template onboarding` uses the global template market defaults."""
        handler = self._make_handler()
        engine = self._make_engine()
        engine.memory.read_agent_template.return_value = {
            "name": "onboarding",
            "tool_type": "coco",
            "role": "writer",
            "emoji": "🧭",
            "system_prompt": "You guide new team members.",
        }
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Guide --template onboarding")

        agent = engine.registry.register.call_args[0][0]
        assert agent.name == "Guide"
        assert agent.emoji == "✍️"  # pick_unique_emoji overrides template emoji
        assert agent.role == "writer"
        assert agent.agent_type == "coco"
        assert agent.system_prompt == "You guide new team members."

    def test_create_role_forks_existing_role_memory_and_skill_profile(self):
        """`--fork Coder` copies directive, memory, and skill profiles into the new role."""
        from src.slock_engine.models import AgentIdentity, SkillProfile, SlockMemory

        handler = self._make_handler()
        engine = self._make_engine()
        source = AgentIdentity(
            agent_id="codex:default:Coder",
            name="Coder",
            emoji="🔧",
            agent_type="codex",
            role="coder",
            system_prompt="Source directive",
            owner_group="chat_test",
        )
        source_memory = SlockMemory(role="Source role", key_knowledge="Source knowledge")
        source_profiles = [SkillProfile(tag="code", success_rate=95, total_tasks=4)]
        engine.registry.find_by_name.return_value = source
        engine.memory.read_agent_memory.return_value = source_memory
        engine.memory.read_skill_profiles.return_value = source_profiles
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "CoderFork --fork Coder")

        agent = engine.registry.register.call_args[0][0]
        assert agent.agent_type == "codex"
        assert agent.role == "coder"
        assert agent.system_prompt == "Source directive"
        engine.memory.write_agent_memory.assert_called_once()
        written_memory = engine.memory.write_agent_memory.call_args[0][1]
        assert "Forked from codex:default:Coder" in written_memory.active_context
        engine.memory.write_skill_profiles.assert_called_once_with(agent.agent_id, source_profiles)

    def test_explicit_role_overrides_tool_inference(self):
        """Explicit --role takes priority over tool_type inference."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Epsilon --tool codex --role writer")

        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "writer"
        assert agent.card_color == "green"

    def test_unknown_tool_rejected(self):
        """Unknown tool_type is rejected with error listing valid tools."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Zeta --tool unknown_tool")

        # Should NOT register — validation rejects unknown tool
        engine.registry.register.assert_not_called()
        # Should reply with error listing valid tools
        handler.reply_text.assert_called_once()
        error_msg = handler.reply_text.call_args[0][1]
        assert "无效" in error_msg or "invalid" in error_msg.lower()
        assert "claude" in error_msg
        assert "codex" in error_msg

    def test_create_role_with_default_tool_after_selection_infers_writer(self):
        """Finalized default tool_type='coco' infers role='writer'."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Eta --tool coco")

        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "writer"  # coco → writer
        assert agent.card_color == "green"


class TestRoleWhitelistValidation:
    """Test role and tool_type whitelist validation (security audit fix)."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.registry.register = MagicMock()
        return engine

    def test_invalid_role_rejected(self):
        """--role admin is rejected with error listing valid roles."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "TestAgent --role admin")

        # Should NOT register
        engine.registry.register.assert_not_called()
        # Error message should list valid roles
        handler.reply_text.assert_called_once()
        error_msg = handler.reply_text.call_args[0][1]
        assert "admin" in error_msg
        assert "coder" in error_msg
        assert "writer" in error_msg
        assert "reviewer" in error_msg

    def test_invalid_tool_rejected(self):
        """--tool fake is rejected with error listing valid tool types."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "TestAgent --tool fake")

        # Should NOT register
        engine.registry.register.assert_not_called()
        # Error message should list valid tools
        handler.reply_text.assert_called_once()
        error_msg = handler.reply_text.call_args[0][1]
        assert "fake" in error_msg
        assert "codex" in error_msg
        assert "claude" in error_msg
        assert "coco" in error_msg

    def test_valid_role_accepted(self):
        """--role coder is accepted and agent is created with correct role."""
        handler = self._make_handler()
        engine = self._make_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "TestAgent --role coder --tool codex")

        # Should register successfully
        engine.registry.register.assert_called_once()
        agent = engine.registry.register.call_args[0][0]
        assert agent.role == "coder"
        assert agent.name == "TestAgent"


# ============================================================
# Task 10: /task assign quoted parsing boundary tests
# ============================================================


class TestTaskAssignQuotedParsing:
    """Test _parse_assign_args and /task assign with quoted multi-word arguments."""

    def test_both_quoted(self):
        """/task assign is deprecated even for quoted arguments."""
        from src.slock_engine.slash_commands import parse_slock_command
        cmd = parse_slock_command('/task assign "multi word task" "Role Name"')
        assert cmd.action.value == "unknown"
        assert "deprecated" in cmd.args.lower()

    def test_quoted_task_unquoted_role(self):
        """/task assign is deprecated for quoted task and role input."""
        from src.slock_engine.slash_commands import parse_slock_command
        cmd = parse_slock_command('/task assign "build the feature" coder')
        assert cmd.action.value == "unknown"
        assert "deprecated" in cmd.args.lower()

    def test_simple_two_words(self):
        """/task assign simple input is deprecated."""
        from src.slock_engine.slash_commands import parse_slock_command
        cmd = parse_slock_command("/task assign fix_bug reviewer")
        assert cmd.action.value == "unknown"
        assert "deprecated" in cmd.args.lower()

    def test_multi_word_unquoted_last_is_role(self):
        """/task assign no longer parses target role from the final word."""
        from src.slock_engine.slash_commands import parse_slock_command
        cmd = parse_slock_command("/task assign fix the login bug coder")
        assert cmd.action.value == "unknown"
        assert "deprecated" in cmd.args.lower()

    def test_single_word_no_role(self):
        """/task assign single-word input is deprecated."""
        from src.slock_engine.slash_commands import parse_slock_command
        cmd = parse_slock_command("/task assign cleanup")
        assert cmd.action.value == "unknown"
        assert "deprecated" in cmd.args.lower()

    def test_empty_assign(self):
        """Empty /task assign is deprecated."""
        from src.slock_engine.slash_commands import parse_slock_command
        cmd = parse_slock_command("/task assign")
        assert cmd.action.value == "unknown"
        assert "deprecated" in cmd.args.lower()

    def test_parse_assign_args_directly(self):
        """Direct unit test of _parse_assign_args helper."""
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args('"write documentation" "Tech Writer"')
        assert content == "write documentation"
        assert role == "Tech Writer"

    def test_parse_assign_args_empty(self):
        """_parse_assign_args with empty string returns empty tuple."""
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args("")
        assert content == ""
        assert role == ""

    def test_parse_assign_args_malformed_quotes(self):
        """_parse_assign_args handles malformed quotes via fallback."""
        from src.slock_engine.slash_commands import _parse_assign_args
        # Unbalanced quote — falls back to rsplit
        content, role = _parse_assign_args('"unclosed quote task role')
        # Should still return something reasonable (fallback behavior)
        assert isinstance(content, str)
        assert isinstance(role, str)


class TestRoleListShowsCreatedAgent:
    """AC-02: /role list shows newly created agent after registration."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler.__new__(SlockHandler)
        handler.context = ctx
        handler.reply_text = MagicMock(return_value=True)
        handler.reply_card = MagicMock(return_value="card_msg_id")
        handler.send_card_to_chat = MagicMock(return_value="card_msg_id")
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        return handler

    def _make_engine_with_agents(self, agents):
        from src.slock_engine.models import AgentStatus
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.registry.list_agents.return_value = agents
        engine.get_agent_status.return_value = AgentStatus.IDLE
        return engine

    def test_list_roles_shows_registered_agent(self):
        """After registration, /role list shows the agent name."""
        from src.slock_engine.models import AgentIdentity
        handler = self._make_handler()
        agent = AgentIdentity(
            agent_id="claude:sonnet-4:TestCoder",
            name="TestCoder",
            emoji="🔧",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test",
            role="coder",
        )
        engine = self._make_engine_with_agents([agent])
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.list_roles("msg_1", "chat_test")

        # Current implementation renders a card (not plain text)
        handler.reply_card.assert_called_once()
        import json
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        # Verify header
        assert "角色列表" in card["header"]["title"]["content"]
        # Verify agent name appears somewhere in serialized card
        assert "TestCoder" in card_json
        assert "🔧" in card_json

    def test_list_roles_empty_shows_hint(self):
        """When no roles exist, shows hint card to create one."""
        handler = self._make_handler()
        engine = self._make_engine_with_agents([])
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.list_roles("msg_1", "chat_test")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        assert "没有角色" in card_json
        assert "/new-role" in card_json
