from __future__ import annotations

import logging
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from .models import WorktreeSelectionItem, WorktreeUnit, WorktreeUnitStatus
from ..card.styles import UI_TEXT as _UI_TEXT
from ..config import get_settings
from ..utils.callbacks import safe_invoke
from ..utils.errors import classify_timeout, get_error_detail, sanitize_futures_msg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..agent_session import SyncSession


def _detect_worktree_changes(worktree_path: str) -> bool:
    if not worktree_path:
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            check=False,
            capture_output=True,
            text=True,
        )
        return bool((result.stdout or "").strip())
    except Exception:
        return False


class WorktreeDispatcher:
    def __init__(
        self,
        *,
        session_factory: Optional[Callable[..., "SyncSession"]] = None,
    ) -> None:
        self._session_factory = session_factory or self._default_session_factory

    @staticmethod
    def _default_session_factory(**kwargs):
        from ..agent_session import create_sync_session_for_worktree

        return create_sync_session_for_worktree(**kwargs)

    # Tools known for strong reasoning / analysis capabilities
    _REASONING_TOOLS: frozenset[str] = frozenset({"claude", "gemini"})

    def plan_user_goal(self, goal: str, units: Iterable[WorktreeUnit], tools: Iterable[WorktreeSelectionItem]) -> list[WorktreeUnit]:
        normalized_goal = str(goal or "").strip()
        planned_units = list(units)
        tool_pool = list(tools)
        if not planned_units or not tool_pool:
            return planned_units

        # 决定角色与工具分配
        assignments = self._assign_roles_smart(planned_units, tool_pool)
        for unit, (tool, role_info) in zip(planned_units, assignments):
            title, role_prompt = role_info
            
            # 动态绑定工具
            unit.provider = tool.provider
            unit.tool_name = tool.tool_name
            unit.selection_key = tool.selection_key
            unit.display_name = tool.display_name
            unit.model_name = tool.model_name
            
            unit.task_title = title
            unit.task_prompt = (
                f"用户目标：{normalized_goal}\n"
                f"你的角色：{role_prompt}\n"
                "请只在当前 worktree 中工作，并输出清晰结论与必要修改。"
            )
            unit.status = WorktreeUnitStatus.PLANNED
        return planned_units

    def _fail_unit(
        self,
        unit: WorktreeUnit,
        error_msg: str,
        *,
        log_level: int = logging.ERROR,
        on_unit_update: Optional[Callable[[WorktreeUnit], None]] = None,
    ) -> None:
        unit.status = WorktreeUnitStatus.FAILED
        unit.error = error_msg
        unit.summary = error_msg
        logger.log(log_level, "[Worktree] 单元失败: unit=%s, error=%s", unit.unit_id, error_msg)
        if on_unit_update:
            try:
                on_unit_update(unit)
            except Exception:
                logger.debug("on_unit_update callback failed", exc_info=True)

    def execute_units(
        self,
        units: Iterable[WorktreeUnit],
        *,
        timeout: Optional[int] = None,
        max_workers: Optional[int] = None,
        on_unit_update: Optional[Callable[[WorktreeUnit], None]] = None,
        pool_timeout: Optional[int] = None,
    ) -> list[WorktreeUnit]:
        planned_units = list(units)
        if not planned_units:
            return []

        if pool_timeout is None:
            settings = get_settings()
            pool_timeout = getattr(settings, "worktree_pool_timeout", 600)

        workers = max(1, min(max_workers or len(planned_units), len(planned_units)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(self._run_single_unit, unit, timeout=timeout, on_unit_update=on_unit_update): unit for unit in planned_units}
            processed_futures = set()
            try:
                for future in as_completed(future_map, timeout=pool_timeout):
                    unit = future_map[future]
                    processed_futures.add(future)
                    try:
                        future.result()
                    except Exception as exc:
                        # 使用 classify_timeout 区分超时和其他错误
                        err_msg = get_error_detail(exc)
                        log_level = logging.WARNING if classify_timeout(exc) else logging.ERROR
                        self._fail_unit(unit, err_msg, log_level=log_level, on_unit_update=on_unit_update)
            except TimeoutError:
                # 处理 pool-level timeout
                unprocessed_futures = set(future_map.keys()) - processed_futures
                # 使用域语义文案
                err = _UI_TEXT["timeout_busy_worktree"]
                for fut in unprocessed_futures:
                    unit = future_map[fut]
                    fut.cancel()
                    logger.warning("[Worktree] unit %s timeout: %s", unit.unit_id, err)
                    self._fail_unit(unit, err, log_level=logging.WARNING, on_unit_update=on_unit_update)
        return planned_units

    def _run_single_unit(self, unit: WorktreeUnit, *, timeout: Optional[int] = None, on_unit_update: Optional[Callable[[WorktreeUnit], None]] = None) -> None:
        # 防止 pool-level timeout 设置的 failed 状态被覆盖
        if unit.status == WorktreeUnitStatus.FAILED:
            return
        unit.status = WorktreeUnitStatus.RUNNING
        if on_unit_update:
            try:
                on_unit_update(unit)
            except Exception:
                logger.debug("on_unit_update callback failed", exc_info=True)
        
        # 如果尚未分配具体工具（理论上 plan 阶段已完成分配，这里做安全检查）
        if not all([unit.provider, unit.tool_name]):
            self._fail_unit(unit, "工作单元未绑定执行工具", on_unit_update=on_unit_update)
            return

        session = self._session_factory(
            provider=unit.provider,
            tool_name=unit.tool_name,
            working_dir=unit.worktree_path,
            model_name=unit.model_name,
        )
        try:
            session.start()
            result = session.send_prompt(unit.task_prompt or unit.task_title, timeout=timeout)
            unit.summary = (getattr(result, "text", "") or "").strip()
            unit.status = WorktreeUnitStatus.COMPLETED if getattr(result, "stop_reason", "") not in {"failed", "error", "cancelled"} else WorktreeUnitStatus.FAILED
            unit.error = "" if unit.status == WorktreeUnitStatus.COMPLETED else unit.summary
            unit.has_changes = _detect_worktree_changes(unit.worktree_path)
            safe_invoke(on_unit_update, unit, label="on_unit_update")
        except TimeoutError as te:
            self._fail_unit(unit, f"执行超时: {get_error_detail(te)}", log_level=logging.WARNING, on_unit_update=on_unit_update)
        except Exception as exc:
            self._fail_unit(unit, f"执行异常: {get_error_detail(exc)}", log_level=logging.ERROR, on_unit_update=on_unit_update)
        finally:
            try:
                session.close()
            except Exception:
                logger.debug("failed to close session", exc_info=True)

    def _assign_roles_smart(
        self,
        units: list[WorktreeUnit],
        tools: list[WorktreeSelectionItem]
    ) -> list[tuple[WorktreeSelectionItem, tuple[str, str]]]:
        """Assign analysis/implementation/review roles and bind tools from pool.

        Reasoning-strong tools (claude, gemini) are preferred for analysis and
        review. Remaining tools are assigned implementation roles.
        """
        role_defs = {
            "analysis": ("分析与方案", "先理解需求、梳理风险与改动范围，并给出执行建议"),
            "implement": ("实现与修改", "聚焦代码实现、必要修改与验证思路"),
            "review": ("审查与汇总", "复核前面结果，指出风险、遗漏与合并建议"),
        }

        count = len(units)
        if count <= 1:
            # Only one unit, use the first tool for comprehensive handling
            tool = tools[0] if tools else WorktreeSelectionItem(provider="none", tool_name="none", display_name="None")
            return [(tool, ("综合处理", "完成需求分析、实现与复核，给出最终建议"))]

        # Classify tool pool
        reasoning_tools = [t for t in tools if t.tool_name in self._REASONING_TOOLS]
        other_tools = [t for t in tools if t.tool_name not in self._REASONING_TOOLS]

        # Shuffle for diversity if multiple options exist
        random.shuffle(reasoning_tools)
        random.shuffle(other_tools)

        assignments: list[tuple[WorktreeSelectionItem, tuple[str, str]]] = []
        
        # 1. Pick analyser – prefer reasoning tool
        if reasoning_tools:
            analyser_tool = reasoning_tools.pop(0)
        else:
            analyser_tool = other_tools.pop(0)

        # 2. Pick reviewer (if count >= 2) – prefer reasoning tool (different from analyser)
        reviewer_tool = None
        if count >= 2:
            if reasoning_tools:
                reviewer_tool = reasoning_tools.pop(0)
            elif other_tools:
                reviewer_tool = other_tools.pop(-1) # Take from the end to leave room for implementers

        # 3. Remaining are implementers
        remaining = reasoning_tools + other_tools
        random.shuffle(remaining)

        # Build sequence: Analysis -> Implementation(s) -> Review
        # Analysis first
        assignments.append((analyser_tool, role_defs["analysis"]))

        # Implementers middle
        impl_target_count = count - (2 if reviewer_tool else 1)
        if reviewer_tool:
            # If we have a reviewer, we expect count-2 implementers
            impl_target_count = count - 2
        else:
            # This case shouldn't happen if count >= 2 due to logic above
            impl_target_count = count - 1
        for i in range(impl_target_count):
            if remaining:
                tool = remaining.pop(0)
            else:
                # Fallback: re-use existing tools if pool is smaller than units (theoretically shouldn't happen)
                tool = tools[i % len(tools)]
            
            label = f"实现与修改 {i + 1}" if impl_target_count > 1 else "实现与修改"
            assignments.append((tool, (label, role_defs["implement"][1])))

        # Review last
        if reviewer_tool:
            assignments.append((reviewer_tool, role_defs["review"]))

        return assignments

    @staticmethod
    def _build_role_templates(count: int) -> list[tuple[str, str]]:
        if count <= 1:
            return [("综合处理", "完成需求分析、实现与复核，给出最终建议")]
        templates = [
            ("分析与方案", "先理解需求、梳理风险与改动范围，并给出执行建议"),
            ("实现与修改", "聚焦代码实现、必要修改与验证思路"),
            ("审查与汇总", "复核前面结果，指出风险、遗漏与合并建议"),
        ]
        roles: list[tuple[str, str]] = []
        for index in range(count):
            if index == 0:
                roles.append(templates[0])
            elif index == count - 1:
                roles.append(templates[2])
            else:
                roles.append((f"实现与修改 {index}", templates[1][1]))
        return roles
