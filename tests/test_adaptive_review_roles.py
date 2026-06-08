from src.engine_base import ReviewPerspective
from src.spec_engine.review_artifacts import ReviewArtifacts
from src.spec_engine.review_roles import (
    COMPLETION_CONTROL_ROLE_ID,
    ReviewRoleSpec,
    batch_roles_by_dependencies,
    build_adaptive_role_plan,
    fixed_programming_roles,
)


def _artifacts(requirement: str, *, diff: str = "", files: list[str] | None = None) -> ReviewArtifacts:
    return ReviewArtifacts(
        cycle_number=1,
        requirement=requirement,
        cwd="/repo",
        diff_patch=diff,
        touched_files=files or [],
    )


def test_programming_tasks_keep_fixed_review_roles_and_add_relevant_dynamic_roles():
    artifacts = _artifacts(
        "实现登录 API，补充权限校验和单元测试",
        diff="diff --git a/src/api/auth.py b/src/api/auth.py\n+def login(): pass\n",
        files=["src/api/auth.py", "tests/test_auth.py"],
    )

    plan = build_adaptive_role_plan(
        artifacts,
        dynamic_roles_enabled=True,
        dynamic_roles_max=3,
        total_roles_max=8,
    )

    assert plan.task_kind == "programming"
    fixed_ids = [p.value for p in ReviewPerspective]
    assert [role.role_id for role in plan.roles[:5]] == fixed_ids
    assert all(role.blocking for role in plan.roles[:5])
    assert plan.roles[5].role_id == COMPLETION_CONTROL_ROLE_ID
    assert plan.roles[5].category == "completion_control"
    assert plan.roles[5].blocking is True
    assert any(role.role_id == "security_reviewer" for role in plan.roles)
    assert len(plan.roles) <= 8


def test_writing_tasks_generate_editorial_roles_without_software_defaults():
    artifacts = _artifacts("写一篇介绍 AI Agent 产品落地方法的公众号文章，需要配图建议和标题优化")

    plan = build_adaptive_role_plan(artifacts, dynamic_roles_enabled=True)

    role_ids = {role.role_id for role in plan.roles}
    assert plan.task_kind == "writing"
    assert "editor_in_chief" in role_ids
    assert "fact_checker" in role_ids
    assert "visual_designer" in role_ids
    assert "architect" not in role_ids
    assert COMPLETION_CONTROL_ROLE_ID in role_ids


def test_research_tasks_generate_source_verification_roles():
    artifacts = _artifacts("调研 2026 年企业 AI 编程工具市场，要求求证数据来源并给出反方观点")

    plan = build_adaptive_role_plan(artifacts, dynamic_roles_enabled=True)

    role_ids = {role.role_id for role in plan.roles}
    assert plan.task_kind == "research"
    assert "source_verifier" in role_ids
    assert "methodology_reviewer" in role_ids
    assert "opposing_view_reviewer" in role_ids
    assert COMPLETION_CONTROL_ROLE_ID in role_ids


def test_dynamic_role_caps_apply_after_fixed_programming_roles():
    artifacts = _artifacts(
        "实现移动端支付 API、权限校验、隐私合规、性能优化和文档",
        diff="diff --git a/src/mobile/pay.py b/src/mobile/pay.py\n+secret='x'\n",
        files=["src/mobile/pay.py", "docs/api.md"],
    )

    plan = build_adaptive_role_plan(
        artifacts,
        dynamic_roles_enabled=True,
        dynamic_roles_max=2,
        total_roles_max=7,
    )

    assert len(plan.roles) == 7
    assert len([role for role in plan.roles if role.category == "software"]) == 5
    assert len([role for role in plan.roles if role.category == "completion_control"]) == 1
    assert len([role for role in plan.roles if role.category not in {"software", "completion_control"}]) == 1


def test_completion_control_role_is_not_dropped_by_low_total_role_cap():
    artifacts = _artifacts(
        "实现移动端支付 API、权限校验、隐私合规、性能优化和文档",
        diff="diff --git a/src/mobile/pay.py b/src/mobile/pay.py\n+secret='x'\n",
        files=["src/mobile/pay.py", "docs/api.md"],
    )

    plan = build_adaptive_role_plan(
        artifacts,
        dynamic_roles_enabled=True,
        dynamic_roles_max=3,
        total_roles_max=5,
    )

    role_ids = [role.role_id for role in plan.roles]
    assert role_ids[:5] == [p.value for p in ReviewPerspective]
    assert role_ids[5] == COMPLETION_CONTROL_ROLE_ID
    assert all(role.role_id != "security_reviewer" for role in plan.roles)


def test_dependency_batches_are_parallel_by_default_and_layered_when_needed():
    roles = [
        ReviewRoleSpec(role_id="editor", display_name="主编", category="writing", mission="结构", review_focus=[], must_check=[], evidence_policy="required"),
        ReviewRoleSpec(role_id="fact_checker", display_name="事实核查", category="research", mission="事实", review_focus=[], must_check=[], evidence_policy="required"),
        ReviewRoleSpec(role_id="conclusion_editor", display_name="结论编辑", category="writing", mission="结论", review_focus=[], must_check=[], evidence_policy="required", depends_on=["fact_checker"]),
    ]

    batches = batch_roles_by_dependencies(roles)

    assert [[role.role_id for role in batch] for batch in batches] == [
        ["editor", "fact_checker"],
        ["conclusion_editor"],
    ]


def test_dependency_cycles_fall_back_to_single_parallel_batch():
    roles = [
        ReviewRoleSpec(role_id="a", display_name="A", category="x", mission="A", review_focus=[], must_check=[], evidence_policy="required", depends_on=["b"]),
        ReviewRoleSpec(role_id="b", display_name="B", category="x", mission="B", review_focus=[], must_check=[], evidence_policy="required", depends_on=["a"]),
    ]

    batches = batch_roles_by_dependencies(roles)

    assert [[role.role_id for role in batch] for batch in batches] == [["a", "b"]]


def test_fixed_programming_role_metadata_matches_current_perspectives():
    roles = fixed_programming_roles()

    assert [role.role_id for role in roles] == [p.value for p in ReviewPerspective]
    assert roles[0].display_name == ReviewPerspective.ARCHITECT.display_name
    assert roles[0].review_focus
    assert all(role.blocking for role in roles)
