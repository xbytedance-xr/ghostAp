"""Display helpers for Spec review results."""

from __future__ import annotations

from src.engine_base import PerspectiveReview, ReviewResult


_ROLE_STYLES: tuple[tuple[str, str], ...] = (
    ("wathet", "wathet"),
    ("green", "green"),
    ("yellow", "orange"),
    ("blue", "blue"),
    ("grey", "grey"),
    ("orange", "orange"),
)


def build_review_role_payloads(review: ReviewResult, cycle_num: int) -> list[dict]:
    """Convert a ReviewResult into card role-panel payloads."""
    roles: list[dict] = []
    for index, pr in enumerate(review.reviews, start=1):
        background_style, border_color = _ROLE_STYLES[(index - 1) % len(_ROLE_STYLES)]
        title = pr.role_display_name or pr.perspective.display_name
        status_text = "✅ PASS" if pr.passed else pr.perspective.failure_label
        suggestions = [str(item).strip() for item in pr.suggestions if str(item).strip()]
        roles.append({
            "cycle_num": cycle_num,
            "role_index": index,
            "role_id": pr.role_id or pr.perspective.value,
            "title": title,
            "emoji": pr.perspective.emoji,
            "status_text": status_text,
            "passed": pr.passed,
            "suggestions": suggestions,
            "summary": str(pr.summary or "").strip(),
            "agent_detail": _review_agent_detail(pr),
            "blocking": bool(pr.blocking),
            "background_style": background_style,
            "border_color": border_color,
            "expanded": bool(suggestions) or not pr.passed,
        })
    return roles


def format_review_overview(review: ReviewResult, cycle_num: int) -> str:
    """Short review overview; detailed suggestions live in role panels."""
    total = len(review.reviews)
    passed = sum(1 for item in review.reviews if item.passed)
    suggestion_count = review.total_suggestions
    lines = [f"🔍 **多角色审查 [循环 {cycle_num}]**", f"{passed}/{total} 个角色通过"]
    if suggestion_count:
        lines.append(f"💡 **改进建议: {suggestion_count} 条** → 将驱动下一轮 Spec 循环")
    else:
        lines.append("✅ **所有角色均通过，无改进建议**")
    return "\n\n".join(lines)


def _review_agent_detail(review: PerspectiveReview) -> str:
    label = str(getattr(review, "review_agent_label", "") or "").strip()
    if label:
        return label
    agent = str(getattr(review, "review_agent_type", "") or "").strip()
    model = str(getattr(review, "review_model_name", "") or "").strip()
    if not agent and not model:
        return ""
    if not agent:
        return model
    if not model:
        return agent
    return f"{agent} / {model}"
