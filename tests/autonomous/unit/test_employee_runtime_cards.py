from __future__ import annotations

import json

from src.autonomous.manager.cards import (
    EmployeeRuntimeCardView,
    build_employee_roster_card,
    build_employee_runtime_status_card,
)
from src.autonomous.team.renderer import (
    EmployeeTeamRenderer,
    TeamAssignmentCardView,
    TeamRunCardView,
)


def _view(**overrides) -> EmployeeRuntimeCardView:
    values = {
        "agent_id": "agt_atlas",
        "name": "Atlas",
        "emoji": "🧭",
        "role": "reviewer",
        "tool": "codex",
        "model": "gpt-test",
        "employee_state": "active",
        "bot_state": "ready",
        "bot_generation": 3,
        "actor_state": "ready_cold",
        "mailbox_depth": 0,
        "can_accept": True,
        "identity_version": 12,
        "knowledge_generation": 7,
        "active_assignment_id": "asg_1",
        "active_run_id": "run_1",
        "last_checkpoint": "cp_14",
        "context_quality": "canonical_partial",
        "context_warnings": ("thread root unavailable", "attachment unavailable"),
        "review_item_ids": ("kng_review",),
    }
    values.update(overrides)
    return EmployeeRuntimeCardView(**values)


def test_runtime_card_separates_channel_actor_admission_and_context() -> None:
    card = build_employee_runtime_status_card(_view(), admin=True)
    payload = json.dumps(card, ensure_ascii=False)
    assert card["schema"] == "2.0"
    assert "Bot READY · generation 3" in payload
    assert "Agent READY_COLD · session cold" in payload
    assert "可接任务 · mailbox 0" in payload
    assert "上下文部分可用" in payload
    assert "asg_1" in payload and "run_1" in payload and "cp_14" in payload
    assert "identity v12" in payload and "knowledge generation 7" in payload
    assert "employee_runtime_recycle_session" in payload
    assert "employee_runtime_rebuild_workspace" in payload
    assert "employee_runtime_lint_knowledge" in payload
    assert "employee_runtime_retry_review" in payload


def test_runtime_card_omits_empty_assignment_warning_and_admin_blocks() -> None:
    card = build_employee_runtime_status_card(
        _view(
            active_assignment_id="",
            active_run_id="",
            last_checkpoint="",
            context_quality="complete",
            context_warnings=(),
        ),
        admin=False,
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "当前 Assignment" not in payload
    assert "降级说明" not in payload
    assert "管理员恢复动作" not in payload
    assert "employee_runtime_" not in payload


def test_action_required_card_names_code_and_preserves_completed_work() -> None:
    card = build_employee_runtime_status_card(
        _view(
            can_accept=False,
            actor_state="degraded",
            error_code="context_unavailable",
            completed_contributions=("Atlas: 已完成接口审计", "Nova: 已补齐回归测试"),
            recovery_hint="重建 Workspace 后重试该 assignment",
        )
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "context_unavailable" in payload
    assert "run_1" in payload and "asg_1" in payload
    assert "已完成接口审计" in payload and "已补齐回归测试" in payload
    assert "重建 Workspace 后重试该 assignment" in payload


def test_roster_shows_bot_and_actor_states_without_empty_details() -> None:
    card = build_employee_roster_card((_view(active_assignment_id=""),), archived_count=2)
    payload = json.dumps(card, ensure_ascii=False)
    assert "Bot READY / Agent READY_COLD" in payload
    assert "可接任务" in payload
    assert "历史归档 2 人" in payload
    assert "当前 Assignment" not in payload


def test_team_renderer_keeps_one_assignment_per_card_and_continues_long_output() -> None:
    renderer = EmployeeTeamRenderer(max_content_chars=80)
    bundle = renderer.render(
        TeamRunCardView(
            run_id="run_long",
            phase="reviewing",
            goal="完成跨员工评审",
            assignments=(
                TeamAssignmentCardView("asg_a", "agt_atlas", "Atlas", "completed", "A" * 170),
                TeamAssignmentCardView("asg_b", "agt_nova", "Nova", "running", "B" * 20),
            ),
        )
    )
    assert len(bundle.assignment_cards) == 2
    assert all("assignment_id" in card for card in bundle.assignment_cards)
    assert bundle.assignment_cards[0]["assignment_id"] == "asg_a"
    assert len(bundle.continuation_cards) == 2
    assert all(card["assignment_id"] == "asg_a" for card in bundle.continuation_cards)
