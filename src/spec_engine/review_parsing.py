"""Review output parsing and LLM-based fallback parsing."""

import json
import logging
from typing import Callable, Optional

from ..engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from .constants import SPEC_UI_TEXT
from .utils import (
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
)
from .utils import (
    parse_review_output_loose,
    parse_review_output_strict_tolerant,
)

logger = logging.getLogger(__name__)


def extract_reviews_from_llm_response(text: str) -> list[PerspectiveReview]:
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("["):
                cleaned = stripped
                break

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []

    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    reviews: list[PerspectiveReview] = []
    found: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("perspective", "")).upper()
        perspective = _PERSPECTIVE_TAG_MAP.get(tag)
        if not perspective or tag in found:
            continue
        found.add(tag)
        verdict = str(item.get("verdict", "")).upper()
        passed = verdict == "PASS"
        suggestions = item.get("suggestions", [])
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = [str(s) for s in suggestions if s]
        if passed:
            suggestions = []
        reviews.append(
            PerspectiveReview(
                perspective=perspective,
                passed=passed,
                suggestions=suggestions,
                summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
            )
        )
    return reviews


def parse_review_output(
    text: str,
    cycle: int,
    *,
    parse_with_llm_fn: Optional[Callable[[str], list[PerspectiveReview]]] = None,
) -> ReviewResult:
    raw = (text or "").replace("\r\n", "\n")

    reviews = parse_review_output_strict_tolerant(raw, cycle)

    if not reviews:
        reviews = parse_review_output_loose(raw, cycle)

    if not reviews and callable(parse_with_llm_fn):
        preview = raw[:500] if raw else "(empty)"
        logger.warning("[Spec] 正则+loose解析全部失败, 尝试LLM兜底解析. 原文预览: %s", preview)
        reviews = parse_with_llm_fn(raw)

    if not reviews:
        logger.warning("[Spec] 审查输出解析失败, 将视为有改进建议继续循环")
        for p in ReviewPerspective:
            reviews.append(
                PerspectiveReview(
                    perspective=p,
                    passed=False,
                    suggestions=[SPEC_UI_TEXT["review_parse_fail_system"]],
                    summary="解析失败",
                )
            )

    return ReviewResult(reviews=reviews, iteration=cycle)


def parse_review_with_llm(
    raw_text: str,
    settings,
    send_fn: Optional[Callable[[str], str]] = None,
) -> list[PerspectiveReview]:
    """Use the current ACP tool to extract structured review results from text.

    Parameters
    ----------
    raw_text:
        Free-form review output that failed regex parsing.
    settings:
        Application settings (kept for interface compat).
    send_fn:
        ``(prompt) -> response_text`` callable backed by an ACP sub-session.
        When *None*, returns ``[]``.
    """
    if not send_fn:
        return []
    if not raw_text or len(raw_text.strip()) < 10:
        return []

    prompt = f"""请从以下文本中提取五个视角的审查结果。

文本内容：
{raw_text[:3000]}

请严格按以下 JSON 格式输出（不要输出其他内容）：
[
  {{"perspective": "ARCHITECT", "verdict": "PASS或FAIL", "suggestions": ["建议1", "建议2"]}},
  {{"perspective": "PRODUCT", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "USER", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "TESTER", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "DESIGNER", "verdict": "PASS或FAIL", "suggestions": []}}
]

规则：
- perspective 只能是 ARCHITECT/PRODUCT/USER/TESTER/DESIGNER
- verdict 只能是 PASS 或 FAIL
- 如果文本中找不到某个视角的审查，verdict 设为 FAIL，suggestions 填 ["未找到该视角的审查结果"]
- suggestions 数组中只放 FAIL 视角的改进建议，PASS 视角为空数组"""

    try:
        response = send_fn(prompt)
        reviews = extract_reviews_from_llm_response(response)
        if {r.perspective for r in reviews} != set(ReviewPerspective):
            logger.warning("[Spec] ACP 兜底审查解析视角不完整, 将视为解析失败")
            return []
        return reviews
    except Exception as e:
        from ..utils.errors import get_error_detail
        logger.warning("[Spec] ACP 兜底审查解析失败: %s", get_error_detail(e))
        return []
