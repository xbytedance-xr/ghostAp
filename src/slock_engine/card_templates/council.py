"""Slock Council card templates.

Migrated from card_templates_legacy.py — council-related card builders.
"""

from __future__ import annotations

from .common import (
    COUNCIL_STATUS_LABEL_ZH,
    build_card_wrapper,
    build_collapsible_panel,
    redact_sensitive,
)
from ..models import CouncilRun, CouncilStatus

__all__ = [
    "build_council_card",
    "build_council_expandable_card",
    "build_council_detail_card",
    "build_council_result_card",
]


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _build_council_stage_block(index: str, title: str, content: str) -> dict:
    return {
        "tag": "markdown",
        "content": f"**阶段 {index} — {title}**\n{content or '*等待中*'}",
    }


def _format_council_responses(run: CouncilRun) -> str:
    if not run.responses:
        return "*等待 Agent 独立作答*"
    lines: list[str] = []
    for response in run.responses[:6]:
        content = response.content or response.error or "(空)"
        lines.append(
            f"• **{response.label}** · {response.agent_name or response.agent_id[:8]}: "
            f"{redact_sensitive(content)[:240]}"
        )
    return "\n".join(lines)


def _format_council_reviews(run: CouncilRun) -> str:
    if run.aggregate_rankings:
        return "\n".join(
            f"• #{idx + 1} **{item.label}** · {item.agent_name or item.agent_id[:8]} "
            f"(avg {item.average_rank:.2f}, score {item.quality_score:.1f})"
            for idx, item in enumerate(run.aggregate_rankings[:6])
        )
    if run.reviews:
        return "\n".join(
            f"• {review.reviewer_name or review.reviewer_agent_id[:8]}: "
            f"{', '.join(review.parsed_ranking) or '未解析'}"
            for review in run.reviews[:6]
        )
    return "*等待匿名互评*"


def _format_council_final(run: CouncilRun) -> str:
    if run.final_response:
        return redact_sensitive(run.final_response)[:1200]
    return "*等待主席综合*"


# ------------------------------------------------------------------
# Public card builders
# ------------------------------------------------------------------


def build_council_card(run: CouncilRun, *, channel_id: str = "") -> dict:
    """Build a staged Slock Council card."""
    status_label = COUNCIL_STATUS_LABEL_ZH.get(run.status, run.status.value)
    header_template = (
        "green" if run.status == CouncilStatus.COMPLETED
        else "red" if run.status == CouncilStatus.FAILED
        else "indigo"
    )
    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": f"**议题:** {redact_sensitive(run.question)[:300]}",
        }
    ]

    if run.error:
        elements.append({"tag": "markdown", "content": f"**错误:** {redact_sensitive(run.error)[:500]}"})

    elements.append({"tag": "hr"})
    elements.append(_build_council_stage_block("1", "独立意见", _format_council_responses(run)))
    elements.append(_build_council_stage_block("2", "匿名互评", _format_council_reviews(run)))
    elements.append(_build_council_stage_block("3", "主席综合", _format_council_final(run)))

    elements.append({
        "tag": "markdown",
        "content": f"`council: {run.run_id[:12]}...` · {status_label}",
        "text_size": "notation",
    })

    return build_card_wrapper(
        header_title=f"\U0001f9ed Slock Council \u2014 {status_label}",
        header_template=header_template,
        elements=elements,
        mobile_optimize=True,
    )


def build_council_expandable_card(run: CouncilRun, *, channel_id: str = "") -> dict:
    """Build a compact council card with collapsible panels for each stage.

    Unlike build_council_card which always shows all stages expanded,
    this variant uses collapsible_panel elements -- only the latest active
    stage is expanded by default, keeping the card compact in chat.
    """
    status_label = COUNCIL_STATUS_LABEL_ZH.get(run.status, run.status.value)
    header_template = (
        "green" if run.status == CouncilStatus.COMPLETED
        else "red" if run.status == CouncilStatus.FAILED
        else "indigo"
    )

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": f"**议题:** {redact_sensitive(run.question)[:300]}",
        }
    ]

    if run.error:
        elements.append({"tag": "markdown", "content": f"**错误:** {redact_sensitive(run.error)[:500]}"})

    elements.append({"tag": "hr"})

    # Determine which stage to expand (latest non-empty stage)
    stage_data = [
        ("1", "独立意见", _format_council_responses(run)),
        ("2", "匿名互评", _format_council_reviews(run)),
        ("3", "主席综合", _format_council_final(run)),
    ]
    last_active_idx = 0
    for idx, (_, _, content) in enumerate(stage_data):
        if content and not content.startswith("*等待"):
            last_active_idx = idx

    for idx, (stage_num, title, content) in enumerate(stage_data):
        elements.append(
            build_collapsible_panel(
                title=f"**阶段 {stage_num} \u2014 {title}**",
                elements=[
                    {"tag": "markdown", "content": content or "*等待中*"},
                ],
                expanded=idx == last_active_idx,
            )
        )

    elements.append({
        "tag": "markdown",
        "content": f"`council: {run.run_id[:12]}...` · {status_label}",
        "text_size": "notation",
    })

    return build_card_wrapper(
        header_title=f"\U0001f9ed Slock Council \u2014 {status_label}",
        header_template=header_template,
        elements=elements,
        mobile_optimize=True,
    )


