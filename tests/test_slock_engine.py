"""Unit tests for slock_engine/engine.py and slock_engine/slash_commands.py."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
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
from src.slock_engine.engine import SlockEngine, SlockEngineCallbacks, SlockStreamProcessor  # noqa: E402
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
        engine.root_path = str(tmp_path / "project_root")
        ch = SlockChannel(channel_id="ch_ws", name="ws-group", team_name="WS Team")
        engine.activate_channel(ch)

        workspace_dir = engine.memory.get_group_base_path("ch_ws")
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
    def test_default_storage_base_is_user_config_slock(self, mock_create_session, tmp_path):
        """Default Slock storage follows the existing global ~/.ghostap/slock contract."""
        mock_create_session.return_value = None
        root_path = str(tmp_path / "project_root")
        engine = SlockEngine(chat_id="chat_storage", root_path=root_path)

        expected = os.path.expanduser("~/.ghostap/slock")
        assert engine.memory.base_path == expected
        assert engine.registry.base_path == expected

    @patch("src.slock_engine.engine.create_engine_session")
    def test_activate_channel_writes_marker_to_config_dir_only(self, mock_create_session, tmp_path):
        """Slock channel markers are written under the app config dir, not the repo."""
        mock_create_session.return_value = None
        root_path = str(tmp_path / "project_root")
        config_path = str(tmp_path / "app_config" / "slock")
        engine = SlockEngine(chat_id="chat_canonical", root_path=root_path, memory_base_path=config_path)
        ch = SlockChannel(channel_id="ch_canonical", name="canonical", team_name="Canon")

        engine.activate_channel(ch)

        marker_path = os.path.join(
            config_path,
            "groups",
            "ch_canonical",
            ".slock_channel.json",
        )
        assert os.path.isfile(marker_path)
        assert not os.path.exists(os.path.join(root_path, "slock", "ch_canonical"))

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
        """Tasks are persisted to the group task board under the configured Slock store."""
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

        # Flush the async observer queue so writes land on disk before assertions
        engine._observer_queue.flush()

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


class TestSlockRuntimeArtifactsIgnored:
    def test_repo_ignores_slock_runtime_group_state(self):
        """Machine-local Slock group state must not be tracked by git."""
        repo_root = Path(__file__).resolve().parents[1]
        gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

        assert "slock/" in gitignore


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


# ============================================================
# Parallel Execution (Task 1: ThreadPoolExecutor dispatch)
# ============================================================


class TestSlockEngineParallelExecution:
    """Tests for execute_parallel and dispatch_pending_tasks."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock_parallel"
        return SlockEngine(
            chat_id="chat_parallel",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_execute_parallel_runs_multiple_tasks(self, tmp_path):
        """execute_parallel dispatches tasks to different agents concurrently."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_par", name="Parallel", team_name="ParTeam")
        engine.activate_channel(ch)

        agent_a = AgentIdentity(agent_id="a1", name="AgentA", agent_type="coco", owner_group="ch_par")
        agent_b = AgentIdentity(agent_id="a2", name="AgentB", agent_type="coco", owner_group="ch_par")
        engine.registry.register(agent_a)
        engine.registry.register(agent_b)

        t1 = engine.add_task("Task one")
        t2 = engine.add_task("Task two")

        with patch.object(engine, "_run_acp_session", side_effect=["result one", "result two"]):
            results = engine.execute_parallel(
                [(t1.task_id, "a1"), (t2.task_id, "a2")],
                timeout=10.0,
            )

        assert results[t1.task_id] is not None
        assert results[t2.task_id] is not None
        # Both tasks should be marked DONE
        statuses = {t.task_id: t.status for t in engine.tasks}
        assert statuses[t1.task_id] == TaskStatus.DONE
        assert statuses[t2.task_id] == TaskStatus.DONE

    def test_execute_parallel_handles_failure(self, tmp_path):
        """execute_parallel returns None for tasks that fail."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_fail", name="Fail", team_name="FailTeam")
        engine.activate_channel(ch)

        agent = AgentIdentity(agent_id="a1", name="AgentA", agent_type="coco", owner_group="ch_fail")
        engine.registry.register(agent)

        t1 = engine.add_task("Will fail")

        with patch.object(engine, "_run_acp_session", return_value=None):
            results = engine.execute_parallel(
                [(t1.task_id, "a1")],
                timeout=10.0,
            )

        assert results[t1.task_id] is None
        # Task should be rolled back to TODO on failure
        assert engine.tasks[0].status == TaskStatus.TODO

    def test_execute_parallel_when_stopping(self, tmp_path):
        """execute_parallel returns None for all tasks when engine is stopping."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_stop", name="Stop", team_name="StopTeam")
        engine.activate_channel(ch)

        t1 = engine.add_task("Whatever")
        engine.pause()  # sets state to STOPPING

        results = engine.execute_parallel([(t1.task_id, "a1")], timeout=5.0)
        assert results[t1.task_id] is None

    def test_dispatch_pending_tasks_assigns_and_executes(self, tmp_path):
        """dispatch_pending_tasks auto-routes and executes TODO tasks."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_dispatch", name="Dispatch", team_name="DispatchTeam")
        engine.activate_channel(ch)

        agent1 = AgentIdentity(agent_id="a1", name="Coder", agent_type="coco", owner_group="ch_dispatch")
        agent2 = AgentIdentity(agent_id="a2", name="Tester", agent_type="coco", owner_group="ch_dispatch")
        engine.registry.register(agent1)
        engine.registry.register(agent2)

        engine.add_task("Implement login")
        engine.add_task("Write tests")

        with patch.object(engine, "_run_acp_session", return_value="done"):
            results = engine.dispatch_pending_tasks()

        assert len(results) == 2
        assert all(v is not None for v in results.values())
        assert all(t.status == TaskStatus.DONE for t in engine.tasks)

    def test_dispatch_pending_tasks_no_agents(self, tmp_path):
        """dispatch_pending_tasks returns empty when no agents registered."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_empty", name="Empty", team_name="EmptyTeam")
        engine.activate_channel(ch)
        engine.add_task("No one to run this")

        results = engine.dispatch_pending_tasks()
        assert results == {}

    def test_dispatch_pending_tasks_respects_max_concurrent(self, tmp_path):
        """dispatch_pending_tasks limits concurrent tasks."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_limit", name="Limit", team_name="LimitTeam")
        engine.activate_channel(ch)

        # Register 3 agents so max_concurrent=2 is the binding constraint
        for i in range(3):
            agent = AgentIdentity(agent_id=f"a{i}", name=f"Worker{i}", agent_type="coco", owner_group="ch_limit")
            engine.registry.register(agent)

        for i in range(5):
            engine.add_task(f"Task {i}")

        with patch.object(engine, "_run_acp_session", return_value="done"):
            results = engine.dispatch_pending_tasks(max_concurrent=2)

        # Only 2 should have been dispatched (limited by max_concurrent, not agent count)
        assert len(results) == 2

    def test_cleanup_shuts_down_executor(self, tmp_path):
        """cleanup() gracefully shuts down the thread pool."""
        engine = self._make_engine(tmp_path=tmp_path)
        # Force executor creation
        executor = engine._get_executor()
        assert executor is not None
        engine.cleanup()
        assert engine._executor is None


