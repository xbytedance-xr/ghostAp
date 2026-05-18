"""Unit tests for slock_engine/engine.py and slock_engine/slash_commands.py."""

from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ------------------------------------------------------------------
# Slash commands have zero heavy dependencies — safe to import directly.
# ------------------------------------------------------------------
from src.slock_engine.slash_commands import (
    SlockCommandAction,
    is_slock_command,
    parse_slock_command,
)

# ============================================================
# Slash Commands
# ============================================================


class TestIsSLockCommand:
    """Test is_slock_command with context-aware scoping."""

    @pytest.mark.parametrize("text", [
        "/slock",
        "/slock status",
        "/slock list",
        "/slocks",
        "/new-team Alpha",
    ])
    def test_globally_recognized(self, text):
        """These commands are always captured regardless of chat context."""
        assert is_slock_command(text) is True

    @pytest.mark.parametrize("text", [
        "/new-role Coder",
        "/role list",
        "/task assign something coder",
        "/team dissolve Alpha",
    ])
    def test_recognized_in_managed_chat(self, text):
        """These commands are only captured in managed slock chats."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = True
        assert is_slock_command(text, chat_id="chat_123", manager=manager) is True

    @pytest.mark.parametrize("text", [
        "/new-role Coder",
        "/role list",
        "/task assign something coder",
        "/team dissolve Alpha",
    ])
    def test_not_captured_in_unmanaged_chat(self, text):
        """Team commands passthrough in unmanaged chats."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = False
        assert is_slock_command(text, chat_id="chat_456", manager=manager) is False

    @pytest.mark.parametrize("text", [
        "",
        "/deep",
        "/spec",
        "hello",
        "/exit",
    ])
    def test_not_recognized(self, text):
        assert is_slock_command(text) is False


class TestParseSlockCommand:
    def test_slock_bare(self):
        cmd = parse_slock_command("/slock")
        assert cmd.action == SlockCommandAction.ACTIVATE

    def test_slock_status(self):
        cmd = parse_slock_command("/slock status")
        assert cmd.action == SlockCommandAction.STATUS

    def test_slock_help(self):
        cmd = parse_slock_command("/slock help")
        assert cmd.action == SlockCommandAction.HELP

    def test_slock_list(self):
        cmd = parse_slock_command("/slock list")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_slock_teams(self):
        cmd = parse_slock_command("/slock teams")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_slocks_alias(self):
        cmd = parse_slock_command("/slocks")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_slock_with_args(self):
        cmd = parse_slock_command("/slock build a web app")
        assert cmd.action == SlockCommandAction.ACTIVATE
        assert cmd.args == "build a web app"

    def test_new_team(self):
        cmd = parse_slock_command("/new-team Alpha")
        assert cmd.action == SlockCommandAction.NEW_TEAM
        assert cmd.args == "Alpha"

    def test_new_role(self):
        cmd = parse_slock_command("/new-role Frontend-Dev")
        assert cmd.action == SlockCommandAction.NEW_ROLE
        assert cmd.args == "Frontend-Dev"

    def test_role_list(self):
        cmd = parse_slock_command("/role list")
        assert cmd.action == SlockCommandAction.ROLE_LIST

    def test_role_bare(self):
        cmd = parse_slock_command("/role")
        assert cmd.action == SlockCommandAction.ROLE_LIST

    def test_role_remove(self):
        cmd = parse_slock_command("/role remove Coder")
        assert cmd.action == SlockCommandAction.ROLE_REMOVE
        assert cmd.target == "Coder"

    def test_role_info(self):
        cmd = parse_slock_command("/role info Alice")
        assert cmd.action == SlockCommandAction.ROLE_INFO
        assert cmd.target == "Alice"

    def test_role_shorthand(self):
        cmd = parse_slock_command("/role Bob")
        assert cmd.action == SlockCommandAction.ROLE_INFO
        assert cmd.target == "bob"

    def test_task_list(self):
        cmd = parse_slock_command("/task list")
        assert cmd.action == SlockCommandAction.TASK_LIST

    def test_task_bare(self):
        cmd = parse_slock_command("/task")
        assert cmd.action == SlockCommandAction.TASK_LIST

    def test_task_status(self):
        cmd = parse_slock_command("/task status")
        assert cmd.action == SlockCommandAction.TASK_STATUS

    def test_task_assign_with_role(self):
        cmd = parse_slock_command("/task assign implement login coder")
        assert cmd.action == SlockCommandAction.TASK_ASSIGN
        assert cmd.args == "implement login"
        assert cmd.target == "coder"

    def test_task_assign_without_role(self):
        cmd = parse_slock_command("/task assign something")
        assert cmd.action == SlockCommandAction.TASK_ASSIGN
        assert cmd.args == "something"

    def test_team_list(self):
        cmd = parse_slock_command("/team list")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_team_bare(self):
        cmd = parse_slock_command("/team")
        assert cmd.action == SlockCommandAction.TEAM_LIST

    def test_team_status(self):
        cmd = parse_slock_command("/team status Alpha")
        assert cmd.action == SlockCommandAction.TEAM_STATUS
        assert cmd.target == "Alpha"

    def test_team_dissolve(self):
        cmd = parse_slock_command("/team dissolve Beta")
        assert cmd.action == SlockCommandAction.TEAM_DISSOLVE
        assert cmd.target == "Beta"

    def test_team_shorthand(self):
        cmd = parse_slock_command("/team Gamma")
        assert cmd.action == SlockCommandAction.TEAM_STATUS
        assert cmd.target == "gamma"

    def test_empty_input(self):
        cmd = parse_slock_command("")
        assert cmd.action == SlockCommandAction.UNKNOWN

    def test_unknown_command(self):
        cmd = parse_slock_command("/other stuff")
        assert cmd.action == SlockCommandAction.UNKNOWN