def build_council_detail_card(
    topic: str,
    opinions: list[dict],
    *,
    final_summary: str = "",
    channel_id: str = "",
    scores: list[dict] | None = None,
) -> dict:
    """Build an expanded council review detail card with collapsible opinions.

    Args:
        topic: The council review topic.
        opinions: List of dicts with keys: agent_name, emoji, role, opinion_text.
        final_summary: Optional synthesis summary.
        scores: Optional list of dicts with keys: agent_name, score (for ranking).
    """
    elements: list[dict] = []

    # Topic
    elements.append({"tag": "markdown", "content": f"**议题：** {topic}"})
    elements.append({"tag": "hr"})

    # Score ranking summary (if provided)
    if scores:
        sorted_scores = sorted(scores, key=lambda x: x.get("score", 0), reverse=True)
        ranking_lines = ["**\U0001f4ca 评分排名**"]
        for idx, s in enumerate(sorted_scores):
            medal = ["\U0001f947", "\U0001f948", "\U0001f949"][idx] if idx < 3 else f"{idx + 1}."
            ranking_lines.append(f"{medal} {s.get('agent_name', 'Agent')} \u2014 {s.get('score', 0)}分")
        elements.append({"tag": "markdown", "content": "\n".join(ranking_lines)})
        elements.append({"tag": "hr"})

    # Each opinion in a collapsible panel
    for idx, opinion in enumerate(opinions):
        agent_name = opinion.get("agent_name", "Agent")
        emoji = opinion.get("emoji", "\U0001f916")
        role = opinion.get("role", "")
        opinion_text = opinion.get("opinion_text", "")

        elements.append(
            build_collapsible_panel(
                title=f"{emoji} **{agent_name}** ({role})",
                elements=[
                    {"tag": "markdown", "content": opinion_text[:2000] if opinion_text else "(无回答)"},
                ],
                expanded=idx == 0,
            )
        )

    # Final summary section
    if final_summary:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**\U0001f4dd 综合评估**\n{final_summary}",
        })

    return build_card_wrapper(
        header_title="\U0001f3db\ufe0f Council 评审详情",
        header_template="purple",
        elements=elements,
        mobile_optimize=True,
    )


def build_council_result_card(
    question: str,
    agents_answers: list[dict],
    rankings: list[dict],
    *,
    channel_id: str = "",
) -> dict:
    """Build a council result card with collapsible agent answers and rankings.

    Args:
        question: The original question posed to the council.
        agents_answers: List of dicts with keys: agent_name, answer, score.
        rankings: List of dicts with keys: rank, agent_name, score.
        channel_id: Optional channel identifier.
    """
    # Build ranking section
    ranking_lines = []
    for r in rankings:
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(r.get("rank", 0), f"#{r.get('rank', '?')}")
        ranking_lines.append(f"{medal} **{r.get('agent_name', '?')}** \u2014 {r.get('score', 0):.1f}\u5206")

    ranking_text = "\n".join(ranking_lines) if ranking_lines else "\u65e0\u8bc4\u5206\u6570\u636e"

    # Build collapsible agent answer elements
    _rank_by_name: dict[str, int] = {r.get("agent_name", ""): r.get("rank", 0) for r in rankings}
    agent_elements = []
    for idx, ans in enumerate(agents_answers, 1):
        agent_name = ans.get("agent_name", "?")
        score = ans.get("score", 0)
        rank = _rank_by_name.get(agent_name, idx)
        ordinal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(rank, f"#{rank}")
        response_text = (ans.get("answer", "") or "\u65e0\u56de\u7b54")[:2000]
        agent_elements.append(
            build_collapsible_panel(
                title=f"\U0001f4dd **{agent_name}** (\u8bc4\u5206: {score:.1f}/10)",
                elements=[
                    {"tag": "markdown", "content": response_text},
                ],
                expanded=False,
            )
        )

    card_elements: list[dict] = [
        {
            "tag": "markdown",
            "content": f"**\u95ee\u9898\uff1a** {question[:200]}",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"**\u8bc4\u5206\u6392\u540d**\n{ranking_text}",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": "**\u5404 Agent \u539f\u59cb\u56de\u7b54** (\u70b9\u51fb\u5c55\u5f00)",
        },
        *agent_elements,
    ]

    return build_card_wrapper(
        header_title="\U0001f4cb Council \u8bc4\u5ba1\u7ed3\u679c",
        header_template="blue",
        elements=card_elements,
        mobile_optimize=True,
    )