# ============================================================
# TaskClaim Persistence (Task 9-11)
# ============================================================


class TestTaskClaimPersistence:
    """Tests for TaskClaim file persistence and TTL expiry."""

    def test_claim_persists_to_disk(self, tmp_path):
        """Claims are written to disk on mutation."""
        from src.slock_engine.task_router import TaskClaim
        import json

        path = str(tmp_path / "claims.json")
        tc = TaskClaim(persist_path=path)
        tc.claim("t1", "agent-1")

        assert os.path.isfile(path)
        with open(path, "r") as f:
            data = json.load(f)
        assert "t1" in data
        assert data["t1"]["agent_id"] == "agent-1"

    def test_claim_loads_from_disk(self, tmp_path):
        """Claims are restored from disk on construction."""
        from src.slock_engine.task_router import TaskClaim
        import json

        path = str(tmp_path / "claims.json")
        # Write a claim manually
        data = {"t1": {"agent_id": "agent-x", "claimed_at": time.time()}}
        with open(path, "w") as f:
            json.dump(data, f)

        tc = TaskClaim(persist_path=path)
        assert tc.get_holder("t1") == "agent-x"

    def test_expired_claims_not_loaded(self, tmp_path):
        """Expired claims are pruned on load."""
        from src.slock_engine.task_router import TaskClaim
        import json

        path = str(tmp_path / "claims.json")
        # Write an expired claim (claimed 2 hours ago, TTL is 1 hour)
        data = {"t1": {"agent_id": "agent-x", "claimed_at": time.time() - 7200}}
        with open(path, "w") as f:
            json.dump(data, f)

        tc = TaskClaim(default_ttl=3600.0, persist_path=path)
        assert tc.get_holder("t1") is None

    def test_release_removes_from_disk(self, tmp_path):
        """Release removes claim from persisted file."""
        from src.slock_engine.task_router import TaskClaim
        import json

        path = str(tmp_path / "claims.json")
        tc = TaskClaim(persist_path=path)
        tc.claim("t1", "agent-1")
        tc.release("t1", "agent-1")

        with open(path, "r") as f:
            data = json.load(f)
        assert "t1" not in data

    def test_purge_expired_removes_stale_claims(self, tmp_path):
        """purge_expired removes all claims past TTL."""
        from src.slock_engine.task_router import TaskClaim

        path = str(tmp_path / "claims.json")
        tc = TaskClaim(default_ttl=1.0, persist_path=path)
        tc.claim("t1", "agent-1")
        tc.claim("t2", "agent-2")

        # Manually backdate claims
        with tc._lock:
            tc._claims["t1"] = ("agent-1", time.time() - 5.0)
            tc._claims["t2"] = ("agent-2", time.time())

        purged = tc.purge_expired()
        assert purged == 1
        assert tc.get_holder("t1") is None
        assert tc.get_holder("t2") == "agent-2"

    def test_no_persist_path_works_in_memory_only(self):
        """TaskClaim without persist_path works purely in-memory (backward compat)."""
        from src.slock_engine.task_router import TaskClaim

        tc = TaskClaim()
        tc.claim("t1", "agent-1")
        assert tc.get_holder("t1") == "agent-1"
        tc.release("t1")
        assert tc.get_holder("t1") is None


