"""Criteria decomposition and evaluation helpers for SpecEngine."""

import logging
import re
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..acp import ACPEvent, ACPEventType
from ..utils.errors import get_error_detail
from ..utils.llm import ChatOpenAICacheKey, get_cached_chat_openai
from ..utils.retry import RetryPolicy
from ..utils.spec_utils import CRITERIA_PATTERNS as _CRITERIA_PATTERNS
from .artifacts import extract_criteria_from_llm_response

logger = logging.getLogger(__name__)

_LLM_CACHE: dict[ChatOpenAICacheKey, ChatOpenAI] = {}


def _get_llm(settings, temperature: float) -> ChatOpenAI:
    return get_cached_chat_openai(settings, temperature, cache=_LLM_CACHE, llm_cls=ChatOpenAI)


def decompose_criteria_with_llm(text: str, settings) -> list[str]:
    if not settings.ark_api_key or not settings.ark_model:
        return []

    prompt = f"""请分析以下用户需求，提取并拆解为明确的验收标准。

用户需求（口语化描述）：
{text}

要求：
1. 先理解用户的核心诉求
2. 将需求拆解为 3-8 条具体、可验证的验收标准
3. 每条标准应该是独立可验证的（能明确判断 PASS/FAIL）
4. 标准应覆盖用户提到的所有功能点
5. 用简洁的技术语言描述，不要过于笼统

输出格式（严格按此格式，每行一条，以 "- " 开头）：
- 验收标准1
- 验收标准2
- 验收标准3
..."""

    try:
        response = _get_llm(settings, 0.1).invoke(
            [
                SystemMessage(content="你是一个需求分析助手，擅长将口语化的产品需求拆解为结构化的验收标准。"),
                HumanMessage(content=prompt),
            ]
        )
        return extract_criteria_from_llm_response(response.content)
    except Exception as e:
        logger.warning("[Spec] LLM 需求拆解失败: %s, 将使用原始文本", get_error_detail(e))
        return []


def evaluate_criteria(
    session,
    criteria: list[str],
    cycle: int,
    project,
    send_prompt_fn: Callable,
    settings,
) -> dict:
    if not session:
        return {"all_satisfied": False}

    criteria_list = "\n".join(f"CRITERIA_{i + 1}: {c}" for i, c in enumerate(criteria))
    eval_prompt = f"""请评估以下验收标准是否已满足：
{criteria_list}

对每个标准回答 PASS 或 FAIL，严格按照以下格式回复（每行一个）：
CRITERIA_1: PASS
CRITERIA_2: FAIL
...
"""
    try:
        eval_text: list[str] = []

        def on_eval_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                eval_text.append(event.text)

        send_prompt_fn(
            eval_prompt,
            on_event=on_eval_event,
            timeout=60,
            retry_policy=RetryPolicy(max_retries=1, retry_delay=2.0),
        )
        full_text = "".join(eval_text).upper()

        per_criteria: dict[int, bool] = {}
        for i in range(len(criteria)):
            pat = (
                _CRITERIA_PATTERNS[i]
                if i < len(_CRITERIA_PATTERNS)
                else re.compile(rf"CRITERIA_{i + 1}\s*:\s*(PASS|FAIL)")
            )
            match = pat.search(full_text)
            if match:
                per_criteria[i] = match.group(1) == "PASS"

        if project:
            project.criteria_tracker.batch_update(per_criteria, cycle)

        all_satisfied = project.criteria_tracker.is_all_satisfied if project else False
        return {"all_satisfied": all_satisfied}

    except Exception as e:
        logger.debug("[Spec] 验收标准评估失败: %s", get_error_detail(e))
        return {"all_satisfied": False}
