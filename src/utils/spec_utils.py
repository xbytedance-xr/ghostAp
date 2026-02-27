"""Spec/Loop shared utilities.

目标：减少 spec_engine / loop_engine 的相互依赖，把可复用的解析/校验逻辑
下沉到 utils 层。

- Review 解析（严格/宽松两条路径，不含 LLM 兜底）
- Criteria 评估的正则 patterns
- JSON 产物提取（```json fenced block``` 优先）
- Spec/Plan 结构化产物校验（用于在 UI 中显式提示降级）
"""

from __future__ import annotations

import re
from typing import Optional

from ..loop_engine.models import ReviewPerspective, PerspectiveReview


# ---------------------------------------------------------------------------
# Criteria patterns
# ---------------------------------------------------------------------------


CRITERIA_PATTERNS: list[re.Pattern] = [
    re.compile(rf"CRITERIA_{i}\s*:\s*(PASS|FAIL)") for i in range(1, 101)
]


# ---------------------------------------------------------------------------
# JSON extraction / normalization
# ---------------------------------------------------------------------------


def normalize_list(items) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for x in items:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def extract_json_blob(text: str) -> Optional[str]:
    """Extract a JSON object string from text.

    优先匹配 ```json fenced block```，否则退化为第一个 '{' 到最后一个 '}'。
    """
    if not text:
        return None
    raw = text.strip()
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                return p
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start:end + 1]
    return None


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------


def validate_spec_artifact_dict(data: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Spec JSON 不是对象"]

    required_list_fields = [
        "goals",
        "functional_spec",
        "non_functional_requirements",
        "acceptance_criteria",
        "out_of_scope",
        "risks",
        "clarification_questions",
        "decisions",
    ]
    for k in required_list_fields:
        v = data.get(k)
        if not isinstance(v, list):
            errors.append(f"Spec 字段 `{k}` 不是数组")

    # soft constraints
    ac = data.get("acceptance_criteria")
    if isinstance(ac, list) and len([x for x in ac if str(x).strip()]) == 0:
        errors.append("Spec 字段 `acceptance_criteria` 为空（应提供可验收条件）")

    return errors


def validate_plan_artifact_dict(data: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Plan JSON 不是对象"]

    if not isinstance(data.get("architecture"), str):
        errors.append("Plan 字段 `architecture` 不是字符串")

    required_list_fields = [
        "tech_stack",
        "steps",
        "file_changes",
        "test_plan",
        "risks",
    ]
    for k in required_list_fields:
        v = data.get(k)
        if not isinstance(v, list):
            errors.append(f"Plan 字段 `{k}` 不是数组")

    steps = data.get("steps")
    if isinstance(steps, list) and len([x for x in steps if str(x).strip()]) == 0:
        errors.append("Plan 字段 `steps` 为空（应给出可执行步骤）")

    return errors


# ---------------------------------------------------------------------------
# Review parsing (strict/tolerant, no LLM)
# ---------------------------------------------------------------------------


REVIEW_SECTION_PATTERN = re.compile(
    r"\[(\w+)\]\s*\n\s*(PASS|FAIL)\b(.*?)(?=\[(?:ARCHITECT|PRODUCT|USER|TESTER)\]|\Z)",
    re.DOTALL,
)


REVIEW_HEADER_EN_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?im)^\s*(?:#+\s*)?\[\s*(ARCHITECT|PRODUCT|USER|TESTER)\s*\]\s*(?:[:：]?\s*(.*))?$"),
    re.compile(r"(?im)^\s*(?:#+\s*)?\*{1,2}\[\s*(ARCHITECT|PRODUCT|USER|TESTER)\s*\]\*{1,2}\s*(?:[:：]?\s*(.*))?$"),
    re.compile(r"(?im)^\s*(?:#+\s*)?\*{1,2}(ARCHITECT|PRODUCT|USER|TESTER)\*{1,2}\s*(?:[:：]?\s*(.*))?$"),
    re.compile(r"(?im)^\s*#+\s*(ARCHITECT|PRODUCT|USER|TESTER)\s*(?:[:：]?\s*(.*))?$"),
    re.compile(r"(?im)^\s*(ARCHITECT|PRODUCT|USER|TESTER)\s*[:：]\s*(.*)$"),
]

REVIEW_HEADER_ZH_PATTERN = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:\d+[.、)]\s*)?(?:[-*]\s*)?"
    r"(?:🏗️|📦|👤|🧪)?\s*"
    r"\*{0,2}"
    r"(架构师|产品经理|用户|测试)"
    r"\*{0,2}"
    r"(?:审查|评审|视角)?"
    r"\s*(?:[:：]\s*(.*))?$",
)

