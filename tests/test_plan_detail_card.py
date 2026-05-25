"""Tests for build_task_overview_card (plan detail card rendering)."""

from __future__ import annotations

import json
import time

from src.slock_engine.card_templates.progress import build_task_overview_card
from src.slock_engine.models import (
    AgentIdentity,
    AgentStatus,
    CollaborationPlan,
    CollaborationPlanStatus,
    PlanStep,
    PlanStepStatus,
)


def _make_plan(num_steps: int = 3, status: CollaborationPlanStatus = CollaborationPlanStatus.EXECUTING) -> CollaborationPlan:
    """Create a mock CollaborationPlan with the given number of steps."""
    steps = []
    for i in range(num_steps):
        step_status = PlanStepStatus.DONE if i == 0 else PlanStepStatus.IN_PROGRESS if i == 1 else PlanStepStatus.TODO
        steps.append(PlanStep(
            step_id=f"step-{i}",
            role="coder" if i % 2 == 0 else "reviewer",
            agent_id=f"agent-{i}",
            description=f"Step {i}: do something useful",
            order=i,
            status=step_status,
        ))
    return CollaborationPlan(
        plan_id="plan-test-001",
        task_id="task-001",
        task_content="Implement the feature and write tests",
        steps=steps,
        status=status,
        created_at=time.time(),
        chain_template="coder-reviewer",
    )


def _make_agents(num_agents: int = 2) -> list[tuple[AgentIdentity, str]]:
    """Create a list of (AgentIdentity, status_str) tuples."""
    agents = []
    for i in range(num_agents):
        agent = AgentIdentity(
            agent_id=f"agent-{i}",
            name=f"agent_{i}",
            emoji="💻" if i % 2 == 0 else "🔍",
            role="coder" if i % 2 == 0 else "reviewer",
        )
        status_str = AgentStatus.RUNNING.value if i == 0 else AgentStatus.IDLE.value
        agents.append((agent, status_str))
    return agents


class TestBuildTaskOverviewCard:
    """Tests for build_task_overview_card."""

    def test_basic_card_structure(self):
        """Card should have header and body keys."""
        plan = _make_plan()
        agents = _make_agents()

        result = build_task_overview_card(plan, agents, channel_id="ch-001")

        assert isinstance(result, dict)
        assert "header" in result
        assert "body" in result

    def test_card_json_size_within_limit(self):
        """Serialized card JSON must be < 30720 bytes."""
        plan = _make_plan(num_steps=5)
        agents = _make_agents(num_agents=4)

        result = build_task_overview_card(
            plan,
            agents,
            channel_id="ch-001",
            latest_output_summary="This is the latest output from the agent.",
            discussion_entries=[
                {"speaker": "coder", "content": "I think we should refactor this module.", "timestamp": time.time()},
                {"speaker": "reviewer", "content": "Agreed, let me check the tests.", "timestamp": time.time()},
            ],
            timeline_events=[
                {"event_type": "claimed", "agent_id": "agent-0", "timestamp": time.time(), "detail": "Agent claimed the task"},
                {"event_type": "started", "agent_id": "agent-0", "timestamp": time.time(), "detail": "Execution started"},
                {"event_type": "completed", "agent_id": "agent-0", "timestamp": time.time(), "detail": "Step completed"},
            ],
        )

        card_json = json.dumps(result, ensure_ascii=False)
        assert len(card_json.encode("utf-8")) < 30720

    def test_with_discussion_entries(self):
        """Card should include discussion entries when provided."""
        plan = _make_plan()
        agents = _make_agents()
        discussion_entries = [
            {"speaker": "coder", "content": "Working on implementation", "timestamp": time.time()},
            {"speaker": "reviewer", "content": "Please add error handling", "timestamp": time.time()},
        ]

        result = build_task_overview_card(
            plan,
            agents,
            channel_id="ch-001",
            discussion_entries=discussion_entries,
        )

        assert isinstance(result, dict)
        card_str = json.dumps(result, ensure_ascii=False)
        # Discussion entries should appear in the rendered card
        assert "讨论" in card_str or "discussion" in card_str.lower()

    def test_with_timeline_events(self):
        """Card should include timeline events when provided."""
        plan = _make_plan()
        agents = _make_agents()
        timeline_events = [
            {"event_type": "claimed", "agent_id": "agent-0", "timestamp": time.time(), "detail": "Task claimed by coder"},
            {"event_type": "completed", "agent_id": "agent-0", "timestamp": time.time(), "detail": "Step 1 done"},
        ]

        result = build_task_overview_card(
            plan,
            agents,
            channel_id="ch-001",
            timeline_events=timeline_events,
        )

        assert isinstance(result, dict)
        card_str = json.dumps(result, ensure_ascii=False)
        # Timeline section should appear
        assert "时间线" in card_str or "timeline" in card_str.lower()

    def test_empty_agents_list(self):
        """Card should render correctly even with no agents."""
        plan = _make_plan()

        result = build_task_overview_card(plan, [], channel_id="ch-001")

        assert isinstance(result, dict)
        assert "header" in result
        assert "body" in result

    def test_completed_plan(self):
        """Card should render for a completed plan."""
        plan = _make_plan(num_steps=2, status=CollaborationPlanStatus.COMPLETED)
        # Mark all steps as done
        for step in plan.steps:
            step.status = PlanStepStatus.DONE
        agents = _make_agents()

        result = build_task_overview_card(plan, agents, channel_id="ch-001")

        assert isinstance(result, dict)
        card_str = json.dumps(result, ensure_ascii=False)
        # Should show completed status
        assert result["header"] is not None

    def test_no_optional_params(self):
        """Card should render with only required params (plan and agents)."""
        plan = _make_plan()
        agents = _make_agents()

        result = build_task_overview_card(plan, agents)

        assert isinstance(result, dict)
        assert "header" in result
        assert "body" in result
        # Should still be valid JSON under size limit
        card_json = json.dumps(result, ensure_ascii=False)
        assert len(card_json.encode("utf-8")) < 30720