# ============================================================
# SlockEngine — requires `acp` package (skip if unavailable)
# ============================================================

_acp_available = pytest.importorskip("acp", reason="acp package not installed")

from src.slock_engine.agent_registry import AgentRegistry  # noqa: E402
from src.slock_engine.engine import SlockEngine, SlockEngineCallbacks  # noqa: E402
from src.slock_engine.models import (  # noqa: E402
    AgentIdentity,
    AgentStatus,
    SkillProfile,
    SlockChannel,
    SlockMemory,
    TaskStatus,
)
from src.slock_engine.task_router import TaskRouter  # noqa: E402


class TestSlockEngine:
    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None, **kwargs):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock"
        return SlockEngine(
            chat_id="chat1",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
            **kwargs,
        )

    def test_construction(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.chat_id == "chat1"
        assert engine.channel is None
        assert engine.tasks == []

    def test_activate_channel(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch1", team_name="Alpha")
        engine.activate_channel(ch)
        assert engine.channel is not None
        assert engine.channel.channel_id == "ch1"

    def test_activate_channel_creates_workspace(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        # Override root_path to use tmp_path for assertions
        engine.root_path = str(tmp_path / "project_root")
        ch = SlockChannel(channel_id="ch_ws", name="ws-group", team_name="WS Team")
        engine.activate_channel(ch)

        workspace_dir = os.path.join(str(tmp_path / "project_root"), "slock", "ch_ws")
        assert os.path.isdir(workspace_dir), "workspace directory should exist"

        marker_path = os.path.join(workspace_dir, ".slock_channel.json")
        assert os.path.isfile(marker_path), "marker file should exist"

        with open(marker_path, "r", encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["channel_id"] == "ch_ws"
        assert marker["team_name"] == "WS Team"
        assert marker["name"] == "ws-group"
        assert "activated_at" in marker

    @patch("src.slock_engine.engine.create_engine_session")
    def test_default_storage_base_is_project_ghostap_slock(self, mock_create_session, tmp_path):
        """Default Slock storage follows the project-local .ghostap/slock contract."""
        mock_create_session.return_value = None
        root_path = str(tmp_path / "project_root")
        engine = SlockEngine(chat_id="chat_storage", root_path=root_path)

        expected = os.path.join(root_path, ".ghostap", "slock")
        assert engine.memory.base_path == expected
        assert engine.registry.base_path == expected

    @patch("src.slock_engine.engine.create_engine_session")
    def test_activate_channel_writes_canonical_group_marker(self, mock_create_session, tmp_path):
        """Slock channel markers are written under .ghostap/slock/groups/{chat_id}."""
        mock_create_session.return_value = None
        root_path = str(tmp_path / "project_root")
        engine = SlockEngine(chat_id="chat_canonical", root_path=root_path)
        ch = SlockChannel(channel_id="ch_canonical", name="canonical", team_name="Canon")

        engine.activate_channel(ch)

        marker_path = os.path.join(
            root_path,
            ".ghostap",
            "slock",
            "groups",
            "ch_canonical",
            ".slock_channel.json",
        )
        assert os.path.isfile(marker_path)

    @patch("src.slock_engine.engine.create_engine_session")
    def test_activate_channel_initializes_team_workspace_files(self, mock_create_session, tmp_path):
        """Activation creates the auditable team workspace described by the design doc."""
        mock_create_session.return_value = None
        root_path = str(tmp_path / "project_root")
        engine = SlockEngine(chat_id="chat_workspace", root_path=root_path)
        ch = SlockChannel(channel_id="ch_workspace", name="Workspace", team_name="WorkspaceTeam")

        engine.activate_channel(ch)

        workspace = engine.memory.team_workspace_path("ch_workspace")
        assert os.path.isdir(os.path.join(workspace, "agents"))
        assert os.path.isdir(os.path.join(workspace, "shared", "artifacts"))
        assert os.path.isdir(os.path.join(workspace, "shared", "references"))
        assert os.path.isdir(os.path.join(workspace, "shared", "templates"))
        assert os.path.isdir(os.path.join(workspace, "project"))
        assert os.path.isfile(os.path.join(workspace, ".team-config.json"))
        assert os.path.isfile(os.path.join(workspace, ".task-board.json"))

    @patch("src.slock_engine.engine.create_engine_session")
    def test_activate_channel_seeds_global_agent_templates(self, mock_create_session, tmp_path):
        """Activation seeds the global Agent template market, including onboarding."""
        mock_create_session.return_value = None
        engine = SlockEngine(chat_id="chat_templates", root_path=str(tmp_path))
        ch = SlockChannel(channel_id="ch_templates", name="Templates", team_name="TemplateTeam")

        engine.activate_channel(ch)

        templates = engine.memory.list_agent_templates()
        assert "onboarding" in templates
        onboarding = engine.memory.read_agent_template("onboarding")
        assert onboarding["role"] == "writer"
        assert "new team members" in onboarding["system_prompt"]

    def test_execute_agent_archives_user_and_agent_messages(self, tmp_path):
        """Successful agent execution appends JSONL records to the channel archive."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_archive", name="Archive", team_name="ArchiveTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(name="ArchiveBot", emoji="📝", agent_type="coco", owner_group="ch_archive")
        engine.registry.register(agent)

        with patch.object(engine, "_run_acp_session", return_value="archived response"):
            engine._execute_agent(agent, "please archive this", None)

        archive_path = engine.memory.message_archive_path("ch_archive")
        assert os.path.isfile(archive_path)
        with open(archive_path, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]

        assert [record["sender_type"] for record in records] == ["user", "agent"]
        assert records[0]["content"] == "please archive this"
        assert records[1]["agent_id"] == agent.agent_id
        assert records[1]["content"] == "archived response"

    @patch("src.slock_engine.engine.create_engine_session")
    def test_parallel_acp_sessions_close_their_own_session(self, mock_create_session, tmp_path):
        """Parallel agents must not share self._session and close each other's sessions."""
        engine = SlockEngine(chat_id="chat_parallel", root_path=str(tmp_path))
        barrier = threading.Barrier(2)

        def make_session(label: str, delay: float):
            session = MagicMock()
            result = MagicMock()
            result.text = label

            def send_prompt(*args, **kwargs):
                barrier.wait(timeout=2)
                time.sleep(delay)
                return result

            session.send_prompt.side_effect = send_prompt
            return session

        session_a = make_session("A done", 0.03)
        session_b = make_session("B done", 0.0)
        mock_create_session.side_effect = [session_a, session_b]
        agent_a = AgentIdentity(agent_id="agent-a", name="A", agent_type="coco")
        agent_b = AgentIdentity(agent_id="agent-b", name="B", agent_type="coco")

        results: dict[str, str | None] = {}
        threads = [
            threading.Thread(target=lambda: results.setdefault("a", engine._run_acp_session(agent_a, "A"))),
            threading.Thread(target=lambda: results.setdefault("b", engine._run_acp_session(agent_b, "B"))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        assert results == {"a": "A done", "b": "B done"}
        session_a.close.assert_called_once()
        session_b.close.assert_called_once()

    def test_add_task(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Implement feature X")
        assert task.content == "Implement feature X"
        assert task.status == TaskStatus.TODO
        assert len(engine.tasks) == 1

    def test_add_task_persists_group_task_board(self, tmp_path):
        """Tasks are persisted to the group task board under .ghostap/slock."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_board", name="Board", team_name="BoardTeam")
        engine.activate_channel(ch)

        task = engine.add_task("Persist this task")

        board = engine.memory.read_task_board("ch_board")
        assert len(board) == 1
        assert board[0].task_id == task.task_id
        assert board[0].content == "Persist this task"

    def test_claim_task(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Build API")
        result = engine.claim_task(task.task_id, "agent-1")
        assert result is True
        claimed = engine.tasks[0]
        assert claimed.status == TaskStatus.IN_PROGRESS
        assert claimed.claimed_by == "agent-1"

    def test_claim_task_double_claim_fails(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Build API")
        engine.claim_task(task.task_id, "agent-1")
        result = engine.claim_task(task.task_id, "agent-2")
        assert result is False

    def test_complete_task(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Write tests")
        engine.claim_task(task.task_id, "agent-1")
        result = engine.complete_task(task.task_id, "agent-1")
        assert result is True
        assert engine.tasks[0].status == TaskStatus.DONE

    def test_complete_task_persists_done_status(self, tmp_path):
        """Task state changes are reflected in the persisted task board."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_done", name="Done", team_name="DoneTeam")
        engine.activate_channel(ch)
        task = engine.add_task("Ship it")

        engine.claim_task(task.task_id, "agent-1")
        engine.complete_task(task.task_id, "agent-1")

        board = engine.memory.read_task_board("ch_done")
        assert board[0].status == TaskStatus.DONE
        assert board[0].claimed_by == "agent-1"

    def test_complete_task_wrong_agent(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        task = engine.add_task("Task A")
        engine.claim_task(task.task_id, "agent-1")
        result = engine.complete_task(task.task_id, "agent-2")
        assert result is False

    def test_execute_agent_updates_persistent_skill_profile_on_success(self, tmp_path):
        """Successful task execution feeds back into the agent's persisted skill profile."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_skill", name="Skill", team_name="SkillTeam")
        engine.activate_channel(ch)
        agent = AgentIdentity(name="Tester", emoji="🧪", agent_type="coco", owner_group="ch_skill")
        engine.registry.register(agent)

        with patch.object(engine, "_run_acp_session", return_value="tests added"):
            engine._execute_agent(agent, "add regression tests for login", None)

        profiles = engine.memory.read_skill_profiles(agent.agent_id)
        profile_by_tag = {profile.tag: profile for profile in profiles}
        assert "test" in profile_by_tag
        assert profile_by_tag["test"].total_tasks == 1
        assert profile_by_tag["test"].success_rate > 50.0
        assert profile_by_tag["test"].last_active > 0

    def test_idle_agents_observe_successful_tasks_as_potential_skills(self, tmp_path):
        """Idle team members learn potential skills from another agent's completed task."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_observe", name="Observe", team_name="ObserveTeam")
        engine.activate_channel(ch)
        actor = AgentIdentity(agent_id="actor", name="Actor", agent_type="coco", owner_group="ch_observe")
        observer = AgentIdentity(agent_id="observer", name="Observer", agent_type="coco", owner_group="ch_observe")
        engine.registry.register(actor)
        engine.registry.register(observer)
        engine.memory.write_agent_memory(observer.agent_id, SlockMemory(role="Observer"))

        with patch.object(engine, "_run_acp_session", return_value="implemented"):
            engine._execute_agent(actor, "implement a parser", None)

        profiles = engine.memory.read_skill_profiles(observer.agent_id)
        profile_by_tag = {profile.tag: profile for profile in profiles}
        assert "code" in profile_by_tag
        assert 0 < profile_by_tag["code"].success_rate < 100
        observer_memory = engine.memory.read_agent_memory(observer.agent_id)
        assert "Observed actor complete" in observer_memory.active_context


class TestSlockAgentRegistryCrossTeam:
    def test_same_agent_id_can_join_multiple_groups_without_losing_original_team(self, tmp_path):
        """Agent identity follows the agent across teams while membership includes both groups."""
        registry = AgentRegistry(base_path=str(tmp_path))
        first = AgentIdentity(
            agent_id="codex:o3-pro:Coder",
            name="Coder",
            agent_type="codex",
            model_name="o3-pro",
            owner_group="chat_alpha",
        )
        second = AgentIdentity(
            agent_id="codex:o3-pro:Coder",
            name="Coder",
            agent_type="codex",
            model_name="o3-pro",
            owner_group="chat_beta",
        )

        registry.register(first)
        merged = registry.register(second)

        assert merged.owner_group == "chat_alpha"
        assert set(merged.member_groups) == {"chat_alpha", "chat_beta"}
        assert [agent.agent_id for agent in registry.list_agents("chat_alpha")] == ["codex:o3-pro:Coder"]
        assert [agent.agent_id for agent in registry.list_agents("chat_beta")] == ["codex:o3-pro:Coder"]


class TestSlockTaskRouterEvolution:
    def test_equal_scores_use_round_robin(self):
        """When skill scores tie, automatic assignment rotates between candidates."""
        router = TaskRouter()
        agents = [
            AgentIdentity(agent_id="agent-a", name="AgentA", owner_group="chat"),
            AgentIdentity(agent_id="agent-b", name="AgentB", owner_group="chat"),
        ]
        for agent in agents:
            router.set_skill_profiles(agent.agent_id, [SkillProfile(tag="code", success_rate=90, total_tasks=2)])

        first = router.route_message("implement the login flow", agents)
        second = router.route_message("implement the logout flow", agents)

        assert first is agents[0]
        assert second is agents[1]


class TestSlockEngineStateMachine:
    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock"
        return SlockEngine(
            chat_id="chat1",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_initial_status_is_idle(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.get_agent_status("a1") == AgentStatus.IDLE

    def test_valid_transition_idle_to_waking(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.transition_agent("a1", AgentStatus.WAKING) is True
        assert engine.get_agent_status("a1") == AgentStatus.WAKING

    def test_full_lifecycle_transition(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        agent_id = "a1"
        assert engine.transition_agent(agent_id, AgentStatus.WAKING) is True
        assert engine.transition_agent(agent_id, AgentStatus.THINKING) is True
        assert engine.transition_agent(agent_id, AgentStatus.RUNNING) is True
        assert engine.transition_agent(agent_id, AgentStatus.CHECKING) is True
        assert engine.transition_agent(agent_id, AgentStatus.SENDING) is True
        assert engine.transition_agent(agent_id, AgentStatus.IDLE) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.IDLE

    def test_invalid_transition_rejected(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        # IDLE → RUNNING (skipping WAKING) should fail
        assert engine.transition_agent("a1", AgentStatus.RUNNING) is False
        assert engine.get_agent_status("a1") == AgentStatus.IDLE

    def test_any_state_can_return_to_idle(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        engine.transition_agent("a1", AgentStatus.WAKING)
        engine.transition_agent("a1", AgentStatus.THINKING)
        # THINKING → IDLE (abort)
        assert engine.transition_agent("a1", AgentStatus.IDLE) is True

    def test_get_status_card(self, tmp_path):
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch1", team_name="Test")
        engine.activate_channel(ch)
        card = engine.get_status_card(team_name="Test")
        assert card["schema"] == "2.0"
        assert "Test" in card["header"]["title"]["content"]


class TestSlockEngineCallbacks:
    def test_callbacks_dataclass(self):
        cb = SlockEngineCallbacks()
        assert cb.on_agent_wake is None
        assert cb.on_error is None

    def test_callbacks_with_values(self):
        fn = MagicMock()
        cb = SlockEngineCallbacks(on_error=fn)
        cb.on_error("test error")
        fn.assert_called_once_with("test error")


# ============================================================
# AC-11: Human interaction suppression in slock mode
# ============================================================


class TestHumanInteractionSuppression:
    """AC-11: slock mode passes auto_approve=True to suppress human interaction."""

    @patch("src.slock_engine.engine.create_engine_session")
    def test_acp_session_created_with_auto_approve(self, mock_create, tmp_path):
        """AC-11: _run_acp_session passes auto_approve=True to create_engine_session."""
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "done"
        mock_session.send_prompt.return_value = mock_result
        mock_create.return_value = mock_session

        engine = SlockEngine(
            chat_id="chat_ac11",
            root_path=str(tmp_path),
            engine_name="AC11Test",
        )
        agent = AgentIdentity(
            name="Tester",
            agent_type="coco",
            owner_group="chat_ac11",
        )
        result = engine._run_acp_session(agent, "test prompt")

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        # auto_approve=True is the key assertion for AC-11
        assert call_kwargs.kwargs.get("auto_approve") is True or (
            len(call_kwargs.args) > 0 and any(
                k == "auto_approve" and v is True
                for k, v in call_kwargs.kwargs.items()
            )
        )
        assert result == "done"

    @patch("src.slock_engine.engine.create_engine_session")
    def test_auto_approve_keyword_in_call(self, mock_create, tmp_path):
        """AC-11: Verify auto_approve=True is explicitly passed as keyword arg."""
        mock_create.return_value = None  # Session creation fails

        engine = SlockEngine(
            chat_id="chat_ac11b",
            root_path=str(tmp_path),
            engine_name="AC11Test",
        )
        agent = AgentIdentity(
            name="Bot",
            agent_type="claude",
            owner_group="chat_ac11b",
        )
        engine._run_acp_session(agent, "prompt")

        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["auto_approve"] is True


# ============================================================
# AC-09: Memory update after agent execution (integration)
# ============================================================


class TestMemoryUpdateAfterExecution:
    """AC-09: _execute_agent updates agent memory after successful execution."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        return SlockEngine(
            chat_id="chat_mem",
            root_path=str(tmp_path) if tmp_path else "/tmp/test_mem",
            memory_base_path=str(tmp_path) if tmp_path else "/tmp/test_mem",
        )

    def test_execute_agent_updates_memory_on_success(self, tmp_path):
        """AC-09: Successful _execute_agent writes context to agent memory."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_mem", team_name="MemTest")
        engine.activate_channel(ch)

        agent = AgentIdentity(
            name="MemBot",
            agent_type="coco",
            owner_group="ch_mem",
        )
        engine.registry.register(agent)

        # Mock _run_acp_session to return a result
        with patch.object(engine, "_run_acp_session", return_value="Task completed"):
            with patch.object(engine._memory, "update_agent_context") as mock_update:
                engine._execute_agent(agent, "fix the bug", None)
                mock_update.assert_called_once()
                call_args = mock_update.call_args
                assert call_args[0][0] == agent.agent_id
                assert "fix the bug" in call_args[0][1]

    def test_execute_agent_skips_memory_on_no_result(self, tmp_path):
        """AC-09: When _run_acp_session returns None, memory is NOT updated."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_mem2", team_name="MemTest2")
        engine.activate_channel(ch)

        agent = AgentIdentity(
            name="MemBot2",
            agent_type="coco",
            owner_group="ch_mem2",
        )
        engine.registry.register(agent)

        with patch.object(engine, "_run_acp_session", return_value=None):
            with patch.object(engine._memory, "update_agent_context") as mock_update:
                engine._execute_agent(agent, "do something", None)
                mock_update.assert_not_called()


# ============================================================
# Task 9: Engine deactivate lifecycle tests
# ============================================================


class TestSlockEngineDeactivate:
    """Test deactivate() method, is_active property, and state reset behavior."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_deactivate"
        return SlockEngine(
            chat_id="chat_deact",
            root_path=str(tmp_path) if tmp_path else "/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_deactivate_sets_not_active(self, tmp_path):
        """deactivate() causes is_active to return False."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_deact", team_name="Deact")
        engine.activate_channel(ch)
        assert engine.is_active is True

        engine.deactivate()
        assert engine.is_active is False

    def test_deactivate_clears_channel(self, tmp_path):
        """deactivate() sets channel to None."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_deact2", team_name="Deact2")
        engine.activate_channel(ch)
        assert engine.channel is not None

        engine.deactivate()
        assert engine.channel is None

    def test_deactivate_resets_agents_to_idle(self, tmp_path):
        """deactivate() resets all agent statuses to IDLE."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_deact3", team_name="Deact3")
        engine.activate_channel(ch)

        # Transition agents to non-IDLE states
        engine.transition_agent("a1", AgentStatus.WAKING)
        engine.transition_agent("a1", AgentStatus.THINKING)
        engine.transition_agent("a2", AgentStatus.WAKING)

        assert engine.get_agent_status("a1") == AgentStatus.THINKING
        assert engine.get_agent_status("a2") == AgentStatus.WAKING

        engine.deactivate()

        assert engine.get_agent_status("a1") == AgentStatus.IDLE
        assert engine.get_agent_status("a2") == AgentStatus.IDLE

    def test_deactivate_cancels_session(self, tmp_path):
        """deactivate() calls session.cancel() if session exists."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_deact4", team_name="Deact4")
        engine.activate_channel(ch)

        mock_session = MagicMock()
        engine._session = mock_session

        engine.deactivate()
        mock_session.cancel.assert_called_once()

    def test_deactivate_without_channel_is_safe(self, tmp_path):
        """deactivate() on engine without channel does not raise."""
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.channel is None
        engine.deactivate()  # Should not raise
        assert engine.is_active is False

    def test_is_active_false_when_no_channel(self, tmp_path):
        """is_active is False when no channel is bound."""
        engine = self._make_engine(tmp_path=tmp_path)
        assert engine.is_active is False

    def test_is_active_true_after_activation(self, tmp_path):
        """is_active is True after channel activation."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_active", team_name="Active")
        engine.activate_channel(ch)
        assert engine.is_active is True


# ============================================================
# Task 11: _create_callbacks verification tests
# ============================================================


class TestSlockHandlerCallbacks:
    """Verify _create_callbacks produces working SlockEngineCallbacks."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def test_create_callbacks_returns_callbacks_instance(self):
        """_create_callbacks returns a SlockEngineCallbacks with all hooks set."""
        from src.slock_engine.engine import SlockEngineCallbacks
        handler = self._make_handler()
        cb = handler._create_callbacks("msg1", "chat1", None, "test_engine", "/tmp")
        assert isinstance(cb, SlockEngineCallbacks)
        assert cb.on_agent_wake is not None
        assert cb.on_agent_running is not None
        assert cb.on_agent_done is not None
        assert cb.on_error is not None

    def test_callbacks_on_agent_wake_callable(self):
        """on_agent_wake callback is callable without error."""
        handler = self._make_handler()
        cb = handler._create_callbacks("msg1", "chat1", None, "eng", "/tmp")
        agent = MagicMock()
        agent.name = "TestAgent"
        cb.on_agent_wake(agent)  # Should not raise

    def test_callbacks_on_agent_done_callable(self):
        """on_agent_done callback is callable without error."""
        handler = self._make_handler()
        cb = handler._create_callbacks("msg1", "chat1", None, "eng", "/tmp")
        agent = MagicMock()
        agent.name = "DoneAgent"
        cb.on_agent_done(agent, "result text")  # Should not raise

    def test_callbacks_on_error_callable(self):
        """on_error callback is callable without error."""
        handler = self._make_handler()
        cb = handler._create_callbacks("msg1", "chat1", None, "eng", "/tmp")
        cb.on_error("something went wrong")  # Should not raise


# ============================================================
# Thread safety: deactivate/pause snapshot-under-lock
# ============================================================


class TestSlockEngineThreadSafety:
    """Verify deactivate() and pause() use snapshot-under-lock for session."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_ts"
        return SlockEngine(
            chat_id="chat_ts",
            root_path=str(tmp_path) if tmp_path else "/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_concurrent_deactivate_no_exception(self, tmp_path):
        """Two threads calling deactivate() concurrently must not raise."""
        import threading

        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_ts", team_name="TS")
        engine.activate_channel(ch)

        mock_session = MagicMock()
        engine._session = mock_session

        errors = []

        def deactivate_worker():
            try:
                engine.deactivate()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=deactivate_worker)
        t2 = threading.Thread(target=deactivate_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert errors == [], f"Concurrent deactivate raised: {errors}"

    def test_deactivate_with_none_session(self, tmp_path):
        """deactivate() with self._session = None must not raise."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_ts2", team_name="TS2")
        engine.activate_channel(ch)
        engine._session = None
        engine.deactivate()  # Should not raise

    def test_pause_with_none_session(self, tmp_path):
        """pause() with self._session = None must not raise."""
        engine = self._make_engine(tmp_path=tmp_path)
        engine._session = None
        engine.pause()  # Should not raise

    def test_pause_cancels_session_via_snapshot(self, tmp_path):
        """pause() calls cancel on the session snapshot, not self._session."""
        engine = self._make_engine(tmp_path=tmp_path)
        mock_session = MagicMock()
        engine._session = mock_session
        engine.pause()
        mock_session.cancel.assert_called_once()


# ============================================================
# Timeout config: _run_acp_session uses settings.coco_execution_timeout
# ============================================================


class TestSlockEngineTimeoutConfig:
    """Verify _run_acp_session reads timeout from self.settings."""

    @patch("src.slock_engine.engine.create_engine_session")
    def test_timeout_from_settings(self, mock_create, tmp_path):
        """send_prompt is called with self.settings.coco_execution_timeout."""
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "ok"
        mock_session.send_prompt.return_value = mock_result
        mock_create.return_value = mock_session

        engine = SlockEngine(
            chat_id="chat_to",
            root_path=str(tmp_path),
            engine_name="TOTest",
        )
        # Inject a known timeout value
        engine.settings = MagicMock()
        engine.settings.coco_execution_timeout = 600

        agent = AgentIdentity(
            name="TimeoutBot",
            agent_type="coco",
            owner_group="chat_to",
        )
        engine._run_acp_session(agent, "test")

        mock_session.send_prompt.assert_called_once_with("test", timeout=600)


# ============================================================
# Status panel card: Stop button presence
# ============================================================


class TestStatusPanelStopButton:
    """Verify build_status_panel_card includes a Stop button with slock_stop action."""

    def _collect_buttons(self, node):
        if isinstance(node, dict):
            buttons = [node] if node.get("tag") == "button" else []
            for value in node.values():
                buttons.extend(self._collect_buttons(value))
            return buttons
        if isinstance(node, list):
            buttons = []
            for item in node:
                buttons.extend(self._collect_buttons(item))
            return buttons
        return []

    def test_stop_button_in_card(self):
        """Status panel card must contain a button with action 'slock_stop'."""
        from src.slock_engine.card_templates import build_status_panel_card

        card = build_status_panel_card(
            agents=[],
            team_name="TestTeam",
            channel_id="ch_btn",
        )

        actions = self._collect_buttons(card)
        stop_buttons = [
            btn for btn in actions
            if btn.get("value", {}).get("action") == "slock_stop"
        ]
        assert len(stop_buttons) == 1
        assert stop_buttons[0]["type"] == "danger"
        assert stop_buttons[0]["value"]["channel_id"] == "ch_btn"

    def test_refresh_button_still_present(self):
        """Refresh button must still exist alongside Stop."""
        from src.slock_engine.card_templates import build_status_panel_card

        card = build_status_panel_card(
            agents=[],
            team_name="TestTeam",
            channel_id="ch_btn2",
        )

        actions = self._collect_buttons(card)
        refresh_buttons = [
            btn for btn in actions
            if btn.get("value", {}).get("action") == "slock_refresh_status"
        ]
        assert len(refresh_buttons) == 1
