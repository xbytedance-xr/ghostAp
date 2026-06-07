"""Workflow-aware role selection and prompt generation.

Reuses ReviewRoleSpec definitions from spec_engine where possible and adds
workflow-specific roles (migration_validator, adversarial_verifier, audit_*).
"""

from __future__ import annotations

from src.spec_engine.review_roles import (
    ReviewRoleSpec,
    _research_roles,
    _role,
    fixed_programming_roles,
)

# ---------------------------------------------------------------------------
# Subagent encouragement constant
# ---------------------------------------------------------------------------

SUBAGENT_ENCOURAGEMENT_PROMPT: str = (
    "When a task can be decomposed, always delegate to subagents rather than "
    "doing everything yourself. Each subagent can further spawn its own "
    "subagents or sub-workflows. When you encounter independent sub-problems "
    "during this task — such as researching a library API, validating a "
    "hypothesis, running a set of tests, or drafting an isolated component — "
    "you are strongly encouraged to delegate them to subagents. Subagents work "
    "in parallel and keep the main thread focused on orchestration and "
    "integration. Prefer spawning a subagent over doing everything sequentially "
    "yourself; the overall task will complete faster and with better separation "
    "of concerns. Each subagent should receive a clear, self-contained brief "
    "and return a structured result."
)


def get_subagent_encouragement_prompt() -> str:
    """Return the subagent encouragement paragraph, or "" if disabled via settings.

    Reads ``workflow_subagent_hint_enabled`` at call time so runtime config
    changes are honoured.  Falls back to returning the paragraph when the
    settings module is unavailable (e.g. during isolated unit tests).
    """
    try:
        from src.config import get_settings

        enabled = bool(getattr(get_settings(), "workflow_subagent_hint_enabled", True))
    except Exception:
        enabled = True
    return SUBAGENT_ENCOURAGEMENT_PROMPT if enabled else ""

# ---------------------------------------------------------------------------
# Workflow-specific role definitions
# ---------------------------------------------------------------------------


def _migration_roles() -> list[ReviewRoleSpec]:
    """Roles tailored for code/data migration tasks."""
    return [
        _role(
            "migration_validator",
            "迁移验证员",
            "migration",
            "验证迁移前后行为一致性和数据完整性",
            ["行为等价性", "数据完整性", "回滚方案", "兼容性"],
            ["迁移后输出是否与旧系统一致", "是否存在数据丢失风险", "是否有回滚路径"],
        ),
        _role(
            "compatibility_reviewer",
            "兼容性审查员",
            "migration",
            "审查向后兼容、API 稳定性和依赖影响",
            ["向后兼容", "API 稳定性", "依赖链影响"],
            ["是否破坏现有调用方", "依赖版本是否冲突", "废弃路径是否有迁移指南"],
        ),
        _role(
            "rollback_planner",
            "回滚规划员",
            "migration",
            "审查回滚方案的完整性和可执行性",
            ["回滚步骤", "数据回退", "服务降级"],
            ["回滚是否可在限定时间内完成", "是否覆盖所有变更点"],
            blocking=False,
        ),
    ]


def _audit_roles() -> list[ReviewRoleSpec]:
    """Roles tailored for code/security audit tasks."""
    return [
        _role(
            "security_auditor",
            "安全审计员",
            "security",
            "审计认证授权、注入攻击面、敏感数据处理",
            ["认证授权", "注入攻击", "敏感数据", "加密实践"],
            ["是否存在未鉴权接口", "输入是否经过校验", "敏感数据是否加密存储"],
        ),
        _role(
            "correctness_auditor",
            "正确性审计员",
            "audit",
            "审计业务逻辑正确性、边界条件和异常路径",
            ["逻辑正确性", "边界条件", "异常处理", "并发安全"],
            ["边界值是否处理", "异常路径是否有兜底", "共享状态是否加锁"],
        ),
        _role(
            "adversarial_verifier",
            "对抗验证员",
            "audit",
            "尝试推翻已有发现，验证结论可靠性",
            ["反例构造", "假设验证", "证据链审查"],
            ["发现是否有充分证据", "是否存在反例", "修复建议是否引入新风险"],
            depends_on=["security_auditor", "correctness_auditor"],
        ),
    ]


