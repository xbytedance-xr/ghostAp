"""Adaptive review role planning for Spec Engine.

This module keeps the existing software review perspectives as the default
programming role set while allowing non-code tasks to derive task-specific
review roles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from src.engine_base import ReviewPerspective
from src.spec_engine.review_artifacts import ReviewArtifacts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewRoleSpec:
    """A single reviewer role used by adaptive Spec review."""

    role_id: str
    display_name: str
    category: str
    mission: str
    review_focus: list[str]
    must_check: list[str]
    evidence_policy: str
    blocking: bool = True
    depends_on: list[str] = field(default_factory=list)
    max_suggestions: int = 5
    base_perspective: ReviewPerspective | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["base_perspective"] = self.base_perspective.value if self.base_perspective else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewRoleSpec":
        base = data.get("base_perspective")
        return cls(
            role_id=str(data.get("role_id") or ""),
            display_name=str(data.get("display_name") or data.get("role_id") or ""),
            category=str(data.get("category") or "general"),
            mission=str(data.get("mission") or ""),
            review_focus=[str(x) for x in data.get("review_focus", []) if x],
            must_check=[str(x) for x in data.get("must_check", []) if x],
            evidence_policy=str(data.get("evidence_policy") or "blockers require artifact evidence"),
            blocking=bool(data.get("blocking", True)),
            depends_on=[str(x) for x in data.get("depends_on", []) if x],
            max_suggestions=int(data.get("max_suggestions") or 5),
            base_perspective=ReviewPerspective(base) if base else None,
        )


@dataclass(frozen=True)
class RolePlan:
    """Planned review roles for one Spec review cycle."""

    task_kind: str
    roles: list[ReviewRoleSpec]
    source: str = "heuristic"

    @property
    def blocking_roles(self) -> list[ReviewRoleSpec]:
        return [role for role in self.roles if role.blocking]

    def blocking_role_hash(self) -> str:
        rows = [
            {
                "role_id": role.role_id,
                "category": role.category,
                "mission": role.mission,
                "blocking": role.blocking,
            }
            for role in self.blocking_roles
        ]
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fixed_programming_roles() -> list[ReviewRoleSpec]:
    """Return the fixed software role set matching the current perspectives."""

    roles: list[ReviewRoleSpec] = []
    for perspective in ReviewPerspective:
        roles.append(
            ReviewRoleSpec(
                role_id=perspective.value,
                display_name=perspective.display_name,
                category="software",
                mission=f"从{perspective.display_name}视角审查当前任务结果",
                review_focus=[perspective.review_focus],
                must_check=[perspective.review_focus],
                evidence_policy="blocker and major suggestions must cite requirement, artifact, diff, or touched file evidence",
                blocking=True,
                max_suggestions=5,
                base_perspective=perspective,
            )
        )
    return roles


def detect_task_kind(artifacts: ReviewArtifacts) -> str:
    """Classify task kind using deterministic, cheap evidence."""

    text = " ".join(
        [
            artifacts.requirement or "",
            artifacts.spec_output or "",
            artifacts.plan_output or "",
            artifacts.build_output or "",
            " ".join(artifacts.touched_files or []),
        ]
    ).lower()
    files = [f.lower() for f in (artifacts.touched_files or [])]

    programming_markers = (
        "代码", "实现", "修复", "bug", "api", "测试", "pytest", "函数", "类", "组件",
        "python", "typescript", "javascript", "src/", "tests/", ".py", ".ts", ".tsx",
    )
    writing_markers = ("文章", "公众号", "博客", "标题", "配图", "编辑", "文案", "写一篇", "稿件")
    research_markers = ("调研", "研究", "求证", "来源", "数据来源", "市场", "竞品", "报告", "反方")
    design_markers = ("设计", "视觉", "版式", "海报", "插图", "图表", "ui", "ux")

    if any(marker in text for marker in programming_markers) or any(
        f.startswith(("src/", "tests/")) or re.search(r"\.(py|ts|tsx|js|jsx|go|rs)$", f)
        for f in files
    ):
        return "programming"
    if any(marker in text for marker in research_markers):
        return "research"
    if any(marker in text for marker in writing_markers):
        return "writing"
    if any(marker in text for marker in design_markers):
        return "design"
    return "other"


def _role(
    role_id: str,
    display_name: str,
    category: str,
    mission: str,
    focus: Iterable[str],
    checks: Iterable[str],
    *,
    depends_on: list[str] | None = None,
    blocking: bool = True,
) -> ReviewRoleSpec:
    return ReviewRoleSpec(
        role_id=role_id,
        display_name=display_name,
        category=category,
        mission=mission,
        review_focus=list(focus),
        must_check=list(checks),
        evidence_policy="blocker and major suggestions must cite concrete artifact evidence",
        blocking=blocking,
        depends_on=depends_on or [],
    )


def _programming_dynamic_roles(artifacts: ReviewArtifacts) -> list[ReviewRoleSpec]:
    text = " ".join([artifacts.requirement or "", artifacts.diff_patch or "", " ".join(artifacts.touched_files or [])]).lower()
    roles: list[ReviewRoleSpec] = []
    if any(k in text for k in ("auth", "权限", "登录", "token", "secret", "安全", "加密", "password")):
        roles.append(_role(
            "security_reviewer", "安全审查员", "security",
            "审查认证、授权、敏感信息和安全边界",
            ["认证授权", "敏感信息", "输入校验", "权限绕过"],
            ["是否引入明文 secret", "是否缺少权限校验", "错误路径是否泄露敏感信息"],
        ))
    if any(k in text for k in ("api", "接口", "schema", "payload", "contract")):
        roles.append(_role(
            "api_contract_reviewer", "API 契约审查员", "api",
            "审查接口契约、payload 兼容性和错误语义",
            ["接口 schema", "向后兼容", "错误码", "调用方契约"],
            ["payload 字段是否稳定", "错误语义是否清晰", "是否破坏现有调用方"],
        ))
    if any(k in text for k in ("mobile", "移动", "手机", "ios", "android", "响应式")):
        roles.append(_role(
            "mobile_ux_reviewer", "移动端体验审查员", "ux",
            "审查移动端可用性、密度和交互可达性",
            ["移动端布局", "触控目标", "小屏可读性"],
            ["长文本是否换行合理", "按钮是否可点", "状态是否清晰"],
        ))
    if any(k in text for k in ("性能", "慢", "latency", "timeout", "并发", "队列")):
        roles.append(_role(
            "performance_reviewer", "性能审查员", "performance",
            "审查性能、并发和资源占用风险",
            ["延迟", "并发", "队列", "缓存", "超时"],
            ["是否有不必要串行", "是否存在无界队列", "超时是否有上限"],
        ))
    if any(k in text for k in ("docs", "文档", "readme", "帮助", "help")):
        roles.append(_role(
            "docs_reviewer", "文档审查员", "docs",
            "审查用户可见文档、帮助入口和迁移说明",
            ["文档准确性", "帮助入口", "迁移说明"],
            ["文档是否跟实现一致", "用户能否找到操作入口"],
        ))
    return roles


def _writing_roles() -> list[ReviewRoleSpec]:
    return [
        _role("editor_in_chief", "主编", "writing", "审查主题、结构和叙事完整性", ["结构", "主题", "叙事"], ["主线是否清晰", "段落是否服务目标"]),
        _role("style_editor", "风格编辑", "writing", "审查语气、措辞和阅读节奏", ["语气", "措辞", "节奏"], ["是否啰嗦", "标题和小标题是否有吸引力"]),
        _role("fact_checker", "事实核查员", "research", "核查事实、数据和来源缺口", ["事实准确性", "来源可信度"], ["是否存在无来源事实", "是否过度推断"]),
        _role("target_reader", "目标读者代表", "writing", "从目标读者角度审查理解成本和吸引力", ["可读性", "吸引力", "理解成本"], ["开头是否抓人", "术语是否解释"]),
        _role("visual_designer", "视觉设计师", "design", "审查配图、图表和版式建议", ["配图", "图表", "版式"], ["是否需要图示", "视觉层级是否清晰"], blocking=False),
        _role("distribution_editor", "传播编辑", "writing", "审查标题、摘要和平台传播适配", ["标题", "摘要", "关键词"], ["标题是否清晰", "摘要是否适合平台"], blocking=False),
    ]


def _research_roles() -> list[ReviewRoleSpec]:
    return [
        _role("researcher", "研究员", "research", "审查资料覆盖面和问题拆解", ["覆盖面", "问题拆解"], ["是否遗漏关键维度", "是否定义研究范围"]),
        _role("source_verifier", "求证审查员", "research", "审查来源可信度和交叉验证", ["来源可信度", "交叉验证"], ["关键事实是否有来源", "来源是否可靠"]),
        _role("methodology_reviewer", "方法论审查员", "research", "审查样本偏差、口径和论证方法", ["样本偏差", "统计口径", "方法限制"], ["是否说明口径", "结论是否超出样本"]),
        _role("domain_expert", "领域专家", "domain", "审查行业术语、背景和实践可行性", ["行业知识", "实践可行性"], ["术语是否准确", "结论是否落地"]),
        _role("opposing_view_reviewer", "反方审查员", "research", "寻找反例、替代解释和风险", ["反例", "替代解释", "风险"], ["是否存在相反证据", "是否忽视风险"]),
        _role("conclusion_editor", "结论编辑", "writing", "审查结论表达和行动建议", ["结论", "行动建议"], ["结论是否克制", "建议是否可执行"], depends_on=["source_verifier"]),
    ]


def _design_roles() -> list[ReviewRoleSpec]:
    return [
        _role("creative_director", "创意总监", "design", "审查创意方向和表达统一性", ["创意方向", "统一性"], ["视觉目标是否明确"]),
        _role("visual_designer", "视觉设计师", "design", "审查版式、层级、配色和图像建议", ["版式", "层级", "配色"], ["层级是否清晰", "视觉是否服务内容"]),
        _role("user_reviewer", "用户体验审查员", "design", "审查目标用户理解和交互路径", ["理解成本", "交互路径"], ["用户是否能快速理解"]),
        _role("accessibility_reviewer", "可访问性审查员", "design", "审查对比度、可读性和包容性", ["对比度", "可读性"], ["小屏和弱视场景是否可用"]),
    ]


def _generic_roles() -> list[ReviewRoleSpec]:
    return [
        _role("product", "产品经理", "general", "审查目标完整性和用户价值", ["目标", "用户价值"], ["目标是否明确"]),
        _role("user", "用户代表", "general", "审查理解成本和可用性", ["可理解性", "可用性"], ["用户是否能理解结果"]),
        _role("tester", "验收审查员", "general", "审查验收标准和边界", ["验收标准", "边界"], ["是否可验证"]),
        _role("domain_reviewer", "领域审查员", "domain", "审查任务领域内的合理性", ["领域合理性"], ["是否符合任务语境"]),
    ]


def build_adaptive_role_plan(
    artifacts: ReviewArtifacts,
    *,
    dynamic_roles_enabled: bool = True,
    dynamic_roles_max: int = 3,
    total_roles_max: int = 8,
) -> RolePlan:
    """Build a deterministic role plan from review artifacts."""

    task_kind = detect_task_kind(artifacts)
    total_roles_max = max(1, int(total_roles_max or 8))
    dynamic_roles_max = max(0, int(dynamic_roles_max or 0))

    if task_kind == "programming":
        roles = fixed_programming_roles()
        if dynamic_roles_enabled:
            slots = max(0, min(dynamic_roles_max, total_roles_max - len(roles)))
            roles.extend(_programming_dynamic_roles(artifacts)[:slots])
    elif task_kind == "writing":
        roles = _writing_roles()
    elif task_kind == "research":
        roles = _research_roles()
    elif task_kind == "design":
        roles = _design_roles()
    else:
        roles = _generic_roles()

    return RolePlan(task_kind=task_kind, roles=roles[:total_roles_max])


def batch_roles_by_dependencies(roles: list[ReviewRoleSpec]) -> list[list[ReviewRoleSpec]]:
    """Topologically batch roles so each batch can run concurrently."""

    if not roles:
        return []
    by_id = {role.role_id: role for role in roles}
    remaining = dict(by_id)
    completed: set[str] = set()
    batches: list[list[ReviewRoleSpec]] = []

    while remaining:
        ready = [
            role for role in roles
            if role.role_id in remaining
            and all(dep not in by_id or dep in completed for dep in role.depends_on)
        ]
        if not ready:
            logger.warning("[Spec] review role dependency cycle detected; running remaining roles concurrently")
            return [roles]
        batches.append(ready)
        for role in ready:
            completed.add(role.role_id)
            remaining.pop(role.role_id, None)
    return batches
