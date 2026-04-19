from __future__ import annotations

import logging
from typing import Callable, Optional

from ..project.context import ProjectContext
from .dispatcher import WorktreeDispatcher
from .git_service import WorktreeGitService
from .models import WorktreeRuntimeState
from .reporter import WorktreeReporter
from .selection import WorktreeToolOption, apply_model_to_item, build_selection_item, format_selection_lines

logger = logging.getLogger(__name__)


class WorktreeManager:
    def __init__(self, project_manager):
        self._project_manager = project_manager
        self._git = WorktreeGitService()
        self._dispatcher = WorktreeDispatcher()
        self._reporter = WorktreeReporter()

    def ensure_project(self, chat_id: str, project: Optional[ProjectContext] = None) -> Optional[ProjectContext]:
        if project is not None:
            return project
        if self._project_manager is None:
            return None
        return self._project_manager.get_active_project(chat_id)

    @staticmethod
    def get_state(project: ProjectContext) -> WorktreeRuntimeState:
        state = getattr(project, "worktree_state", None)
        if not isinstance(state, WorktreeRuntimeState):
            state = WorktreeRuntimeState()
            project.worktree_state = state
        return state

    def start_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        state = self.get_state(project)
        state.selection.active = True
        state.selection.stage = "tool_select"
        state.selection.pending_item = None
        state.selection.last_error = ""
        state.selection.last_message = "请选择一个工具开始 worktree 组合"
        return state

    def reset_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        project.worktree_state = WorktreeRuntimeState()
        state = self.get_state(project)
        state.selection.active = True
        state.selection.stage = "tool_select"
        state.selection.last_message = "已清空已有选择，请重新开始"
        return state

    def select_tool(self, project: ProjectContext, option: WorktreeToolOption) -> WorktreeRuntimeState:
        state = self.get_state(project)
        state.selection.active = True
        state.selection.pending_item = build_selection_item(option)
        state.selection.stage = "model_select" if option.supports_model else "review"
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
        state = self.get_state(project)
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
        state.selection.stage = "review"
        if added:
            state.selection.last_error = ""
            state.selection.last_message = f"已添加 {final_item.display_label}"
            return state, True, state.selection.last_message
        state.selection.last_error = ""
        state.selection.last_message = f"已忽略重复选择：{existing.display_label}"
        return state, False, state.selection.last_message

    def back_to_tool_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        state = self.get_state(project)
        state.selection.pending_item = None
        state.selection.active = True
        state.selection.stage = "tool_select"
        state.selection.last_error = ""
        state.selection.last_message = "请继续选择工具"
        return state

    def finalize_selection(self, project: ProjectContext) -> WorktreeRuntimeState:
        state = self.get_state(project)
        state.enabled = bool(state.selection.selected_items)
        state.selection.active = False
        state.selection.pending_item = None
        state.selection.stage = "ready" if state.enabled else "tool_select"
        state.summary_lines = format_selection_lines(state.selection.selected_items)
        state.selection.last_error = ""
        state.selection.last_message = "已进入 worktree 模式" if state.enabled else "请至少选择一个工具"
        return state

    def ensure_worktrees(self, project: ProjectContext) -> WorktreeRuntimeState:
        state = self.get_state(project)
        if not state.selection.selected_items:
            state.last_error = "当前没有可创建 worktree 的工具-模型组合"
            return state
        try:
            repo_state, units = self._git.create_units(
                root_path=project.root_path,
                selections=state.selection.selected_items,
                base_branch=state.base_branch or None,
            )
        except Exception as exc:
            from ..utils.errors import get_error_detail
            state.enabled = False
            state.merge_entry_ready = False
            state.last_error = get_error_detail(exc)
            state.summary_lines = [f"- worktree 创建失败：{state.last_error}"]
            return state

        state.enabled = True
        state.git_initialized_locally = bool(repo_state.initialized)
        state.git_root = repo_state.repo_root
        state.base_branch = repo_state.base_branch
        state.units = units
        state.summary_lines = format_selection_lines(state.selection.selected_items)
        state.last_error = ""
        return state

    def plan_goal(self, project: ProjectContext, goal: str) -> WorktreeRuntimeState:
        state = self.get_state(project)
        state.last_user_goal = str(goal or "").strip()
        state.units = self._dispatcher.plan_user_goal(state.last_user_goal, state.units)
        return self._reporter.refresh_state(state)

    def execute_goal(
        self,
        project: ProjectContext,
        goal: str,
        *,
        timeout: Optional[int] = None,
        on_unit_update: Optional[Callable] = None,
    ) -> WorktreeRuntimeState:
        state = self.get_state(project)
        if not state.units:
            state = self.ensure_worktrees(project)
        if state.last_error or not state.units:
            if not state.last_error:
                state.last_error = "当前没有可执行的 worktree 工作单元"
            return self._reporter.refresh_state(state)
        state = self.plan_goal(project, goal)
        try:
            state.units = self._dispatcher.execute_units(
                state.units, timeout=timeout, on_unit_update=on_unit_update,
            )
            state.last_error = ""
        except Exception as exc:
            from ..utils.errors import get_error_detail
            state.last_error = get_error_detail(exc)
        return self._reporter.refresh_state(state)

    # ------------------------------------------------------------------
    # Merge / cleanup
    # ------------------------------------------------------------------

    def merge_to_base(self, project: ProjectContext) -> tuple[WorktreeRuntimeState, list[dict]]:
        """Merge each completed worktree branch into *base_branch*.

        Returns ``(state, merge_results)`` where each result is a dict with
        ``display_name``, ``branch_name``, ``success``, ``detail``.
        """
        state = self.get_state(project)
        if not state.units or not state.base_branch:
            state.last_error = "没有可合并的 worktree 或基础分支未设置"
            return self._reporter.refresh_state(state), []

        merge_results: list[dict] = []
        for unit in state.units:
            if unit.status != "completed" or not unit.has_changes:
                merge_results.append(
                    {"display_name": unit.display_name, "branch_name": unit.branch_name, "success": False, "detail": "跳过（未完成或无变更）"}
                )
                continue
            try:
                ok, conflicts = self._git.merge_branch(state.git_root, unit.branch_name, state.base_branch)
                if ok:
                    merge_results.append({"display_name": unit.display_name, "branch_name": unit.branch_name, "success": True, "detail": "合并成功"})
                else:
                    merge_results.append({"display_name": unit.display_name, "branch_name": unit.branch_name, "success": False, "detail": f"冲突文件: {', '.join(conflicts)}"})
            except Exception as exc:
                from ..utils.errors import get_error_detail
                merge_results.append({"display_name": unit.display_name, "branch_name": unit.branch_name, "success": False, "detail": get_error_detail(exc)})

        state.last_error = ""
        state.merge_entry_ready = False
        return self._reporter.refresh_state(state), merge_results

    def cleanup_worktrees(self, project: ProjectContext) -> WorktreeRuntimeState:
        """Remove all worktree directories and branches, reset state."""
        state = self.get_state(project)
        for unit in state.units:
            try:
                if unit.worktree_path:
                    self._git.remove_worktree(state.git_root or project.root_path, unit.worktree_path)
                if unit.branch_name:
                    self._git.remove_branch(state.git_root or project.root_path, unit.branch_name)
            except Exception:
                logger.warning("清理 worktree 失败: unit=%s path=%s branch=%s", unit.unit_id, unit.worktree_path, unit.branch_name, exc_info=True)
        # Reset state
        project.worktree_state = WorktreeRuntimeState()
        return self.get_state(project)