def _review_roles() -> list[ReviewRoleSpec]:
    """Roles for general code review tasks (lighter than full programming)."""
    return [
        _role(
            "code_quality_reviewer",
            "代码质量审查员",
            "software",
            "审查代码可读性、命名、结构和惯用写法",
            ["可读性", "命名规范", "代码结构", "惯用模式"],
            ["命名是否清晰", "是否遵循项目惯例", "是否有过度抽象"],
        ),
        _role(
            "bug_hunter",
            "缺陷猎手",
            "software",
            "寻找潜在 bug、竞态条件和资源泄漏",
            ["逻辑错误", "竞态条件", "资源泄漏", "空指针"],
            ["是否有未处理的 None/null", "是否有资源未释放", "并发是否安全"],
        ),
        _role(
            "adversarial_verifier",
            "对抗验证员",
            "review",
            "尝试推翻已有审查结论，寻找遗漏",
            ["反例构造", "假设质疑", "遗漏检测"],
            ["其他审查员是否遗漏问题", "修复建议是否合理"],
            depends_on=["code_quality_reviewer", "bug_hunter"],
        ),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_role_ids() -> list[str]:
    """Return the union of all known role IDs for the role selection card.

    The returned list is deterministic and deduplicated. It is used as the
    source of truth for the role selection UI. Unknown IDs should never be
    accepted by the script-generation prompt.
    """
    collectors: list[Callable[[], list[ReviewRoleSpec]]] = [
        fixed_programming_roles,
        _audit_roles,
        _migration_roles,
        _research_roles,
        _review_roles,
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for collector in collectors:
        try:
            for role in collector():
                rid = getattr(role, "id", None)
                if not isinstance(rid, str) or not rid:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                ordered.append(rid)
        except Exception:
            # Swallow unexpected import issues so tests keep running. The
            # downstream consumer will fall back to the default curated set
            # if this list comes back empty.
            continue
    return ordered


def get_role_display_name(role_id: str) -> str:
    """Return the Chinese display name for a role id, falling back to the id itself."""
    for collector in (fixed_programming_roles, _audit_roles, _migration_roles, _research_roles, _review_roles):
        try:
            for role in collector():
                if getattr(role, "id", None) == role_id and getattr(role, "display_name", None):
                    return role.display_name
        except Exception:
            continue
    return role_id


def get_roles_for_workflow(task_kind: str) -> list[ReviewRoleSpec]:
    """Return appropriate review roles based on workflow task kind.

    Args:
        task_kind: One of "programming", "audit", "migration", "research",
                   "review". Falls back to programming roles for unknown kinds.

    Returns:
        List of ReviewRoleSpec instances suitable for the workflow.
    """
    task_kind = (task_kind or "").strip().lower()

    if task_kind == "programming":
        return fixed_programming_roles()
    elif task_kind == "audit":
        return _audit_roles()
    elif task_kind == "migration":
        return _migration_roles()
    elif task_kind == "research":
        return _research_roles()
    elif task_kind == "review":
        return _review_roles()
    else:
        # Default to programming roles for unrecognized task kinds
        return fixed_programming_roles()


def build_adversarial_verify_prompt(
    original_findings: str,
    role: str = "adversarial_verifier",
) -> str:
    """Generate a prompt for adversarial verification of prior findings.

    The adversarial verifier's job is to try to disprove or weaken the original
    findings — finding false positives, missing context, unjustified severity,
    or alternative explanations.

    Args:
        original_findings: The text of findings/conclusions to challenge.
        role: The role identifier (used in the prompt framing).

    Returns:
        A complete prompt string ready to send to a review agent.
    """
    return f"""\
You are acting as an **{role}**. Your mission is to rigorously challenge the \
findings below. Do NOT confirm them — actively try to disprove or weaken each one.

## Original Findings

{original_findings}

## Your Task

For each finding above:

1. **Attempt to disprove**: Construct a counter-argument or identify a scenario \
where the finding does not hold. Cite specific code paths, documentation, or \
runtime conditions that contradict the claim.

2. **Check evidence quality**: Is the evidence cited sufficient? Could the same \
evidence support a different (benign) interpretation?

3. **Assess severity**: Even if the finding is valid, is its severity justified? \
Could the impact be lower than claimed due to mitigating factors (rate limits, \
auth gates, input validation elsewhere)?

4. **Identify false positives**: Flag any findings that are likely false alarms — \
where the described risk is mitigated by existing code not mentioned in the finding.

5. **Surface missing context**: Note any relevant context (project conventions, \
upstream guarantees, deployment constraints) that the original reviewer may have \
missed.

## Output Format

For each finding, respond with:
- **Verdict**: CONFIRMED / WEAKENED / DISPROVED / INSUFFICIENT-EVIDENCE
- **Reasoning**: 2-3 sentences explaining your conclusion
- **Counter-evidence**: Specific references if you found contradicting information

End with a **Summary** listing how many findings survived adversarial scrutiny \
unchanged, how many were weakened, and how many were disproved.\
"""