PERSPECTIVE_ZH_MAP: dict[str, ReviewPerspective] = {
    "架构师": ReviewPerspective.ARCHITECT,
    "产品经理": ReviewPerspective.PRODUCT,
    "用户": ReviewPerspective.USER,
    "测试": ReviewPerspective.TESTER,
}

PERSPECTIVE_TAG_MAP: dict[str, ReviewPerspective] = {
    "ARCHITECT": ReviewPerspective.ARCHITECT,
    "PRODUCT": ReviewPerspective.PRODUCT,
    "USER": ReviewPerspective.USER,
    "TESTER": ReviewPerspective.TESTER,
}


def normalize_review_verdict(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip().upper()
    if "PASS" in t:
        return "PASS"
    if "FAIL" in t:
        return "FAIL"
    if "通过" in text and "不通过" not in text:
        return "PASS"
    if "不通过" in text or "未通过" in text or "失败" in text:
        return "FAIL"
    return None


BULLET_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.)]|\d+、)\s*(.+?)\s*$")


def extract_suggestions_from_body(body: str, limit: int = 10) -> list[str]:
    suggestions: list[str] = []
    if not body:
        return suggestions
    for line in body.split("\n"):
        m = BULLET_PATTERN.match(line)
        if not m:
            continue
        s = (m.group(1) or "").strip()
        if s:
            suggestions.append(s)
        if len(suggestions) >= limit:
            break
    return suggestions


def split_review_sections(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    normalized = text.replace("\r\n", "\n")
    # hits: (start, end, tag, header_tail)
    hits: list[tuple[int, int, str, str]] = []
    seen_positions: set[int] = set()
    for pat in REVIEW_HEADER_EN_PATTERNS:
        for m in pat.finditer(normalized):
            if m.start() not in seen_positions:
                seen_positions.add(m.start())
                tail = (m.group(2) or "").strip()
                hits.append((m.start(), m.end(), m.group(1).upper(), tail))
    for m in REVIEW_HEADER_ZH_PATTERN.finditer(normalized):
        zh = (m.group(1) or "").strip()
        p = PERSPECTIVE_ZH_MAP.get(zh)
        if not p:
            continue
        tail = (m.group(2) or "").strip()
        hits.append((m.start(), m.end(), p.name, tail))

    if not hits:
        return []
    hits.sort(key=lambda x: x[0])
    sections: list[tuple[str, str]] = []
    for i, (start, end, tag, header_tail) in enumerate(hits):
        next_start = hits[i + 1][0] if i + 1 < len(hits) else len(normalized)
        block = normalized[end:next_start].strip("\n")
        # Preserve same-line verdicts like "[ARCHITECT] FAIL" by injecting
        # the header tail as the first line of the section body.
        if header_tail:
            combined = header_tail if not block else (header_tail + "\n" + block)
        else:
            combined = block
        sections.append((tag, combined))
    return sections


def parse_review_output_strict_tolerant(text: str, iteration: int) -> list[PerspectiveReview]:
    """Parse review output into structured reviews (no LLM fallback)."""
    reviews: list[PerspectiveReview] = []
    found: set[ReviewPerspective] = set()
    raw = (text or "").replace("\r\n", "\n")

    # strict
    for match in REVIEW_SECTION_PATTERN.finditer(raw):
        tag = match.group(1).upper()
        verdict = match.group(2).upper()
        body = match.group(3).strip()
        perspective = PERSPECTIVE_TAG_MAP.get(tag)
        if not perspective or perspective in found:
            continue
        found.add(perspective)
        passed = verdict == "PASS"
        suggestions = extract_suggestions_from_body(body) if not passed else []
        reviews.append(PerspectiveReview(
            perspective=perspective,
            passed=passed,
            suggestions=suggestions,
            summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
        ))

    if reviews:
        return reviews

    # tolerant
    for tag, block in split_review_sections(raw):
        perspective = PERSPECTIVE_TAG_MAP.get(tag)
        if not perspective or perspective in found:
            continue
        found.add(perspective)
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        head = "\n".join(lines[:3])
        verdict = normalize_review_verdict(head) or normalize_review_verdict(block)
        passed = verdict == "PASS"
        body_text = "\n".join(lines[1:]) if len(lines) > 1 else ""
        suggestions = extract_suggestions_from_body(body_text)
        if verdict is None:
            passed = len(suggestions) == 0
        if passed:
            suggestions = []
        elif not suggestions:
            tail_candidates: list[str] = []
            for ln in lines[1:]:
                if normalize_review_verdict(ln):
                    continue
                cleaned = ln.lstrip("-•* ").strip()
                if cleaned:
                    tail_candidates.append(cleaned)
                if len(tail_candidates) >= 3:
                    break
            suggestions = tail_candidates
        reviews.append(PerspectiveReview(
            perspective=perspective,
            passed=passed,
            suggestions=suggestions,
            summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
        ))

    return reviews
