from random import Random
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.card.actions.dispatch import (
    SPEC_REVIEW_SELECT_TOOL,
    SPEC_REVIEW_USE_AUTO,
)
from src.card.events import CardEventType
from src.engine_base import ReviewPerspective
from src.feishu.handlers.spec import SpecHandler
from src.project.context import ProjectContext
from src.spec_engine.review import ReviewCircuitState
from src.spec_engine.review_agents import ReviewAgentBinding, assign_review_agents
from src.spec_engine.review_artifacts import ReviewArtifacts
from src.spec_engine.review_roles import ReviewRoleSpec
from src.spec_engine.review_strategy import AdaptiveRoleReviewStrategy, ReviewContext
from src.worktree_engine.models import WorktreeSelectionItem
from src.worktree_engine.selection import WorktreeToolOption


def _role(role_id: str) -> ReviewRoleSpec:
    return ReviewRoleSpec(
        role_id=role_id,
        display_name=role_id.title(),
        category="software",
        mission="review",
        review_focus=["correctness"],
        must_check=["diff"],
        evidence_policy="blockers require evidence",
        base_perspective=ReviewPerspective.ARCHITECT,
    )


def _item(tool: str, model: str | None = None) -> WorktreeSelectionItem:
    return WorktreeSelectionItem(
        provider="acp",
        tool_name=tool,
        display_name=tool.title(),
        model_name=model,
        model_display_name=model,
    )


def test_assign_review_agents_covers_selected_pool_when_roles_cover_pool():
    roles = [_role(f"role_{i}") for i in range(5)]
    agents = [
        ReviewAgentBinding.from_selection_item(_item("coco", "m1")),
        ReviewAgentBinding.from_selection_item(_item("codex", "gpt-5.2")),
        ReviewAgentBinding.from_selection_item(_item("aiden", "m3")),
    ]

    assigned = assign_review_agents(roles, agents, rng=Random(8))

    used_keys = {binding.selection_key for binding in assigned.values()}
    assert used_keys == {agent.selection_key for agent in agents}
    assert len(used_keys) > 1


def test_adaptive_review_uses_selected_agents_for_role_sessions(monkeypatch):
    roles = [_role("architect"), _role("tester"), _role("security")]
    agents = [
        ReviewAgentBinding.from_selection_item(_item("coco", "m1")),
        ReviewAgentBinding.from_selection_item(_item("codex", "gpt-5.2")),
    ]
    captured: list[tuple[str, str | None]] = []

    class FakeReviewSession:
        def __init__(self, agent_type: str, cwd: str, model_name: str | None = None):
            captured.append((agent_type, model_name))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def send_prompt(self, prompt: str, on_event=None, timeout: float = 240.0):
            return SimpleNamespace(
                text='{"role_id":"x","verdict":"PASS","summary":"ok","suggestions":[]}'
            )

    monkeypatch.setattr("src.spec_engine.review_strategy.EphemeralReviewSession", FakeReviewSession)

    settings = SimpleNamespace(
        spec_review_dynamic_roles_enabled=True,
        spec_review_dynamic_roles_max=3,
        spec_review_total_roles_max=8,
        spec_review_failure_circuit_enabled=False,
        spec_review_max_parallel=3,
        spec_review_timeout=30,
    )
    artifacts = ReviewArtifacts(cycle_number=1, requirement="build", cwd="/tmp")
    result = AdaptiveRoleReviewStrategy().run(
        ReviewContext(
            cycle=1,
            session=None,
            settings=settings,
            project=None,
            send_prompt_with_retry_fn=lambda *args, **kwargs: "",
            build_review_exception_diagnostics_fn=lambda *args, **kwargs: {},
            circuit=ReviewCircuitState(),
            artifacts=artifacts,
            role_plan_override=roles,
            review_agents=agents,
            review_agent_rng=Random(3),
        )
    )

    assert result.all_passed is True
    assert {agent for agent, _ in captured} == {"coco", "codex"}
    assert ("codex", "gpt-5.2") in captured
    assert ("coco", "m1") in captured


def _make_spec_handler() -> SpecHandler:
    ctx = MagicMock()
    ctx.settings.ref_note_enabled = False
    ctx.spec_engine_manager.get.return_value = None
    ctx.mode_manager.get_mode.return_value = None
    return SpecHandler(ctx)


def test_spec_start_shows_review_agent_selection_before_submitting_task():
    handler = _make_spec_handler()
    project = ProjectContext(project_id="p-spec", project_name="Spec", root_path="/tmp/spec")
    mock_session = MagicMock()
    fake_tools = [
        {
            "provider": "acp",
            "tool_name": "coco",
            "display_name": "Coco",
            "description": "ACP Coco",
            "supports_model": True,
        }
    ]

    with patch.object(handler, "_get_available_spec_review_tools", return_value=fake_tools), \
         patch.object(handler.renderer, "get_or_create_session", return_value=mock_session), \
         patch.object(handler, "_submit_engine_task") as submit_task:
        handler.start_spec_engine("msg-spec", "chat-spec", "implement auth", project)

    submit_task.assert_not_called()
    mock_session.dispatch.assert_called_once()
    event = mock_session.dispatch.call_args[0][0]
    assert event.type == CardEventType.WORKTREE_TOOL_SELECT
    assert event.payload["select_action"] == SPEC_REVIEW_SELECT_TOOL
    assert event.payload["auto_action"] == SPEC_REVIEW_USE_AUTO
    assert event.payload["mode_label"] == "Spec Review"
    assert project.spec_review_selection_state.selection.pending_goal == "implement auth"


def test_spec_review_auto_starts_with_empty_review_agent_pool():
    handler = _make_spec_handler()
    project = ProjectContext(project_id="p-auto", project_name="Spec", root_path="/tmp/spec-auto")
    handler.ctx.project_manager.get_project_for_chat.return_value = project
    handler._spec_review_selection_controller().start_selection(project, goal="ship it")

    with patch.object(handler, "_start_spec_engine_now") as start_now:
        handler.handle_spec_review_use_auto("msg-auto", "chat-auto", project_id="p-auto")

    start_now.assert_called_once_with(
        "msg-auto",
        "chat-auto",
        "ship it",
        project,
        review_agents=[],
    )


def test_spec_review_finish_starts_with_selected_review_agent_pool():
    handler = _make_spec_handler()
    project = ProjectContext(project_id="p-finish", project_name="Spec", root_path="/tmp/spec-finish")
    handler.ctx.project_manager.get_project_for_chat.return_value = project
    ctrl = handler._spec_review_selection_controller()
    ctrl.start_selection(project, goal="build review")
    ctrl.select_tool(
        project,
        WorktreeToolOption(
            provider="acp",
            tool_name="codex",
            display_name="Codex",
            supports_model=True,
            model_optional=True,
        ),
    )
    ctrl.add_pending_item(project, model_name="gpt-5.2", model_display_name="GPT 5.2")

    with patch.object(handler, "_start_spec_engine_now") as start_now:
        handler.handle_spec_review_finish_selection("msg-finish", "chat-finish", project_id="p-finish")

    start_now.assert_called_once()
    kwargs = start_now.call_args.kwargs
    agents = kwargs["review_agents"]
    assert len(agents) == 1
    assert agents[0].agent_type == "codex"
    assert agents[0].model_name == "gpt-5.2"