# ============================================================
# Escalation Protocol (Task 12-16)
# ============================================================


class TestEscalationProtocol:
    """Tests for the escalation protocol."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock_esc"
        return SlockEngine(
            chat_id="chat_esc",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_escalate_creates_request(self, tmp_path):
        """escalate() creates an EscalationRequest and stores it."""
        from src.slock_engine.models import EscalationLevel, EscalationRequest

        engine = self._make_engine(tmp_path=tmp_path)
        agent = AgentIdentity(agent_id="a1", name="Coder", agent_type="coco")

        esc = engine.escalate(agent, "Cannot connect to database", level=EscalationLevel.BLOCKED)

        assert esc.agent_id == "a1"
        assert esc.agent_name == "Coder"
        assert esc.reason == "Cannot connect to database"
        assert esc.level == EscalationLevel.BLOCKED
        assert not esc.resolved
        assert len(engine.get_pending_escalations()) == 1

    def test_escalate_pauses_agent(self, tmp_path):
        """escalate() transitions agent back to IDLE."""
        engine = self._make_engine(tmp_path=tmp_path)
        agent = AgentIdentity(agent_id="a1", name="Coder", agent_type="coco")

        # Move agent to RUNNING state
        engine.transition_agent("a1", AgentStatus.WAKING)
        engine.transition_agent("a1", AgentStatus.THINKING)
        engine.transition_agent("a1", AgentStatus.RUNNING)

        engine.escalate(agent, "Stuck")

        assert engine.get_agent_status("a1") == AgentStatus.IDLE

    def test_resolve_escalation(self, tmp_path):
        """resolve_escalation() marks the request as resolved."""
        engine = self._make_engine(tmp_path=tmp_path)
        agent = AgentIdentity(agent_id="a1", name="Coder", agent_type="coco")

        esc = engine.escalate(agent, "Need help")
        resolved = engine.resolve_escalation(esc.escalation_id, "Retry")

        assert resolved is not None
        assert resolved.resolved is True
        assert resolved.resolution == "Retry"
        assert resolved.resolved_at is not None
        assert len(engine.get_pending_escalations()) == 0

    def test_resolve_nonexistent_escalation(self, tmp_path):
        """resolve_escalation() returns None for unknown ID."""
        engine = self._make_engine(tmp_path=tmp_path)
        result = engine.resolve_escalation("nonexistent", "Skip")
        assert result is None

    def test_escalation_card_structure(self, tmp_path):
        """get_escalation_card() produces valid card structure."""
        from src.slock_engine.models import EscalationLevel

        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_esc", name="Esc", team_name="EscTeam")
        engine.activate_channel(ch)

        agent = AgentIdentity(agent_id="a1", name="Coder", agent_type="coco")
        esc = engine.escalate(
            agent, "API rate limit exceeded",
            level=EscalationLevel.CRITICAL,
            context="Error: 429 Too Many Requests",
            options=["Retry", "Wait 5min", "Abort"],
        )

        card = engine.get_escalation_card(esc)
        assert card["schema"] == "2.0"
        assert "升级告警" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "red"  # CRITICAL level

    def test_escalation_with_task_reference(self, tmp_path):
        """Escalation can reference a specific task."""
        engine = self._make_engine(tmp_path=tmp_path)
        agent = AgentIdentity(agent_id="a1", name="Coder", agent_type="coco")
        task = engine.add_task("Deploy to prod")

        esc = engine.escalate(agent, "Deploy failed", task_id=task.task_id)
        assert esc.task_id == task.task_id


# ============================================================
# SlockStreamProcessor (Task 5-6)
# ============================================================


class TestSlockStreamProcessor:
    """Tests for the streaming progress card processor."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock_stream"
        return SlockEngine(
            chat_id="chat_stream",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_build_callbacks_returns_valid_callbacks(self, tmp_path):
        """build_callbacks() returns SlockEngineCallbacks with all hooks set."""
        engine = self._make_engine(tmp_path=tmp_path)
        processor = SlockStreamProcessor(engine)
        callbacks = processor.build_callbacks()

        assert callbacks.on_agent_wake is not None
        assert callbacks.on_agent_thinking is not None
        assert callbacks.on_agent_running is not None
        assert callbacks.on_agent_done is not None
        assert callbacks.on_agent_error is not None
        assert callbacks.on_error is not None

    def test_progress_card_initial_state(self, tmp_path):
        """Initial progress card shows 'Waiting for agents'."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_s", name="Stream", team_name="StreamTeam")
        engine.activate_channel(ch)

        processor = SlockStreamProcessor(engine)
        card = processor.get_progress_card()

        assert card["schema"] == "2.0"
        assert "StreamTeam" in card["header"]["title"]["content"]
        assert "Running" in card["header"]["title"]["content"]

    def test_progress_card_tracks_agent_activity(self, tmp_path):
        """Progress card updates as agents transition through states."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_s", name="Stream", team_name="StreamTeam")
        engine.activate_channel(ch)

        processor = SlockStreamProcessor(engine)
        callbacks = processor.build_callbacks()

        agent = AgentIdentity(agent_id="a1", name="Coder", emoji="🔧", agent_type="coco")
        callbacks.on_agent_wake(agent)

        card = processor.get_progress_card()
        body_text = str(card["body"]["elements"])
        assert "Coder" in body_text
        assert "waking" in body_text

    def test_progress_card_shows_percentage(self, tmp_path):
        """Progress card shows completion percentage when total_tasks is set."""
        engine = self._make_engine(tmp_path=tmp_path)
        ch = SlockChannel(channel_id="ch_s", name="Stream", team_name="StreamTeam")
        engine.activate_channel(ch)

        processor = SlockStreamProcessor(engine)
        processor.set_total_tasks(4)
        callbacks = processor.build_callbacks()

        agent = AgentIdentity(agent_id="a1", name="Coder", emoji="🔧", agent_type="coco")
        callbacks.on_agent_done(agent, "result")

        card = processor.get_progress_card()
        assert "25%" in card["header"]["title"]["content"]
        assert "1/4" in card["header"]["title"]["content"]

    def test_on_update_callback_fires(self, tmp_path):
        """on_update is called with card dict on each state change."""
        engine = self._make_engine(tmp_path=tmp_path)
        updates: list[dict] = []

        processor = SlockStreamProcessor(engine, on_update=lambda card: updates.append(card))
        callbacks = processor.build_callbacks()

        agent = AgentIdentity(agent_id="a1", name="Coder", emoji="🔧", agent_type="coco")
        callbacks.on_agent_wake(agent)
        callbacks.on_agent_thinking(agent)
        callbacks.on_agent_running(agent, "implement feature")

        assert len(updates) == 3
        assert all(u["schema"] == "2.0" for u in updates)

    def test_error_count_tracked(self, tmp_path):
        """Errors increment the error counter in the progress card."""
        engine = self._make_engine(tmp_path=tmp_path)
        processor = SlockStreamProcessor(engine)
        callbacks = processor.build_callbacks()

        agent = AgentIdentity(agent_id="a1", name="Coder", emoji="🔧", agent_type="coco")
        callbacks.on_agent_error(agent, "Connection timeout")

        card = processor.get_progress_card()
        note_text = str(card["body"]["elements"][-1])
        assert "❌" in note_text


