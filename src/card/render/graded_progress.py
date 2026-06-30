"""Graded completion progress card for Spec Engine.

Renders per-criterion scores, dimension breakdown, trend data,
and user intervention buttons as a Feishu interactive card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.card.themes import PANEL_STYLES

# ─── Progress bar constants ──────────────────────────────────────────────────
_BAR_LEN = 8
_FILLED = "\u25b0"  # ▰
_EMPTY = "\u25b1"  # ▱


class Grade(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PARTIAL = "PARTIAL"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"


_GRADE_BADGE: dict[Grade, str] = {
    Grade.NOT_STARTED: "\U0001f534",   # Red circle
    Grade.PARTIAL: "\U0001f7e1",       # Yellow circle
    Grade.IMPLEMENTED: "\U0001f7e2",   # Green circle
    Grade.VERIFIED: "\u2705",          # White check mark
}

_GRADE_LABEL: dict[Grade, str] = {
    Grade.NOT_STARTED: "NOT_STARTED",
    Grade.PARTIAL: "PARTIAL",
    Grade.IMPLEMENTED: "IMPLEMENTED",
    Grade.VERIFIED: "VERIFIED",
}


# ─── Data models ─────────────────────────────────────────────────────────────


@dataclass
class CriterionScore:
    name: str
    score: float  # 0.0 - 1.0
    grade: Grade
    confidence: float = 1.0  # 0.0 - 1.0
    verify_command: Optional[str] = None
    verify_passed: Optional[bool] = None
    last_verified_cycle: Optional[int] = None
    delta: float = 0.0  # change from prev cycle


@dataclass
class DimensionScore:
    name: str
    label: str  # display name (Chinese)
    score: float  # 0.0 - 1.0
    weight: float = 0.25


@dataclass
class GradedMetrics:
    composite_score: float  # 0.0 - 1.0
    cycle_number: int
    prev_composite: Optional[float] = None
    criteria: list[CriterionScore] = field(default_factory=list)
    dimensions: list[DimensionScore] = field(default_factory=list)
    stagnation_cycles: int = 0  # cycles without meaningful progress
    regression_alerts: list[str] = field(default_factory=list)


# ─── Action IDs (to be registered in dispatch.py) ────────────────────────────
SPEC_GRADED_CONFIRM = "spec_graded_confirm"
SPEC_GRADED_CONTINUE = "spec_graded_continue"
SPEC_GRADED_ADJUST = "spec_graded_adjust"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _mini_bar(score: float, *, length: int = _BAR_LEN, color: str = "blue") -> str:
    """Render compact progress bar: <font color='blue'>▰▰▰</font>▱▱▱▱▱"""
    pct = max(0.0, min(1.0, score))
    filled = round(pct * length)
    if pct > 0 and filled == 0:
        filled = 1
    f_str = _FILLED * filled
    e_str = _EMPTY * (length - filled)
    if filled > 0:
        return f"<font color='{color}'>{f_str}</font>{e_str}"
    return e_str


def _score_color(score: float) -> str:
    if score >= 0.8:
        return "green"
    if score >= 0.5:
        return "blue"
    if score >= 0.3:
        return "orange"
    return "red"


def _trend_arrow(current: float, previous: Optional[float]) -> str:
    if previous is None:
        return ""
    delta = current - previous
    if delta > 0.1:
        return " \u2191"   # Up
    if delta > 0.03:
        return " \u2197"   # Up-right
    if delta > -0.03:
        return " \u2192"   # Right (stable)
    if delta > -0.1:
        return " \u2198"   # Down-right
    return " \u2193"       # Down


def _status_text(metrics: GradedMetrics) -> str:
    if metrics.regression_alerts:
        return "\u26a0\ufe0f \u5b58\u5728\u56de\u9000"  # Warning: has regression
    if metrics.stagnation_cycles >= 3:
        return "\u26a0\ufe0f \u9700\u8981\u5173\u6ce8"  # Needs attention
    if metrics.composite_score >= 0.9:
        return "\u2705 \u8d8b\u4e8e\u6536\u655b"  # Converging
    if metrics.composite_score >= 0.5:
        return "\U0001f7e2 \u8fdb\u5c55\u826f\u597d"  # Good progress
    return "\U0001f535 \u6267\u884c\u4e2d"  # In progress


# ─── Card sections ───────────────────────────────────────────────────────────


def _render_summary_header(metrics: GradedMetrics) -> list[dict]:
    """Section 1: Composite score + trend + status at a glance."""
    pct = round(metrics.composite_score * 100)
    color = _score_color(metrics.composite_score)
    bar = _mini_bar(metrics.composite_score, length=12, color=color)
    arrow = _trend_arrow(metrics.composite_score, metrics.prev_composite)
    status = _status_text(metrics)

    header_line = f"**\u5b8c\u6210\u5ea6 {pct}%**{arrow}  \u00b7  Cycle {metrics.cycle_number}  \u00b7  {status}"
    return [
        {"tag": "markdown", "content": header_line},
        {"tag": "markdown", "content": bar, "text_size": "heading"},
    ]


def _render_dimensions(metrics: GradedMetrics) -> list[dict]:
    """Section 2: Per-dimension compact breakdown."""
    if not metrics.dimensions:
        return []

    lines: list[str] = []
    for dim in metrics.dimensions:
        color = _score_color(dim.score)
        bar = _mini_bar(dim.score, length=6, color=color)
        pct = round(dim.score * 100)
        warn = " \u26a0\ufe0f" if dim.score < 0.5 else ""
        lines.append(f"{bar} **{dim.label}** {pct}%{warn}")

    return [{"tag": "markdown", "content": "\n".join(lines)}]


def _render_criteria_panel(metrics: GradedMetrics) -> dict:
    """Section 3: Per-criterion detail as collapsible panel."""
    rows: list[str] = []
    for c in metrics.criteria:
        badge = _GRADE_BADGE.get(c.grade, "\u2b1c")
        score_pct = round(c.score * 100)
        conf_str = f" (conf:{round(c.confidence * 100)}%)" if c.confidence < 1.0 else ""

        parts = [f"{badge} **{c.name}** {score_pct}%{conf_str}"]

        if c.verify_command:
            v_icon = "\u2705" if c.verify_passed else ("\u274c" if c.verify_passed is False else "\u2b1c")
            parts.append(f"  {v_icon} `{c.verify_command}`")

        if c.last_verified_cycle is not None:
            parts.append(f"  _cycle {c.last_verified_cycle}_")

        if abs(c.delta) > 0.01:
            d_arrow = "\u2191" if c.delta > 0 else "\u2193"
            parts.append(f"  {d_arrow}{round(abs(c.delta) * 100)}%")

        rows.append("".join(parts))

    body = "\n".join(rows) if rows else "_\u65e0\u8bc4\u5206\u6807\u51c6_"

    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "markdown",
                "content": f"\U0001f4cb **\u8bc4\u5206\u660e\u7ec6** \u00b7 {len(metrics.criteria)} \u9879",
            },
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": body, "text_align": "left"}],
    }


def _render_trend_alerts(metrics: GradedMetrics) -> list[dict]:
    """Section 4: Trend delta and regression/stagnation warnings."""
    elements: list[dict] = []

    if metrics.prev_composite is not None:
        delta = metrics.composite_score - metrics.prev_composite
        sign = "+" if delta >= 0 else ""
        elements.append({
            "tag": "markdown",
            "content": f"_\u0394 {sign}{round(delta * 100)}% vs prev cycle_",
            "text_size": "notation",
        })

    if metrics.regression_alerts:
        alert_text = "\n".join(f"\u26a0\ufe0f {a}" for a in metrics.regression_alerts)
        elements.append({
            "tag": "column_set",
            "flex_mode": "stretch",
            "background_style": "orange",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [{"tag": "markdown", "content": alert_text}],
            }],
        })

    if metrics.stagnation_cycles >= 3:
        elements.append({
            "tag": "markdown",
            "content": f"\u26a0\ufe0f \u5df2\u8fde\u7eed {metrics.stagnation_cycles} \u8f6e\u65e0\u663e\u8457\u8fdb\u5c55",
            "text_size": "notation",
        })

    return elements


def _render_action_buttons(
    *,
    project_id: Optional[str] = None,
    thread_root_id: str = "",
) -> list[dict]:
    """Section 5: User intervention buttons."""

    def _btn(text: str, action: str, *, primary: bool = False) -> dict:
        value = {"action": action}
        if project_id:
            value["project_id"] = project_id
        if thread_root_id:
            value["thread_root_id"] = thread_root_id
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": "primary" if primary else "default",
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }

    return [{
        "tag": "action",
        "actions": [
            _btn("\u786e\u8ba4\u5b8c\u6210", SPEC_GRADED_CONFIRM, primary=True),
            _btn("\u7ee7\u7eed\u4f18\u5316", SPEC_GRADED_CONTINUE),
            _btn("\u8c03\u6574\u6743\u91cd", SPEC_GRADED_ADJUST),
        ],
    }]


# ─── Main render function ────────────────────────────────────────────────────


def render_graded_progress_card(
    metrics: GradedMetrics,
    *,
    project_id: Optional[str] = None,
    thread_root_id: str = "",
    show_actions: bool = True,
) -> dict:
    """Render graded completion progress as a full Feishu interactive card.

    Args:
        metrics: Graded completion metrics for the current cycle.
        project_id: GhostAP project identifier (for action routing).
        thread_root_id: Feishu thread root message ID.
        show_actions: Whether to include user intervention buttons.

    Returns:
        A dict representing the complete Feishu card JSON payload.
    """
    # Determine header color by overall score
    if metrics.composite_score >= 0.9:
        template = "green"
    elif metrics.composite_score >= 0.5:
        template = "blue"
    elif metrics.regression_alerts:
        template = "red"
    else:
        template = "turquoise"

    # Assemble elements
    elements: list[dict] = []
    elements.extend(_render_summary_header(metrics))
    elements.append({"tag": "hr"})
    elements.extend(_render_dimensions(metrics))
    elements.append(_render_criteria_panel(metrics))

    trend_alerts = _render_trend_alerts(metrics)
    if trend_alerts:
        elements.append({"tag": "hr"})
        elements.extend(trend_alerts)

    if show_actions:
        elements.append({"tag": "hr"})
        elements.extend(_render_action_buttons(
            project_id=project_id,
            thread_root_id=thread_root_id,
        ))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Spec Engine \u00b7 \u5b8c\u6210\u5ea6\u8bc4\u4f30"},
            "template": template,
        },
        "elements": elements,
    }


# ─── Example renders ─────────────────────────────────────────────────────────


def example_mid_progress() -> dict:
    """Example: mid-progress state (cycle 3, ~58% complete)."""
    metrics = GradedMetrics(
        composite_score=0.58,
        cycle_number=3,
        prev_composite=0.42,
        dimensions=[
            DimensionScore("functional_completeness", "\u529f\u80fd\u5b8c\u6574\u6027", 0.7, 0.4),
            DimensionScore("implementation_quality", "\u5b9e\u73b0\u8d28\u91cf", 0.6, 0.25),
            DimensionScore("verification_confidence", "\u9a8c\u8bc1\u53ef\u4fe1\u5ea6", 0.35, 0.2),
            DimensionScore("goal_alignment", "\u76ee\u6807\u5bf9\u9f50", 0.65, 0.15),
        ],
        criteria=[
            CriterionScore("API \u7aef\u70b9\u5b9e\u73b0", 0.8, Grade.IMPLEMENTED, delta=0.3),
            CriterionScore("\u5355\u5143\u6d4b\u8bd5\u8986\u76d6", 0.4, Grade.PARTIAL, confidence=0.7,
                           verify_command="pytest tests/ -q", verify_passed=False,
                           last_verified_cycle=3, delta=0.1),
            CriterionScore("\u9519\u8bef\u5904\u7406", 0.6, Grade.PARTIAL, delta=0.2),
            CriterionScore("\u6587\u6863\u66f4\u65b0", 0.0, Grade.NOT_STARTED),
            CriterionScore("\u6027\u80fd\u57fa\u51c6", 0.3, Grade.PARTIAL,
                           verify_command="bench run --ci", verify_passed=None,
                           delta=-0.05),
        ],
        stagnation_cycles=0,
        regression_alerts=[],
    )
    return render_graded_progress_card(metrics, project_id="proj_demo", thread_root_id="om_abc123")


def example_near_completion() -> dict:
    """Example: near-completion with one regression (cycle 7, ~91%)."""
    metrics = GradedMetrics(
        composite_score=0.91,
        cycle_number=7,
        prev_composite=0.93,
        dimensions=[
            DimensionScore("functional_completeness", "\u529f\u80fd\u5b8c\u6574\u6027", 0.95, 0.4),
            DimensionScore("implementation_quality", "\u5b9e\u73b0\u8d28\u91cf", 0.90, 0.25),
            DimensionScore("verification_confidence", "\u9a8c\u8bc1\u53ef\u4fe1\u5ea6", 0.85, 0.2),
            DimensionScore("goal_alignment", "\u76ee\u6807\u5bf9\u9f50", 0.92, 0.15),
        ],
        criteria=[
            CriterionScore("API \u7aef\u70b9\u5b9e\u73b0", 1.0, Grade.VERIFIED,
                           verify_command="pytest tests/api/ -q", verify_passed=True,
                           last_verified_cycle=7),
            CriterionScore("\u5355\u5143\u6d4b\u8bd5\u8986\u76d6", 0.92, Grade.IMPLEMENTED,
                           confidence=0.9, verify_command="pytest tests/ -q",
                           verify_passed=True, last_verified_cycle=7, delta=0.02),
            CriterionScore("\u9519\u8bef\u5904\u7406", 0.95, Grade.VERIFIED, delta=0.0),
            CriterionScore("\u6587\u6863\u66f4\u65b0", 0.85, Grade.IMPLEMENTED, delta=0.1),
            CriterionScore("\u6027\u80fd\u57fa\u51c6", 0.78, Grade.IMPLEMENTED,
                           verify_command="bench run --ci", verify_passed=True,
                           last_verified_cycle=6, delta=-0.08),
        ],
        stagnation_cycles=0,
        regression_alerts=["\u6027\u80fd\u57fa\u51c6 \u4e0b\u964d 8% (0.86\u21920.78)\uff0c\u53ef\u80fd\u56e0\u65b0\u589e\u4e2d\u95f4\u4ef6\u5f15\u5165\u5ef6\u8fdf"],
    )
    return render_graded_progress_card(metrics, project_id="proj_demo", thread_root_id="om_xyz789")
