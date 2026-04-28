"""Review output parsing and LLM-based fallback parsing."""

import json
import logging
from typing import Callable, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from .constants import SPEC_UI_TEXT
from ..utils.errors import get_error_detail
from ..utils.llm import ChatOpenAICacheKey, get_cached_chat_openai
from ..utils.spec_utils import (
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
    parse_review_output_loose,
    parse_review_output_strict_tolerant,
)

logger = logging.getLogger(__name__)

_LLM_CACHE: dict[ChatOpenAICacheKey, ChatOpenAI] = {}


def _get_llm(settings, temperature: float) -> ChatOpenAI:
    return get_cached_chat_openai(settings, temperature, cache=_LLM_CACHE, llm_cls=ChatOpenAI)


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


def parse_review_with_llm(raw_text: str, settings) -> list[PerspectiveReview]:
    if not settings.ark_api_key or not settings.ark_model:
        return []
    if not raw_text or len(raw_text.strip()) < 10:
        return []

    prompt = f"""请从以下文本中提取四个视角的审查结果。

文本内容：
{raw_text[:3000]}

请严格按以下 JSON 格式输出（不要输出其他内容）：
[
  {{"perspective": "ARCHITECT", "verdict": "PASS或FAIL", "suggestions": ["建议1", "建议2"]}},
  {{"perspective": "PRODUCT", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "USER", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "TESTER", "verdict": "PASS或FAIL", "suggestions": []}}
]"""

    try:
        response = _get_llm(settings, 0.0).invoke(
            [
                SystemMessage(content="你是一个文本解析助手。从审查文本中提取结构化的审查结果，只输出JSON。"),
                HumanMessage(content=prompt),
            ]
        )
        return extract_reviews_from_llm_response(response.content)
    except Exception as e:
        logger.warning("[Spec] LLM 兜底审查解析失败: %s", get_error_detail(e))
        return []