# ============================================================
# AC-19: add_task() rejects new tasks when open task limit is reached
# ============================================================


class TestAddTaskOpenLimit:
    """AC-19: add_task() rejects new tasks when open task limit is reached."""

    def _make_engine(self, tmp_path):
        from src.slock_engine.engine import SlockEngine
        return SlockEngine(chat_id="test_chat", root_path=str(tmp_path), memory_base_path=str(tmp_path))

    @patch("src.slock_engine.task_board_manager.get_settings")
    @patch("src.slock_engine.engine.get_settings")
    def test_add_task_exceeds_max_open(self, mock_settings, mock_tbm_settings, tmp_path):
        """When open tasks >= slock_max_open_tasks, add_task returns None."""
        settings = MagicMock()
        settings.slock_max_parallel_agents = 4
        settings.slock_max_queue_size = 8
        settings.slock_max_open_tasks = 3
        mock_settings.return_value = settings
        mock_tbm_settings.return_value = settings

        engine = self._make_engine(tmp_path)

        # Add 3 tasks (at limit)
        t1 = engine.add_task("Task 1")
        t2 = engine.add_task("Task 2")
        t3 = engine.add_task("Task 3")
        assert t1 is not None
        assert t2 is not None
        assert t3 is not None

        # 4th should be rejected
        t4 = engine.add_task("Task 4")
        assert t4 is None
        assert len(engine.tasks) == 3

    @patch("src.slock_engine.task_board_manager.get_settings")
    @patch("src.slock_engine.engine.get_settings")
    def test_add_task_done_not_counted(self, mock_settings, mock_tbm_settings, tmp_path):
        """DONE tasks don't count toward the open limit."""
        settings = MagicMock()
        settings.slock_max_parallel_agents = 4
        settings.slock_max_queue_size = 8
        settings.slock_max_open_tasks = 2
        mock_settings.return_value = settings
        mock_tbm_settings.return_value = settings

        engine = self._make_engine(tmp_path)

        # Add 2 tasks and mark them DONE
        t1 = engine.add_task("Done Task 1")
        t2 = engine.add_task("Done Task 2")
        t1.status = TaskStatus.DONE
        t2.status = TaskStatus.DONE

        # Should still be able to add (open count = 0)
        t3 = engine.add_task("Active Task")
        assert t3 is not None
        assert t3.content == "Active Task"

    @patch("src.slock_engine.task_board_manager.get_settings")
    @patch("src.slock_engine.engine.get_settings")
    def test_add_task_mixed_statuses(self, mock_settings, mock_tbm_settings, tmp_path):
        """Only non-DONE tasks count toward the limit."""
        settings = MagicMock()
        settings.slock_max_parallel_agents = 4
        settings.slock_max_queue_size = 8
        settings.slock_max_open_tasks = 3
        mock_settings.return_value = settings
        mock_tbm_settings.return_value = settings

        engine = self._make_engine(tmp_path)

        t1 = engine.add_task("TODO task")
        t2 = engine.add_task("In progress task")
        t2.status = TaskStatus.IN_PROGRESS
        t3 = engine.add_task("Done task")
        t3.status = TaskStatus.DONE

        # Open count = 2 (t1 TODO + t2 IN_PROGRESS), limit is 3 → can add one more
        t4 = engine.add_task("Fourth task")
        assert t4 is not None

        # Now open count = 3, should reject
        t5 = engine.add_task("Fifth task")
        assert t5 is None


