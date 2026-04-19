"""Review diagnostics, parsing and formatting helpers for SpecEngine."""

import json
import logging
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..acp import ACPEvent, ACPEventType
from ..engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from ..utils.llm import ChatOpenAICacheKey, get_cached_chat_openai
from ..utils.retry import RetryPolicy
from ..utils.spec_utils import (
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
    parse_review_output_loose,
    parse_review_output_strict_tolerant,
)
from .artifacts import safe_str

logger = logging.getLogger(__name__)

_LLM_CACHE: dict[ChatOpenAICacheKey, ChatOpenAI] = {}


def _get_llm(settings, temperature: float) -> ChatOpenAI:
    return get_cached_chat_openai(settings, temperature, cache=_LLM_CACHE, llm_cls=ChatOpenAI)

REVIEW_DIAG_STABLE_KEYS = (
    "phase",
    "role",
    "cycle",
    "decision",
    "fail_reason",
    "err_type",
    "err_repr",
    "error_text",
    "traceback_snippet",
)

REVIEW_DIAG_COMPAT_KEYS = (
    "cycle_number",
    "exception_type",
    "review_role",
)

REVIEW_EXCEPTION_LOG_FIELDS = (
    "phase",
    "role",
    "cycle",
    "decision",
    "fail_reason",
    "err_type",
    "err_repr",
    "error_text",
    "diag_json",
)


def build_review_exception_diagnostics(
    e: Exception,
    *,
    cycle: int,
    project_name: str = "",
    chat_id: str = "",
    root_path: str = "",
    agent_type: str = "",
    session_id: str = "",
    get_settings_fn=None,
) -> dict:
    from ..config import get_settings

    try:
        from ..acp.diagnostics import get_diagnostics_config, redact_text

        cfg = get_diagnostics_config(get_settings_fn=get_settings_fn or get_settings)
        redact_enabled = cfg.redact_enabled
        redact_patterns = cfg.redact_patterns or []
        redact_repl = cfg.redact_replacement or "***REDACTED***"
        cfg_snip = cfg.snippet_limit
        cfg_total = cfg.total_limit
    except Exception:
        redact_text = None  # type: ignore[assignment]
        redact_enabled, redact_patterns, redact_repl = True, [], "***REDACTED***"
        cfg_snip, cfg_total = 240, 2000

    def _truncate_strict(s: str, lim: int) -> str:
        try:
            lim = int(lim or 0)
        except Exception:
            lim = 0
        if lim <= 0:
            return ""
        ss = safe_str(s)
        if not ss:
            return ""
        if len(ss) <= lim:
            return ss
        suffix = "…(truncated)"
        if lim <= len(suffix):
            return ss[:lim]
        return ss[: max(0, lim - len(suffix))] + suffix

    def _redact_and_truncate(text: str, *, hard_limit: int, cfg_limit: int) -> str:
        lim = hard_limit
        try:
            lim = int(hard_limit or 0)
        except Exception:
            lim = 0
        lim = max(1, lim)
        try:
            cfg_lim = int(cfg_limit or 0)
        except Exception:
            cfg_lim = 0
        if cfg_lim > 0:
            lim = min(lim, cfg_lim)

        s = safe_str(text)
        if redact_enabled and callable(redact_text):
            try:
                s = redact_text(s, redact_patterns, redact_repl)  # type: ignore[misc]
            except Exception:
                pass
        return _truncate_strict(s, lim)

    def _extract_error_text(err: Exception) -> str:
        base = (safe_str(err) or "").strip()
        if base:
            return base
        for k in ("stderr_snippet", "stdout_snippet", "stderr", "stdout", "message", "detail"):
            try:
                v = (safe_str(getattr(err, k, "")) or "").strip()
                if v:
                    return v
            except Exception:
                continue
        return ""

    def _infer_fail_reason(err: Exception) -> str:
        et = "Exception"
        try:
            et = type(err).__name__
        except Exception:
            et = "Exception"
        if isinstance(err, TimeoutError):
            return "timeout"
        if et in ("TimeoutExpired", "ReadTimeout", "ConnectTimeout"):
            return "timeout"
        if et in ("JSONDecodeError",):
            return "parse_json"
        if et in ("ValueError", "TypeError"):
            return "parse_error"
        return "exception"

    def _extract_err_repr(err: Exception) -> str:
        err_type = "Exception"
        try:
            err_type = type(err).__name__
        except Exception:
            err_type = "Exception"
        try:
            s = repr(err)
        except Exception:
            s = ""
        s = (safe_str(s) or "").strip()
        if not s:
            s = f"<{err_type}>"
        return s

    err_repr = _extract_err_repr(e)
    err_type = "Exception"
    try:
        err_type = type(e).__name__
    except Exception:
        err_type = "Exception"

    error_text = _extract_error_text(e)
    if not (error_text or "").strip():
        error_text = f"{err_type} (empty message)"

    fail_reason = _infer_fail_reason(e)

    # 对 timeout 类型异常，使用中文友好文案替代技术性 "empty message"
    if fail_reason == "timeout" and ("(empty message)" in error_text or not _extract_error_text(e).strip()):
        error_text = "审查超时，将在下一轮重试"
    tb = ""
    try:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    except Exception:
        tb = ""
    tb = (tb or "").strip()

    err_repr_rt = _redact_and_truncate(err_repr, hard_limit=600, cfg_limit=cfg_snip)
    if not (err_repr_rt or "").strip():
        err_repr_rt = f"<{err_type}>"
    error_text_rt = _redact_and_truncate(error_text, hard_limit=600, cfg_limit=cfg_snip)
    if not (error_text_rt or "").strip():
        error_text_rt = err_repr_rt

    diag = {
        "phase": "review",
        "role": "multi_perspective",
        "cycle": int(cycle or 0),
        "decision": "review_failed_continue",
        "fail_reason": str(fail_reason or "exception"),
        "err_type": err_type,
        "err_repr": err_repr_rt,
        "error_text": error_text_rt,
        "cycle_number": int(cycle or 0),
        "exception_type": err_type,
        "review_role": "multi_perspective",
        "traceback_snippet": _redact_and_truncate(tb, hard_limit=1600, cfg_limit=cfg_total),
        "project": (project_name or "").strip(),
        "chat_id": (chat_id or ""),
        "root_path": (root_path or ""),
        "agent_type": (agent_type or ""),
    }
    diag["session_id"] = str(session_id or "")

    return diag


