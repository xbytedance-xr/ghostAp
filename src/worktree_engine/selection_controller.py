from __future__ import annotations

import logging
from typing import Optional

from ..project.context import ProjectContext
from .models import WorktreeRuntimeState, WorktreeSelectionStage, WorktreeUnitStatus, ensure_worktree_state
from .selection import (
    WorktreeToolOption,
    apply_model_to_item,
    build_selection_item,
    format_selection_lines,
)

logger = logging.getLogger(__name__)


class WorktreeSelectionController:
    """Manages the worktree tool/model selection state machine."""

    @staticmethod
    def _get_state(project: ProjectContext) -> WorktreeRuntimeState:
        return ensure_worktree_state(project)

    def start_selection(self, project: ProjectContext, goal: str = "") -> WorktreeRuntimeState:
        from .models import WorktreeJourneyState, transition_journey_state
        state = self._get_state(project)
        # Reset journey to IDLE to avoid reusing stale goal / error from a previous run.
        state.journey = transition_journey_state(state.journey, event="reset")
        state.selection.active = True
        state.selection.stage = WorktreeSelectionStage.TOOL_SELECT
        state.selection.pending_item = None
        state.selection.pending_goal = str(goal or "").strip()
        state.selection.last_error = ""
        state.selection.last_message = "请选择一个工具开始 worktree 组合"
        return state

    def reset_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        project.worktree_state = WorktreeRuntimeState()
        state = self._get_state(project)
        state.selection.active = True
        state.selection.stage = WorktreeSelectionStage.TOOL_SELECT
        state.selection.last_message = "已清空已有选择，请重新开始"
        return state

    def select_tool(self, project: ProjectContext, option: WorktreeToolOption) -> WorktreeRuntimeState:
        state = self._get_state(project)
        state.selection.active = True
        state.selection.pending_item = build_selection_item(option)
        state.selection.stage = WorktreeSelectionStage.MODEL_SELECT if option.supports_model else WorktreeSelectionStage.REVIEW
        state.selection.last_error = ""
        state.selection.last_message = f"已选择 {option.display_name}"
        return state

    def add_pending_item(
        self,
        project: ProjectContext,
        *,
        model_name: Optional[str] = None,
        model_display_name: Optional[str] = None,
    ) -> tuple[WorktreeRuntimeState, bool, str]:
        state = self._get_state(project)
        pending_item = state.selection.pending_item
        if pending_item is None:
            state.selection.last_error = "当前没有待确认的工具选择"
            return state, False, state.selection.last_error

        final_item = apply_model_to_item(
            pending_item,
            model_name=model_name,
            model_display_name=model_display_name,
        )
        added, existing = state.selection.add_item(final_item)
        state.selection.pending_item = None
        state.selection.stage = WorktreeSelectionStage.REVIEW
        if added:
            state.selection.last_error = ""
            state.selection.last_message = f"已添加 {final_item.display_label}"
            return state, True, state.selection.last_message
        state.selection.last_error = ""
        state.selection.last_message = f"已忽略重复选择：{existing.display_label}"
        return state, False, state.selection.last_message

    def back_to_tool_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        state = self._get_state(project)
        state.selection.pending_item = None
        state.selection.active = True
        state.selection.stage = WorktreeSelectionStage.TOOL_SELECT
        state.selection.last_error = ""
        state.selection.last_message = "请继续选择工具"
        return state

    def finalize_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        state = self._get_state(project)
        state.enabled = bool(state.selection.selected_items)
        state.selection.active = False
        state.selection.pending_item = None
        state.selection.stage = WorktreeSelectionStage.READY if state.enabled else WorktreeSelectionStage.TOOL_SELECT
        state.summary_lines = format_selection_lines(state.selection.selected_items)
        state.selection.last_error = ""
        state.selection.last_message = "已进入 worktree 模式" if state.enabled else "请至少选择一个工具"

        # Clear pending_goal to avoid interference with subsequent flows
        state.selection.pending_goal = ""

        return state

    def set_pending_goal(self, project: ProjectContext, goal: str) -> None:
        """Set the pending goal on the selection state (encapsulates direct mutation)."""
        state = self._get_state(project)
        state.selection.pending_goal = str(goal or "").strip()

    def mark_units_ready(self, project: ProjectContext) -> None:
        """Mark all units as 'ready' (encapsulates direct mutation)."""
        state = self._get_state(project)
        for unit in state.units:
            unit.status = WorktreeUnitStatus.READY
