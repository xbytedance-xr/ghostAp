from __future__ import annotations

import random
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from .models import WorktreeUnit

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

    def plan_user_goal(self, goal: str, units: Iterable[WorktreeUnit]) -> list[WorktreeUnit]:
        normalized_goal = str(goal or "").strip()
        planned_units = list(units)
        if not planned_units:
            return []

        assignments = self._assign_roles_smart(planned_units)
        for unit, (title, role_prompt) in zip(planned_units, assignments):
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
        on_unit_update: Optional[Callable[[WorktreeUnit], None]] = None,
    ) -> list[WorktreeUnit]:
        planned_units = list(units)
        if not planned_units:
            return []

        workers = max(1, min(max_workers or len(planned_units), len(planned_units)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(self._run_single_unit, unit, timeout=timeout, on_unit_update=on_unit_update): unit for unit in planned_units}
            for future in as_completed(future_map):
                unit = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    unit.status = "failed"
                    unit.error = str(exc).strip() or "执行异常"
                    if on_unit_update:
                        try:
                            on_unit_update(unit)
                        except Exception:
                            pass
        return planned_units

    def _run_single_unit(self, unit: WorktreeUnit, *, timeout: Optional[int] = None, on_unit_update: Optional[Callable[[WorktreeUnit], None]] = None) -> None:
        unit.status = "running"
        if on_unit_update:
            try:
                on_unit_update(unit)
            except Exception:
                pass
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
            if on_unit_update:
                try:
                    on_unit_update(unit)
                except Exception:
                    pass
        except TimeoutError as te:
            unit.status = "failed"
            unit.error = str(te).strip() or f"执行超时 ({timeout}s)"
            unit.summary = unit.error
            if on_unit_update:
                try:
                    on_unit_update(unit)
                except Exception:
                    pass
        finally:
            try:
                session.close()
            except Exception:
                pass

    @classmethod
    def _assign_roles_smart(cls, units: list[WorktreeUnit]) -> list[tuple[str, str]]:
        """Assign analysis/implementation/review roles based on tool characteristics.

        Reasoning-strong tools (claude, gemini) are preferred for analysis and
        review.  All other tools are assigned implementation roles.  When no
        reasoning tool is present the method falls back to positional assignment
        via ``_build_role_templates``.
        """
        role_defs = {
            "analysis": ("分析与方案", "先理解需求、梳理风险与改动范围，并给出执行建议"),
            "implement": ("实现与修改", "聚焦代码实现、必要修改与验证思路"),
            "review": ("审查与汇总", "复核前面结果，指出风险、遗漏与合并建议"),
        }

        if len(units) <= 1:
            return [("综合处理", "完成需求分析、实现与复核，给出最终建议")]

        # Classify units
        reasoning_indices = [i for i, u in enumerate(units) if u.tool_name in cls._REASONING_TOOLS]
        other_indices = [i for i, u in enumerate(units) if u.tool_name not in cls._REASONING_TOOLS]

        # Fallback: no recognisable tool category → use positional assignment
        if not reasoning_indices and not other_indices:
            return cls._build_role_templates(len(units))

        assignments: list[tuple[int, tuple[str, str]]] = []

        # 1. Pick analyser – prefer reasoning tool
        if reasoning_indices:
            analyser_idx = reasoning_indices.pop(0)
        else:
            analyser_idx = other_indices.pop(0)
        assignments.append((analyser_idx, role_defs["analysis"]))

        # 2. Pick reviewer – prefer reasoning tool (different from analyser)
        if reasoning_indices:
            reviewer_idx = reasoning_indices.pop(0)
        elif other_indices:
            reviewer_idx = other_indices.pop(-1)
        else:
            # Only one unit left – re-assigned as reviewer is impossible,
            # fallback handled below.
            reviewer_idx = None

        # 3. Remaining units are implementers
        remaining = reasoning_indices + other_indices
        random.shuffle(remaining)
        impl_count = 0
        for idx in remaining:
            impl_count += 1
            label = f"实现与修改 {impl_count}" if impl_count > 1 else "实现与修改"
            assignments.append((idx, (label, role_defs["implement"][1])))

        # Append reviewer last (review should conceptually happen after implementation)
        if reviewer_idx is not None:
            assignments.append((reviewer_idx, role_defs["review"]))

        # Sort by original unit order so the returned list aligns with *units*
        assignments.sort(key=lambda t: t[0])
        return [role for _, role in assignments]

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
