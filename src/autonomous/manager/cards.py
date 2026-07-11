"""Canonical Feishu interactive cards for the autonomous Manager.

All cards follow the pattern: structured JSON templates rendered
with dynamic data. Reuses the project's shared tool/model discovery
from src/workflow_engine/tool_registry (same source as /wf, Deep, Spec).
"""

from __future__ import annotations

from typing import Any


def _discover_tools() -> list[str]:
    """Get available tools using the same discovery as Workflow/Deep/Spec."""
    try:
        from ...workflow_engine.tool_registry import get_available_tools
        tools = get_available_tools(require_available=True)
        if tools:
            return list(tools.keys())
    except Exception:
        pass
    try:
        from ...acp.providers import get_providers
        providers = get_providers()
        if providers:
            return list(providers.keys())
    except Exception:
        pass
    return ["coco", "claude", "codex", "aiden", "gemini", "traex"]


def _discover_models_for_tool(tool_name: str) -> list[str]:
    """Get available models for a tool using ACP provider model discovery."""
    try:
        from ...acp.providers import get_providers
        providers = get_providers()
        provider = providers.get(tool_name)
        if provider and hasattr(provider, "get_models"):
            models = provider.get_models()
            if models:
                return [m.name if hasattr(m, "name") else str(m) for m in models]
        if provider and hasattr(provider, "default_model"):
            dm = provider.default_model
            if dm:
                return [dm]
    except Exception:
        pass
    return []


def build_employee_creation_card(
    *,
    available_roles: list[str] | None = None,
    available_tools: list[str] | None = None,
    available_models: list[str] | None = None,
) -> dict[str, Any]:
    """Build interactive card for creating a new employee via Feishu chat.

    Tool/model lists are discovered from the same ACP provider registry
    that Deep, Spec, Worktree, and Workflow use. Falls back to static
    defaults only when discovery fails.
    """
    roles = available_roles or ["coder", "reviewer", "planner", "tester", "researcher"]
    tools = available_tools or _discover_tools()
    models = available_models or _get_all_models(tools)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Create New Employee"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "Configure a new autonomous employee for your team:",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": "Select Role"},
                        "options": [
                            {"text": {"tag": "plain_text", "content": r}, "value": r}
                            for r in roles
                        ],
                        "value": {"key": "employee_role"},
                    },
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": "Select Tool"},
                        "options": [
                            {"text": {"tag": "plain_text", "content": t}, "value": t}
                            for t in tools
                        ],
                        "value": {"key": "employee_tool"},
                    },
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": "Select Model (or use tool default)"},
                        "options": [
                            {"text": {"tag": "plain_text", "content": "Auto (tool default)"}, "value": "__auto__"},
                            *[
                                {"text": {"tag": "plain_text", "content": m}, "value": m}
                                for m in models
                            ],
                        ],
                        "value": {"key": "employee_model"},
                    },
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Create Employee"},
                        "type": "primary",
                        "value": {"action": "create_employee"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Cancel"},
                        "type": "default",
                        "value": {"action": "cancel"},
                    },
                ],
            },
        ],
    }


def _get_all_models(tools: list[str]) -> list[str]:
    """Collect unique models from all discovered tools."""
    seen: set[str] = set()
    models: list[str] = []
    for tool in tools:
        for m in _discover_models_for_tool(tool):
            if m not in seen:
                seen.add(m)
                models.append(m)
    return models


def build_employee_created_card(
    *,
    employee_id: str,
    name: str,
    role: str,
    tool: str,
    model: str,
    worker_type: str,
) -> dict[str, Any]:
    """Build confirmation card after employee creation."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Employee Created"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**ID:** {employee_id}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Name:** {name}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Role:** {role}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Tool:** {tool}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Model:** {model}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Type:** {worker_type}"}},
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "Employee is now active and ready to accept work."},
                ],
            },
        ],
    }


def build_goal_progress_card(
    *,
    goal_id: str,
    description: str,
    state: str,
    run_id: str = "",
    step_progress: str = "",
) -> dict[str, Any]:
    """Build progress card for goal execution."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"Goal: {description[:40]}"},
            "template": "wathet" if state == "executing" else "blue",
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Goal:** {goal_id}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**State:** {state}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Run:** {run_id or 'N/A'}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Progress:** {step_progress or 'Starting...'}"}},
                ],
            },
        ],
    }


def build_approval_card(
    *,
    approval_id: str,
    description: str,
    risk_level: str,
    effect_summary: str,
) -> dict[str, Any]:
    """Build approval request card."""
    template = "red" if risk_level in ("r3", "r4") else "orange"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Approval Required"},
            "template": template,
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**Action:** {description}"},
            },
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Risk:** {risk_level.upper()}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**Effect:** {effect_summary}"}},
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Approve"},
                        "type": "primary",
                        "value": {"action": "approve", "approval_id": approval_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Reject"},
                        "type": "danger",
                        "value": {"action": "reject", "approval_id": approval_id},
                    },
                ],
            },
        ],
    }
