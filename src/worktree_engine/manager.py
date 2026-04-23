from __future__ import annotations

import logging
import shutil
from typing import Callable, Optional

from ..acp.helper import fetch_acp_models, list_acp_tools
from ..acp.providers import tool_registry
from ..project.context import ProjectContext
from ..ttadk import get_ttadk_manager
from .dispatcher import WorktreeDispatcher
from .git_service import WorktreeGitService
from .models import (
    DeleteWarning,
    WorktreeJourneyStatus,
    WorktreeRuntimeState,
    transition_journey_state,
)
from .reporter import WorktreeReporter
from .selection import (
    WorktreeModelOption,
    WorktreeToolOption,
    apply_model_to_item,
    build_selection_item,
    format_selection_lines,
)

logger = logging.getLogger(__name__)


class WorktreeManager:
    def __init__(self, project_manager):
        self._project_manager = project_manager
        self._git = WorktreeGitService()
        self._dispatcher = WorktreeDispatcher()
        self._reporter = WorktreeReporter()

    def get_available_tools(self) -> list[dict]:
        """Return available tools as dicts suitable for card builders.

        Probes three provider categories:
        1. ACP direct tools (coco, aiden, codex, gemini)
        2. CLI tools (claude)
        3. TTADK-managed tools (filtered by shutil.which)
        """
        tools: list[dict] = []
        seen: set[str] = set()

        # --- ACP tools ---
        acp_tools = list_acp_tools()
        for t in acp_tools:
            name = t.name
            if name in seen:
                continue
            if shutil.which(name):
                provider = tool_registry.get_provider(name)
                skip = (
                    getattr(provider, "skip_model_selection", False)
                    if provider
                    else False
                )
                tools.append(
                    WorktreeToolOption(
                        provider="acp",
                        tool_name=name,
                        display_name=name.capitalize(),
                        description=t.description,
                        supports_model=True,
                        model_optional=True,
                        skip_model_selection=skip,
                    ).__dict__
                )
                seen.add(name)

        # --- CLI tools ---
        if "claude" not in seen and shutil.which("claude"):
            tools.append(
                WorktreeToolOption(
                    provider="cli",
                    tool_name="claude",
                    display_name="Claude",
                    description="Anthropic Claude CLI",
                    supports_model=False,
                ).__dict__
            )
            seen.add("claude")

        # --- TTADK tools ---
        try:
            manager = get_ttadk_manager()
            result = manager.get_tools()
            for t in result.tools:
                name = t.name
                if name in seen:
                    continue
                tools.append(
                    WorktreeToolOption(
                        provider="ttadk",
                        tool_name=name,
                        display_name=t.description or name,
                        description=f"TTADK · {name}",
                        supports_model=True,
                        model_optional=True,
                        skip_model_selection=getattr(t, "skip_model_selection", False),
                    ).__dict__
                )
                seen.add(name)
        except Exception:
            pass

        return tools

    def get_models_for_tool(
        self,
        tool_name: str,
        provider: str = "ttadk",
        cwd: Optional[str] = None,
        current_model: Optional[str] = None,
    ) -> list[dict]:
        """Return available models for a tool (ACP or TTADK) as dicts for card builder."""
        if provider == "acp":
            try:
                acp_models = fetch_acp_models(
                    tool_name, cwd=cwd, current_model=current_model
                )
                return [
                    {
                        "name": m.name,
                        "display_name": m.description or m.name,
                        "is_default": m.is_default,
                    }
                    for m in acp_models
                ]
            except Exception:
                return []

        try:
            manager = get_ttadk_manager()
            models_result = manager.get_models(tool_name=tool_name, cwd=cwd)
            models = []
            for m in (models_result.models if models_result else []):
                models.append(
                    {
                        "name": m.name,
                        "display_name": getattr(m, "friendly_name", None)
                        or getattr(m, "display_name", None)
                        or m.name,
                        "is_default": getattr(m, "is_default", False),
                    }
                )
            return models
        except Exception:
            return []

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

    # ------------------------------------------------------------------
    # Awaiting-goal helper
    # ------------------------------------------------------------------

    @staticmethod
    def is_awaiting_goal(state: Optional[WorktreeRuntimeState]) -> bool:
        """返回当前 worktree 是否处于“等待用户 goal 输入/确认”的旅程阶段。

        本函数是 Worktree 旅程状态机在“是否等待 goal”维度上的**唯一判定入口**，
        依赖 ``WorktreeJourneyStatus`` 作为真值来源，约定如下真值表（忽略异常场景）：

        - ``IDLE``          → ``False``  （尚未进入 /wt 旅程，或已完全重置）
        - ``PENDING``       → ``True``   （已记录 goal / pending_goal，等待执行或确认）
        - ``AUTO_EXECUTING``→ ``True``   （自动执行快速路径中，仍视为处于等待/处理该 goal 的阶段）
        - ``RUNNING``       → ``False``  （已开始实际执行各 worktree 单元，不再拦截新 goal）
        - ``COMPLETED``     → ``False``  （本次旅程已完成，后续消息按普通 SMART 流程处理）
        - ``FAILED``        → ``False``  （本次旅程失败，是否重试由上层显式操作触发）

        结合运行态中的 unit 列表，本函数的**完整判定规则**为：

        - 仅当 ``journey.status`` 处于 ``PENDING`` / ``AUTO_EXECUTING``，且
          至少存在一个 ``unit.status == "ready"`` 的工作单元时返回 ``True``；
        - 其它枚举值（``IDLE``/``RUNNING``/``COMPLETED``/``FAILED``）、缺失字段、
          类型不匹配或内部异常一律返回 ``False``，以避免在 WS 层抛出错误。

        若未来新增 ``WorktreeJourneyStatus`` 枚举成员，**必须同步更新本函数上方
        的真值表契约以及对应单元测试**（遍历全部枚举值的参数化用例），否则视为
        状态机设计不完整。
        """

        if not isinstance(state, WorktreeRuntimeState):
            return False

        try:
            journey = getattr(state, "journey", None)
            if journey is None:
                return False

            status = getattr(journey, "status", None)
            # 显式基于枚举的真值表：仅在 PENDING/AUTO_EXECUTING 阶段才有可能等待 goal。
            awaiting_by_status = {
                WorktreeJourneyStatus.PENDING: True,
                WorktreeJourneyStatus.AUTO_EXECUTING: True,
                WorktreeJourneyStatus.IDLE: False,
                WorktreeJourneyStatus.RUNNING: False,
                WorktreeJourneyStatus.COMPLETED: False,
                WorktreeJourneyStatus.FAILED: False,
            }

            if awaiting_by_status.get(status) is not True:
                return False

            units = getattr(state, "units", None) or []
            return any(getattr(u, "status", "") == "ready" for u in units)
        except Exception:
            # 保守兜底：出现异常时视为“未处于等待目标阶段”，交由上层走常规路径。
            return False

    # ------------------------------------------------------------------
    # Journey state helpers
    # ------------------------------------------------------------------

    @staticmethod
    def apply_journey_event(
        state: WorktreeRuntimeState,
        *,
        event: str,
        goal: Optional[str] = None,
        error: Optional[str] = None,
        silent_mode: Optional[bool] = None,
    ) -> None:
        """将语义化旅程事件应用到运行态。

        设计要点：
        - 封装对 ``transition_journey_state`` 的调用，作为单一入口；
        - 始终就地更新 ``state.journey``，避免在 handler 层直接 new/赋值；
        - 不吞掉任何错误信息，由状态机自身在 ``last_error`` 中记录非法迁移。

        该方法为副作用函数（就地更新 state），返回值通过 ``state.journey`` 读取，
        便于在调用链中自然传递完整运行态。
        """

        try:
            state.journey = transition_journey_state(
                state.journey,
                event=event,
                goal=goal,
                error=error,
                silent_mode=silent_mode,
            )
        except Exception:
            # 出现意外异常时，不破坏现有运行态，仅在 last_error 中做一个兜底标记，
            # 具体错误信息由上层日志记录负责，避免在状态机内部引入 logging 依赖。
            msg = f"旅程事件应用失败: {event or ''}"
            state.journey.last_error = state.journey.last_error or msg

    def start_selection(self, project: ProjectContext, goal: str = "") -> WorktreeRuntimeState:
        state = self.get_state(project)
        # 每次开始新的选择流程时，将旅程重置为 IDLE，避免复用上一次执行残留的 goal / 错误信息。
        self.apply_journey_event(state, event="reset")
        state.selection.active = True
        state.selection.stage = "tool_select"
        state.selection.pending_item = None
        state.selection.pending_goal = str(goal or "").strip()
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
        
        # 清空 pending_goal，避免对后续流程造成干扰
        state.selection.pending_goal = ""
        
        return state

    def ensure_worktrees(
        self,
        project: ProjectContext,
        *,
        custom_base_dir: Optional[str] = None,
    ) -> WorktreeRuntimeState:
        state = self.get_state(project)
        if not state.selection.selected_items:
            state.last_error = "当前没有可创建 worktree 的工具-模型组合"
            return state
        try:
            repo_state, units = self._git.create_units(
                root_path=project.root_path,
                count=len(state.selection.selected_items),
                base_branch=state.base_branch or None,
                custom_base_dir=custom_base_dir,
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
        """根据用户目标规划各 worktree 单元并更新旅程状态。

        语义：
        - 记录 ``last_user_goal`` 作为本次旅程的语义目标；
        - 触发 ``goal_created`` 事件，将旅程从 IDLE 推进到 PENDING；
        - 调用 dispatcher 生成 unit 级计划，并通过 reporter 刷新摘要信息。
        """

        state = self.get_state(project)
        state.last_user_goal = str(goal or "").strip()
        # 先更新旅程高层状态，再进行具体的 unit 规划。
        self.apply_journey_event(state, event="goal_created", goal=state.last_user_goal)
        state.units = self._dispatcher.plan_user_goal(
            state.last_user_goal, state.units, state.selection.selected_items
        )
        return self._reporter.refresh_state(state)

    def execute_goal(
        self,
        project: ProjectContext,
        goal: str,
        *,
        timeout: Optional[int] = None,
        on_unit_update: Optional[Callable] = None,
    ) -> WorktreeRuntimeState:
        """执行已规划的 worktree 旅程，并推进高层旅程状态机。

        状态机语义：
        - 当存在可执行单元且无致命错误时，进入 ``execution_started``（RUNNING）；
        - 执行成功后触发 ``execution_succeeded``（COMPLETED）；
        - 执行过程中出现异常时触发 ``execution_failed``（FAILED），并在 ``state.last_error`` 中保留细节。
        """

        state = self.get_state(project)
        if not state.units:
            state = self.ensure_worktrees(project)
        if state.last_error or not state.units:
            if not state.last_error:
                state.last_error = "当前没有可执行的 worktree 工作单元"
            return self._reporter.refresh_state(state)

        # 规划目标并进入 RUNNING 之前，先更新旅程事件流。
        state = self.plan_goal(project, goal)
        self.apply_journey_event(state, event="execution_started", goal=state.last_user_goal)

        try:
            state.units = self._dispatcher.execute_units(
                state.units, timeout=timeout, on_unit_update=on_unit_update,
            )
            state.last_error = ""
            self.apply_journey_event(state, event="execution_succeeded")
        except Exception as exc:
            from ..utils.errors import get_error_detail

            state.last_error = get_error_detail(exc)
            self.apply_journey_event(state, event="execution_failed", error=state.last_error)
        return self._reporter.refresh_state(state)

    # ------------------------------------------------------------------
    # Retry failed units
    # ------------------------------------------------------------------

    def retry_failed_units(
        self,
        project: ProjectContext,
        *,
        timeout: Optional[int] = None,
        on_unit_update: Optional[Callable] = None,
    ) -> WorktreeRuntimeState:
        """Re-execute only the failed units, preserving completed ones.

        Reuses ``last_user_goal`` — caller does not need to re-supply the goal.
        """
        state = self.get_state(project)

        # Guard: need a goal from the previous run
        if not state.last_user_goal:
            state.last_error = "没有可重试的目标（上次执行目标为空）"
            return self._reporter.refresh_state(state)

        # Guard: must have failed units to retry
        failed_units = [u for u in state.units if u.status == "failed"]
        if not failed_units:
            state.last_error = "当前没有失败的工作单元需要重试"
            return self._reporter.refresh_state(state)

        # Guard: no units should be running (concurrent safety)
        if any(u.status == "running" for u in state.units):
            state.last_error = "存在正在执行的单元，请等待执行完成后再重试"
            return self._reporter.refresh_state(state)

        # Reset failed units' state fields
        for unit in failed_units:
            unit.status = "pending"
            unit.error = ""
            unit.summary = ""
            unit.has_changes = False

        # Re-plan only failed units (keep completed units unchanged)
        failed_units = self._dispatcher.plan_user_goal(
            state.last_user_goal, failed_units, state.selection.selected_items
        )

        # Execute only the re-planned (previously-failed) units
        try:
            self._dispatcher.execute_units(
                failed_units, timeout=timeout, on_unit_update=on_unit_update,
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

    def cleanup_worktrees(
        self,
        project: ProjectContext,
        *,
        force: bool = True,
    ) -> tuple[WorktreeRuntimeState, list[DeleteWarning]]:
        """Remove all worktree directories and branches, reset state.

        When *force* is ``False``, checks each worktree for safety first.
        Returns ``(state, warnings)`` — warnings is non-empty if any
        worktree was skipped due to uncommitted changes / unmerged branches.
        """
        state = self.get_state(project)
        warnings: list[DeleteWarning] = []
        repo_root = state.git_root or project.root_path
        for unit in state.units:
            try:
                if unit.worktree_path:
                    warning = self._git.remove_worktree(
                        repo_root,
                        unit.worktree_path,
                        force=force,
                        base_branch=state.base_branch or None,
                    )
                    if warning is not None:
                        warnings.append(warning)
                        continue
                if unit.branch_name:
                    self._git.remove_branch(repo_root, unit.branch_name)
            except Exception:
                logger.warning(
                    "清理 worktree 失败: unit=%s path=%s branch=%s",
                    unit.unit_id, unit.worktree_path, unit.branch_name, exc_info=True,
                )
        if not warnings:
            # Only run optimize_storage and reset state when all worktrees cleaned
            try:
                self._git.optimize_storage(repo_root)
            except Exception:
                logger.warning("optimize_storage failed", exc_info=True)
            project.worktree_state = WorktreeRuntimeState()
            return self.get_state(project), []
        return self._reporter.refresh_state(state), warnings

    # ------------------------------------------------------------------
    # List / sync
    # ------------------------------------------------------------------

    def list_worktrees(self, project: ProjectContext) -> tuple[WorktreeRuntimeState, str]:
        """List all worktrees and return formatted table.

        Returns ``(state, table_string)``.
        """
        state = self.get_state(project)
        repo_root = state.git_root or project.root_path
        entries = self._git.list_worktrees(repo_root)
        table = self._reporter.format_worktree_table(entries)
        return state, table

    def sync_worktree(
        self,
        project: ProjectContext,
        worktree_path: str,
        branch: Optional[str] = None,
        *,
        force: bool = False,
    ) -> tuple[WorktreeRuntimeState, Optional[DeleteWarning]]:
        """Sync a worktree to its remote branch's latest state.

        Returns ``(state, warning)`` — warning is non-None if worktree
        has uncommitted changes and *force* is ``False``.
        """
        state = self.get_state(project)
        repo_root = state.git_root or project.root_path
        warning = self._git.sync_worktree(
            repo_root, worktree_path, branch, force=force,
        )
        return state, warning
