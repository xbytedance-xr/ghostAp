from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional

from .models import WorktreeSelectionItem, WorktreeSelectionState


@dataclass(frozen=True)
class WorktreeToolOption:
    provider: str
    tool_name: str
    display_name: str
    agent_name: str = ""
    description: str = ""
    supports_model: bool = True
    model_optional: bool = False
    skip_model_selection: bool = False


def provider_display_name(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized == "ttadk":
        return "TTADK"
    if normalized == "acp":
        return "ACP"
    return normalized.upper() or "UNKNOWN"


def build_selection_item(option: WorktreeToolOption) -> WorktreeSelectionItem:
    provider = str(option.provider or "").strip().lower()
    tool_name = str(option.tool_name or "").strip().lower()
    agent_name = str(option.agent_name or "").strip().lower()
    if not agent_name and provider == "ttadk" and tool_name != "ttadk":
        agent_name = "ttadk"
    return WorktreeSelectionItem(
        provider=provider,
        tool_name=tool_name,
        display_name=str(option.display_name or option.tool_name or "").strip(),
        agent_name=agent_name,
        supports_model=bool(option.supports_model),
        model_optional=bool(option.model_optional),
        skip_model_selection=bool(option.skip_model_selection),
        metadata={
            "provider_display_name": provider_display_name(option.provider),
            "description": str(option.description or "").strip(),
        },
    )


def apply_model_to_item(
    pending_item: WorktreeSelectionItem,
    *,
    model_name: Optional[str],
    model_display_name: Optional[str] = None,
) -> WorktreeSelectionItem:
    return replace(
        pending_item,
        model_name=(str(model_name or "").strip() or None),
        model_display_name=(str(model_display_name or model_name or "").strip() or None),
    )


def format_selection_lines(items: Iterable[WorktreeSelectionItem]) -> list[str]:
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        provider_name = item.metadata.get("provider_display_name") or provider_display_name(item.provider)
        lines.append(f"{idx}. `{provider_name}` · {item.display_label}")
    return lines