# ============================================================
# State Machine Refactoring — VALID_TRANSITIONS class constant
# ============================================================


class TestValidTransitionsClassConstant:
    """Verify VALID_TRANSITIONS is a proper class constant with immutable values."""

    def test_valid_transitions_exists_as_class_attribute(self):
        """AC-2: VALID_TRANSITIONS is a class-level attribute, not instance-level."""
        assert hasattr(SlockEngine, "VALID_TRANSITIONS")
        # Accessing via class (not instance) confirms it's a class constant
        transitions = SlockEngine.VALID_TRANSITIONS
        assert isinstance(transitions, dict)

    def test_valid_transitions_values_are_tuples(self):
        """AC-2: Values must be tuples (immutable) to prevent accidental mutation."""
        for status, targets in SlockEngine.VALID_TRANSITIONS.items():
            assert isinstance(targets, tuple), f"Value for {status} should be tuple, got {type(targets)}"

    def test_valid_transitions_covers_all_statuses(self):
        """All AgentStatus values should have an entry in VALID_TRANSITIONS."""
        for status in AgentStatus:
            assert status in SlockEngine.VALID_TRANSITIONS, f"Missing entry for {status}"

    def test_valid_transitions_content_correctness(self):
        """Validate the expected transition graph."""
        t = SlockEngine.VALID_TRANSITIONS
        assert AgentStatus.WAKING in t[AgentStatus.IDLE]
        assert AgentStatus.MOVING in t[AgentStatus.IDLE]
        assert AgentStatus.THINKING in t[AgentStatus.WAKING]
        assert AgentStatus.RUNNING in t[AgentStatus.THINKING]
        assert AgentStatus.CHECKING in t[AgentStatus.RUNNING]
        assert AgentStatus.SENDING in t[AgentStatus.CHECKING]
        assert AgentStatus.IDLE in t[AgentStatus.SENDING]
        assert AgentStatus.IDLE in t[AgentStatus.MOVING]


