from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def _clean_optional_str(value: object) -> Optional[str]:
    try:
        text = str(value or "").strip()
    except Exception:
        return None
    return text or None


def _clean_str(value: object, default: str = "") -> str:
    try:
        text = str(value or "").strip()
    except Exception:
        return default
    return text or default


@dataclass
class WorktreeSelectionItem:
    provider: str
    tool_name: str
    display_name: str
    model_name: Optional[str] = None
    model_display_name: Optional[str] = None
    supports_model: bool = True
    model_optional: bool = False
    skip_model_selection: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def selection_key(self) -> str:
        model = _clean_str(self.model_name, default="default")
        return f"{self.provider}:{self.tool_name}:{model}"

    @property
    def display_label(self) -> str:
        if self.model_name:
            return f"{self.display_name} / {self.model_display_name or self.model_name}"
        if self.supports_model:
            return f"{self.display_name} / 默认模型"
        return f"{self.display_name} / 工具内置模型"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["selection_key"] = self.selection_key
        data["display_label"] = self.display_label
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional["WorktreeSelectionItem"]:
        if not isinstance(data, dict):
            return None
        provider = _clean_str(data.get("provider"))
        tool_name = _clean_str(data.get("tool_name"))
        display_name = _clean_str(data.get("display_name") or tool_name)
        if not provider or not tool_name:
            return None
        return cls(
            provider=provider,
            tool_name=tool_name,
            display_name=display_name,
            model_name=_clean_optional_str(data.get("model_name")),
            model_display_name=_clean_optional_str(data.get("model_display_name")),
            supports_model=bool(data.get("supports_model", True)),
            model_optional=bool(data.get("model_optional", False)),
            skip_model_selection=bool(data.get("skip_model_selection", False)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class WorktreeSelectionState:
    active: bool = False
    stage: str = "idle"
    pending_item: Optional[WorktreeSelectionItem] = None
    selected_items: list[WorktreeSelectionItem] = field(default_factory=list)
    pending_goal: str = ""
    last_message: str = ""
    last_error: str = ""

    def add_item(self, item: WorktreeSelectionItem) -> tuple[bool, WorktreeSelectionItem]:
        key = item.selection_key
        for existing in self.selected_items:
            if existing.selection_key == key:
                return False, existing
        self.selected_items.append(item)
        return True, item

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": bool(self.active),
            "stage": _clean_str(self.stage, default="idle"),
            "pending_item": self.pending_item.to_dict() if self.pending_item else None,
            "selected_items": [item.to_dict() for item in self.selected_items],
            "pending_goal": self.pending_goal,
            "last_message": self.last_message,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "WorktreeSelectionState":
        if not isinstance(data, dict):
            return cls()
        pending_item = WorktreeSelectionItem.from_dict(data.get("pending_item"))
        selected_items = []
        for raw in list(data.get("selected_items") or []):
            item = WorktreeSelectionItem.from_dict(raw)
            if item:
                selected_items.append(item)
        return cls(
            active=bool(data.get("active", False)),
            stage=_clean_str(data.get("stage"), default="idle"),
            pending_item=pending_item,
            selected_items=selected_items,
            pending_goal=_clean_str(data.get("pending_goal")),
            last_message=_clean_str(data.get("last_message")),
            last_error=_clean_str(data.get("last_error")),
        )


@dataclass
class WorktreeUnit:
    unit_id: str
    selection_key: str = ""
    provider: str = ""
    tool_name: str = ""
    display_name: str = ""
    model_name: Optional[str] = None
    branch_name: str = ""
    worktree_path: str = ""
    task_title: str = ""
    task_prompt: str = ""
    status: str = "pending"
    has_changes: bool = False
    summary: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional["WorktreeUnit"]:
        if not isinstance(data, dict):
            return None
        unit_id = _clean_str(data.get("unit_id"))
        if not unit_id:
            return None
        return cls(
            unit_id=unit_id,
            selection_key=_clean_str(data.get("selection_key")),
            provider=_clean_str(data.get("provider")),
            tool_name=_clean_str(data.get("tool_name")),
            display_name=_clean_str(data.get("display_name") or data.get("tool_name")),
            model_name=_clean_optional_str(data.get("model_name")),
            branch_name=_clean_str(data.get("branch_name")),
            worktree_path=_clean_str(data.get("worktree_path")),
            task_title=_clean_str(data.get("task_title")),
            task_prompt=_clean_str(data.get("task_prompt")),
            status=_clean_str(data.get("status"), default="pending"),
            has_changes=bool(data.get("has_changes", False)),
            summary=_clean_str(data.get("summary")),
            error=_clean_str(data.get("error")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class DeleteWarning:
    """Warning returned when a worktree has uncommitted changes or unmerged branches."""

    has_uncommitted: bool = False
    uncommitted_files: list[str] = field(default_factory=list)
    has_unmerged: bool = False
    unmerged_branch: str = ""

    @property
    def is_safe(self) -> bool:
        return not self.has_uncommitted and not self.has_unmerged

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_uncommitted": self.has_uncommitted,
            "uncommitted_files": list(self.uncommitted_files),
            "has_unmerged": self.has_unmerged,
            "unmerged_branch": self.unmerged_branch,
            "is_safe": self.is_safe,
        }


@dataclass
class WorktreeInfo:
    """Structured info for a single worktree entry from ``git worktree list``."""

    path: str = ""
    branch: str = ""
    commit: str = ""
    is_active: bool = False
    last_updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "commit": self.commit,
            "is_active": self.is_active,
            "last_updated": self.last_updated,
        }


@dataclass
class WorktreeRuntimeState:
    enabled: bool = False
    git_initialized_locally: bool = False
    git_root: str = ""
    base_branch: str = ""
    merge_entry_ready: bool = False
    last_user_goal: str = ""
    selection: WorktreeSelectionState = field(default_factory=WorktreeSelectionState)
    units: list[WorktreeUnit] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    merge_notes: list[str] = field(default_factory=list)
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "git_initialized_locally": bool(self.git_initialized_locally),
            "git_root": self.git_root,
            "base_branch": self.base_branch,
            "merge_entry_ready": bool(self.merge_entry_ready),
            "last_user_goal": self.last_user_goal,
            "selection": self.selection.to_dict(),
            "units": [unit.to_dict() for unit in self.units],
            "summary_lines": list(self.summary_lines),
            "merge_notes": list(self.merge_notes),
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "WorktreeRuntimeState":
        if not isinstance(data, dict):
            return cls()
        units = []
        for raw in list(data.get("units") or []):
            unit = WorktreeUnit.from_dict(raw)
            if unit:
                units.append(unit)
        return cls(
            enabled=bool(data.get("enabled", False)),
            git_initialized_locally=bool(data.get("git_initialized_locally", False)),
            git_root=_clean_str(data.get("git_root")),
            base_branch=_clean_str(data.get("base_branch")),
            merge_entry_ready=bool(data.get("merge_entry_ready", False)),
            last_user_goal=_clean_str(data.get("last_user_goal")),
            selection=WorktreeSelectionState.from_dict(data.get("selection")),
            units=units,
            summary_lines=[_clean_str(x) for x in list(data.get("summary_lines") or []) if _clean_str(x)],
            merge_notes=[_clean_str(x) for x in list(data.get("merge_notes") or []) if _clean_str(x)],
            last_error=_clean_str(data.get("last_error")),
        )
