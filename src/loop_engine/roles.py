"""RoleRouter — 动态角色选择器。

根据迭代状态，按优先级规则选择最适合的执行角色。
规则优先，覆盖所有常见场景，避免额外 LLM 调用开销。
"""

from .models import (
    LoopRole,
    IterationState,
    IterationStatus,
    RoleSelection,
)


# ---------------------------------------------------------------------------
# Role system prompts
# ---------------------------------------------------------------------------

ROLE_PROMPTS: dict[LoopRole, str] = {
    LoopRole.ARCHITECT: (
        "你是一位资深架构师。请从系统设计的角度分析需求，"
        "设计模块结构、数据模型和接口定义。输出清晰的架构方案，"
        "为后续开发奠定基础。"
    ),
    LoopRole.DEVELOPER: (
        "你是一位高效开发者。请专注于功能实现，编写高质量代码。"
        "确保代码结构清晰、命名规范、逻辑正确。"
    ),
    LoopRole.REVIEWER: (
        "你是一位严格的代码审查者。请检查已有代码的质量、安全性和边界情况。"
        "指出问题并直接修复，确保代码满足生产标准。"
    ),
    LoopRole.TESTER: (
        "你是一位测试专家。请编写单元测试和集成测试，"
        "运行测试并确保所有测试通过。覆盖正常路径和边界情况。"
    ),
    LoopRole.DEBUGGER: (
        "你是一位调试专家。之前的迭代遇到了问题，"
        "请诊断错误原因，分析日志和输出，然后修复问题。"
    ),
    LoopRole.INTEGRATOR: (
        "你是一位集成工程师。请确保所有模块协同工作，解决模块间的冲突，执行端到端验证。"
    ),
}


class RoleRouter:
    """按优先级规则选择迭代角色。"""

    def select_role(self, state: IterationState) -> RoleSelection:
        """根据迭代状态选择角色。

        优先级（从高到低）:
        1. 首轮迭代 → ARCHITECT
        2. 连续失败 ≥2 → DEBUGGER
        3. 所有功能标准满足 & 无测试记录 → TESTER
        4. 测试通过 & 有未集成模块 → INTEGRATOR
        5. 代码量较大 & 无审查记录 → REVIEWER
        6. 默认 → DEVELOPER
        """
        # 优先级 1: 首轮 → ARCHITECT
        if state.iteration_number == 1:
            return RoleSelection(
                role=LoopRole.ARCHITECT,
                reason="首轮迭代，需要架构设计",
                focus="系统架构设计与模块划分",
            )

        # 优先级 2: 连续失败 ≥2 → DEBUGGER
        if state.consecutive_failures >= 2:
            return RoleSelection(
                role=LoopRole.DEBUGGER,
                reason=f"连续{state.consecutive_failures}次失败，需要诊断问题",
                focus="诊断错误原因并修复",
            )

        satisfied_ratio = self._satisfied_ratio(state)
        has_tester = self._has_role_iteration(state, LoopRole.TESTER)
        has_reviewer = self._has_role_iteration(state, LoopRole.REVIEWER)
        has_integrator = self._has_role_iteration(state, LoopRole.INTEGRATOR)

        # 优先级 3: 大部分功能标准满足 & 无测试 → TESTER
        if satisfied_ratio >= 0.6 and not has_tester:
            return RoleSelection(
                role=LoopRole.TESTER,
                reason="多数功能标准已满足，需要测试验证",
                focus="编写和运行测试",
            )

        # 优先级 4: 测试通过 & 无集成 → INTEGRATOR
        if has_tester and not has_integrator and satisfied_ratio >= 0.8:
            return RoleSelection(
                role=LoopRole.INTEGRATOR,
                reason="测试已完成，需要集成验证",
                focus="模块集成与端到端验证",
            )

        # 优先级 5: 迭代 ≥3 轮 & 无审查 → REVIEWER
        if state.iteration_number >= 3 and not has_reviewer:
            return RoleSelection(
                role=LoopRole.REVIEWER,
                reason="代码量增加，需要质量审查",
                focus="代码质量与安全审查",
            )

        # 优先级 6: 默认 → DEVELOPER
        unsatisfied = state.criteria_tracker.unsatisfied_criteria
        focus = unsatisfied[0] if unsatisfied else "继续推进功能实现"
        return RoleSelection(
            role=LoopRole.DEVELOPER,
            reason="继续推进功能实现",
            focus=focus,
        )

    def get_role_prompt(self, role: LoopRole) -> str:
        """获取角色的 system prompt。"""
        return ROLE_PROMPTS.get(role, ROLE_PROMPTS[LoopRole.DEVELOPER])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _satisfied_ratio(state: IterationState) -> float:
        """已满足标准的比例 (0.0 ~ 1.0)。"""
        total = state.criteria_tracker.total_count
        if total == 0:
            return 0.0
        return state.criteria_tracker.satisfied_count / total

    @staticmethod
    def _has_role_iteration(state: IterationState, role: LoopRole) -> bool:
        """历史迭代中是否有指定角色的成功记录。"""
        return any(
            r.role == role and r.status == IterationStatus.SUCCESS
            for r in state.recent_iterations
        )