class TestTryLockForMoveUsesTransitionAgent:
    """AC-1: try_lock_for_move delegates to transition_agent (no direct _agent_statuses write)."""

    @patch("src.slock_engine.engine.create_engine_session")
    def _make_engine(self, mock_create_session, tmp_path=None):
        mock_create_session.return_value = None
        base_path = str(tmp_path) if tmp_path else "/tmp/test_slock"
        return SlockEngine(
            chat_id="chat1",
            root_path="/tmp/test_root",
            memory_base_path=base_path,
        )

    def test_try_lock_calls_transition_agent(self, tmp_path):
        """Verify try_lock_for_move invokes transition_agent internally."""
        engine = self._make_engine(tmp_path=tmp_path)
        with patch.object(engine, "transition_agent", wraps=engine.transition_agent) as mock_transition:
            result = engine.try_lock_for_move("agent_x")
            assert result is True
            mock_transition.assert_called_once_with("agent_x", AgentStatus.MOVING)

    def test_try_lock_fails_when_not_idle(self, tmp_path):
        """try_lock_for_move returns False if agent is already WAKING."""
        engine = self._make_engine(tmp_path=tmp_path)
        engine.transition_agent("agent_y", AgentStatus.WAKING)
        result = engine.try_lock_for_move("agent_y")
        assert result is False
        assert engine.get_agent_status("agent_y") == AgentStatus.WAKING

    def test_try_lock_atomicity_concurrent(self, tmp_path):
        """NFR-1: Multiple threads racing try_lock_for_move — exactly one wins."""
        engine = self._make_engine(tmp_path=tmp_path)
        results = []
        barrier = threading.Barrier(5)

        def attempt():
            barrier.wait(timeout=2)
            results.append(engine.try_lock_for_move("agent_race"))

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        assert results.count(True) == 1
        assert results.count(False) == 4
        assert engine.get_agent_status("agent_race") == AgentStatus.MOVING