def normalize_review_diagnostics(diag: object) -> dict:
    d = diag if isinstance(diag, dict) else {}

    def _s(x: object) -> str:
        try:
            return str(x) if x is not None else ""
        except Exception:
            try:
                return repr(x)
            except Exception:
                return ""

    phase = (_s(d.get("phase")) or "review").strip() or "review"
    role = (_s(d.get("role")) or _s(d.get("review_role")) or "multi_perspective").strip() or "multi_perspective"

    cycle_val: int = 0
    try:
        if "cycle" in d and d.get("cycle") is not None:
            cycle_val = int(d.get("cycle") or 0)
        else:
            cycle_val = int(d.get("cycle_number") or 0)
    except Exception:
        cycle_val = 0

    decision = (_s(d.get("decision")) or "review_failed_continue").strip() or "review_failed_continue"

    fail_reason = (_s(d.get("fail_reason")) or "").strip()
    if not fail_reason:
        fail_reason = "exception" if decision.startswith("review_failed") else ""

    err_type = (_s(d.get("err_type")) or _s(d.get("exception_type")) or "Exception").strip() or "Exception"

    err_repr = (_s(d.get("err_repr")) or "").strip()
    if not err_repr:
        err_repr = f"<{err_type}>"

    error_text = (_s(d.get("error_text")) or "").strip()
    if not error_text:
        error_text = err_repr

    tb = (_s(d.get("traceback_snippet")) or "").strip()

    out = {
        "phase": phase,
        "role": role,
        "cycle": int(cycle_val),
        "decision": decision,
        "fail_reason": fail_reason,
        "err_type": err_type,
        "err_repr": err_repr,
        "error_text": error_text,
        "traceback_snippet": tb,
    }

    try:
        return {k: out.get(k) for k in REVIEW_DIAG_STABLE_KEYS}
    except Exception:
        return out


