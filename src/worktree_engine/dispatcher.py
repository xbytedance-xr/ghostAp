from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from .models import WorktreeUnit

if TYPE_CHECKING:
    from ..agent_session import SyncSession


def _detect_worktree_changes(worktree_path: str) -> bool:
    if not worktree_path:
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool((result.stdout or "").strip())


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

    def plan_user_goal(self, goal: str, units: Iterable[WorktreeUnit]) -> list[WorktreeUnit]:
        normalized_goal = str(goal or "").strip()
        planned_units = list(units)
        if not planned_units:
            return []

        role_templates = self._build_role_templates(len(planned_units))
        for unit, (title, role_prompt) in zip(planned_units, role_templates):
            unit.task_title = title
            unit.task_prompt = (
                f"用户目标：{normalized_goal}\n"
                f"你的角色：{role_prompt}\n"
                "请只在当前 worktree 中工作，并输出清晰结论与必要修改。"
            )
            unit.status = "planned"
        return planned_units

    def execute_units(
        self,
        units: Iterable[WorktreeUnit],
        *,
        timeout: Optional[int] = None,
        max_workers: Optional[int] = None,
    ) -> list[WorktreeUnit]:
        planned_units = list(units)
        if not planned_units:
            return []

        workers = max(1, min(max_workers or len(planned_units), len(planned_units)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(self._run_single_unit, unit, timeout=timeout): unit for unit in planned_units}
            for future in as_completed(future_map):
                unit = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    unit.status = "failed"
                    unit.error = str(exc)
        return planned_units

    def _run_single_unit(self, unit: WorktreeUnit, *, timeout: Optional[int] = None) -> None:
        unit.status = "running"
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
            unit.status = "completed" if getattr(result, "stop_reason", "") not in {"failed", "error", "cancelled"} else "failed"
            unit.error = "" if unit.status == "completed" else unit.summary
            unit.has_changes = _detect_worktree_changes(unit.worktree_path)
        finally:
            try:
                session.close()
            except Exception:
                pass

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