# ---------------------------------------------------------------------------
# Security: build_resolved_escalation_card redacts sensitive data
# ---------------------------------------------------------------------------


class TestResolvedEscalationCardRedaction:
    """Verify build_resolved_escalation_card applies redact_sensitive() to reason and context."""

    def _make_escalation(self, reason: str = "", context: str = ""):
        from src.slock_engine.models import EscalationLevel, EscalationRequest

        return EscalationRequest(
            agent_id="agent_sec_test",
            agent_name="SecBot",
            level=EscalationLevel.BLOCKED,
            reason=reason,
            context=context,
        )

    def test_reason_with_api_key_is_redacted(self):
        """API key pattern in reason must be redacted in card output."""
        from src.slock_engine.card_templates import build_resolved_escalation_card

        esc = self._make_escalation(reason="Failed auth: API_KEY=sk-12345abcdef67890")
        card = build_resolved_escalation_card(esc, resolved_by="admin", resolution="Retry")
        card_json = json.dumps(card)

        assert "sk-12345abcdef67890" not in card_json
        assert "<redacted>" in card_json

    def test_context_with_password_is_redacted(self):
        """Password pattern in context must be redacted in card output."""
        from src.slock_engine.card_templates import build_resolved_escalation_card

        esc = self._make_escalation(
            reason="Connection failed",
            context="DB_PASSWORD=hunter2secret\nSECRET_KEY=abc123xyz",
        )
        card = build_resolved_escalation_card(esc, resolved_by="admin", resolution="Skip")
        card_json = json.dumps(card)

        assert "hunter2secret" not in card_json
        assert "abc123xyz" not in card_json
        assert "<redacted>" in card_json

    def test_empty_context_no_crash(self):
        """Empty or missing context should not cause errors."""
        from src.slock_engine.card_templates import build_resolved_escalation_card

        esc = self._make_escalation(reason="Simple reason", context="")
        card = build_resolved_escalation_card(esc, resolved_by="admin", resolution="Done")

        assert card is not None
        assert "header" in card

    def test_none_like_empty_reason_no_crash(self):
        """Empty reason should not crash redact_sensitive."""
        from src.slock_engine.card_templates import build_resolved_escalation_card

        esc = self._make_escalation(reason="", context="")
        card = build_resolved_escalation_card(esc, resolved_by="admin", resolution="Done")

        assert card is not None