def format_review_exception_log_line(diag: dict, *, diag_json: str) -> str:
    d = normalize_review_diagnostics(diag)

    def _s(x: object) -> str:
        try:
            return str(x) if x is not None else ""
        except Exception:
            try:
                return repr(x)
            except Exception:
                return ""

    phase = (_s(d.get("phase")) or "review").strip() or "review"
    role = (_s(d.get("role")) or "multi_perspective").strip() or "multi_perspective"
    decision = (_s(d.get("decision")) or "review_failed_continue").strip() or "review_failed_continue"
    fail_reason = (_s(d.get("fail_reason")) or "").strip()
    err_type = (_s(d.get("err_type")) or "Exception").strip() or "Exception"
    err_repr = (_s(d.get("err_repr")) or "").strip() or f"<{err_type}>"
    error_text = (_s(d.get("error_text")) or "").strip() or err_repr

    cycle_val = 0
    try:
        cycle_val = int(d.get("cycle") or 0)
    except Exception:
        cycle_val = 0

    dj = _s(diag_json)
    try:
        if len(dj) > 2400:
            dj = dj[:2400] + "…(truncated)"
    except Exception:
        pass

    return (
        f"[Spec] review_exception: phase={phase} role={role} cycle={cycle_val} decision={decision} fail_reason={fail_reason} "
        f"err_type={err_type} err_repr={err_repr} error_text={error_text} diag={dj}, 将继续循环"
    )


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


