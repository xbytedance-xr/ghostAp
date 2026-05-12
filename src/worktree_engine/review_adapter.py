from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorktreeReviewRole:
    role_id: str
    display_name: str
    blocking: bool = True


@dataclass
class WorktreeReviewPlan:
    roles: list[WorktreeReviewRole] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roles": [
                {
                    "role_id": role.role_id,
                    "display_name": role.display_name,
                    "blocking": role.blocking,
                }
                for role in self.roles
            ]
        }


@dataclass
class WorktreeReviewOutcome:
    blockers: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blockers": list(self.blockers),
            "observations": list(self.observations),
        }


class WorktreeReviewAdapter:
    """Lightweight Spec-inspired review contract for one WT task."""

    def plan_roles(self, *, goal: str, changed_files: list[str]) -> WorktreeReviewPlan:
        roles = [
            WorktreeReviewRole("architect", "架构审查"),
            WorktreeReviewRole("tester", "测试审查"),
            WorktreeReviewRole("integration", "集成审查"),
            WorktreeReviewRole("product", "目标验收"),
        ]
        haystack = " ".join([goal, *changed_files]).lower()
        if any(token in haystack for token in ("auth", "token", "secret", "permission", "security")):
            roles.append(WorktreeReviewRole("security", "安全审查"))
        if any(path.endswith((".md", ".rst")) or "/docs/" in path for path in changed_files):
            roles.append(WorktreeReviewRole("docs", "文档审查", blocking=False))
        return WorktreeReviewPlan(roles=roles)

    def aggregate(self, findings: list[dict[str, Any]]) -> WorktreeReviewOutcome:
        blockers: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        for finding in findings:
            severity = str(finding.get("severity") or "").strip().lower()
            evidence = str(finding.get("evidence") or "").strip()
            if severity == "blocker" and evidence:
                blockers.append(finding)
            else:
                observations.append(finding)
        return WorktreeReviewOutcome(blockers=blockers, observations=observations)