def review_result_to_text(review: ReviewResult) -> str:
    if not review:
        return ""
    lines: list[str] = []
    for pr in review.reviews:
        verdict = "PASS" if pr.passed else "FAIL"
        lines.append(f"[{pr.perspective.name}] {verdict}")
        for s in pr.suggestions:
            lines.append(f"- {s}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


@dataclass
class ReviewCircuitState:
    last_review_failure_diag: Optional[dict] = None
    review_failure_consecutive: int = 0
    review_circuit_open_until_cycle: int = 0


def conduct_review(
    *,
    session,
    settings,
    project,
    send_prompt_with_retry_fn: Callable,
    build_review_exception_diagnostics_fn: Callable[..., dict],
    circuit: ReviewCircuitState,
    cycle: int,
    on_review_done: Optional[Callable] = None,
) -> ReviewResult:
    from .prompts import build_review_prompt

    enabled = settings.spec_review_failure_circuit_enabled
    max_consecutive = max(1, settings.spec_review_failure_max_consecutive)
    cooldown_cycles = max(0, settings.spec_review_failure_cooldown_cycles)

    if (
        enabled
        and int(circuit.review_circuit_open_until_cycle or 0)
        and int(cycle or 0) <= int(circuit.review_circuit_open_until_cycle or 0)
    ):
        diag_raw = {
            "phase": "review",
            "role": "multi_perspective",
            "cycle": int(cycle or 0),
            "decision": "review_circuit_open_skip",
            "fail_reason": "circuit_open",
            "err_type": "ReviewCircuitOpen",
            "err_repr": "<ReviewCircuitOpen>",
            "error_text": "review_circuit_open",
            "cycle_number": int(cycle or 0),
            "exception_type": "ReviewCircuitOpen",
            "review_role": "multi_perspective",
            "traceback_snippet": "",
            "consecutive_failures": int(circuit.review_failure_consecutive or 0),
            "open_until_cycle": int(circuit.review_circuit_open_until_cycle or 0),
        }

        diag = normalize_review_diagnostics(diag_raw)
        circuit.last_review_failure_diag = dict(diag)
        logger.warning(
            "[Spec] review_circuit_open: phase=review role=multi_perspective cycle=%s decision=review_circuit_open_skip open_until=%s consecutive=%s, 将跳过本轮审查",
            diag_raw.get("cycle_number"),
            diag_raw.get("open_until_cycle"),
            diag_raw.get("consecutive_failures"),
        )
        review_result = ReviewResult(
            reviews=[
                PerspectiveReview(
                    perspective=p,
                    passed=False,
                    suggestions=[f"审查熔断：连续{int(circuit.review_failure_consecutive or 0)}次异常，跳过本轮审查"],
                    summary="熔断",
                )
                for p in ReviewPerspective
            ],
            iteration=cycle,
        )
        if on_review_done:
            on_review_done(cycle, review_result)
        return review_result

    if not session:
        review_result = ReviewResult(iteration=cycle)
        if on_review_done:
            on_review_done(cycle, review_result)
        return review_result

    review_prompt = build_review_prompt(project.requirement if project else "")
    review_text: list[str] = []
    thought_text: list[str] = []

    def on_review_event(event: ACPEvent):
        if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
            review_text.append(event.text)
        elif event.event_type == ACPEventType.THOUGHT_CHUNK and event.text:
            thought_text.append(event.text)

    circuit.last_review_failure_diag = None

    try:
        review_timeout = getattr(settings, "spec_review_timeout", 120)
        send_prompt_with_retry_fn(
            review_prompt,
            on_event=on_review_event,
            timeout=review_timeout,
            retry_policy=RetryPolicy(max_retries=2, retry_delay=2.0),
            before_retry=lambda a, e: (review_text.clear(), thought_text.clear()) if a > 0 else None,
        )
        full_text = "".join(review_text)
        combined_text = full_text
        if thought_text:
            combined_text = full_text + "\n" + "".join(thought_text)
        review_result = parse_review_output(
            combined_text,
            cycle,
            parse_with_llm_fn=lambda raw: parse_review_with_llm(raw, settings),
        )
        circuit.review_failure_consecutive = 0
        circuit.review_circuit_open_until_cycle = 0
    except Exception as e:
        diag_raw = build_review_exception_diagnostics_fn(e, cycle=cycle)
        diag = normalize_review_diagnostics(diag_raw)
        circuit.last_review_failure_diag = dict(diag)

        try:
            circuit.review_failure_consecutive = int(circuit.review_failure_consecutive or 0) + 1
        except Exception:
            circuit.review_failure_consecutive = 1
        if enabled and circuit.review_failure_consecutive >= max_consecutive and cooldown_cycles > 0:
            try:
                circuit.review_circuit_open_until_cycle = int(cycle or 0) + int(cooldown_cycles)
            except Exception:
                circuit.review_circuit_open_until_cycle = int(cycle or 0)
            try:
                circuit.last_review_failure_diag["review_circuit_open"] = True
                circuit.last_review_failure_diag["open_until_cycle"] = int(circuit.review_circuit_open_until_cycle or 0)
                circuit.last_review_failure_diag["consecutive_failures"] = int(circuit.review_failure_consecutive or 0)
                circuit.last_review_failure_diag["decision"] = "review_failed_open_circuit"
            except Exception:
                pass
        diag_json = ""
        try:
            diag_json = json.dumps(diag, ensure_ascii=False, sort_keys=True)
        except Exception:
            diag_json = '{"phase":"review","decision":"review_failed_continue"}'

        try:
            logger.warning(format_review_exception_log_line(diag, diag_json=diag_json))
        except Exception as log_e:
            d = normalize_review_diagnostics(diag)
            err_type = str(d.get("err_type") or "Exception")
            err_repr = str(d.get("err_repr") or "").strip() or f"<{err_type}>"
            error_text = str(d.get("error_text") or "").strip() or err_repr
            logger.warning(
                "[Spec] 多视角审查异常: phase=review role=multi_perspective cycle=%s decision=%s "
                "err_type=%s err_repr=%s error_text=%s (log_format_failed=%s), 将继续循环",
                d.get("cycle"),
                d.get("decision") or "review_failed_continue",
                err_type,
                err_repr,
                error_text,
                type(log_e).__name__,
            )
        _fail_reason = str(diag.get("fail_reason") or "").strip()
        if _fail_reason == "timeout":
            _suggestion_text = "审查超时，跳过本轮审查继续执行"
        else:
            _suggestion_text = f"审查执行异常: {str(diag.get('error_text') or '').strip() or str(diag.get('err_repr') or '(empty)')}"
        review_result = ReviewResult(
            reviews=[
                PerspectiveReview(
                    perspective=p,
                    passed=False,
                    suggestions=[_suggestion_text],
                    summary="异常",
                )
                for p in ReviewPerspective
            ],
            iteration=cycle,
        )

    if on_review_done:
        on_review_done(cycle, review_result)

    return review_result


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
                    suggestions=["审查输出解析失败，请检查实现质量"],
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
        logger.warning("[Spec] LLM 兜底审查解析失败: %s", e)
        return []
