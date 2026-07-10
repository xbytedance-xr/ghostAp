# Workflow Result Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace character-truncated Workflow completion bodies with a typed, budget-aware result brief that shows complete semantic items while preserving the full result in the existing HTML/Markdown report.

**Architecture:** Add a pure `result_brief` normalization boundary between arbitrary Workflow results and the Feishu renderer. Generated workflows should emit a `card_summary`, older results should use deterministic field mapping, and the renderer should consume only the normalized brief. Completion-card overflow must remove whole optional items or sections and report omitted counts; the full report continues to consume the untouched `WorkflowProject`.

**Tech Stack:** Python 3.11+, Pydantic v2, Feishu CardKit 2.0 JSON, Node.js Workflow runtime, `unittest`/`pytest`, `uv`, ruff.

---

## File map

- Create `src/workflow_engine/result_brief.py`: typed result-brief contract, legacy normalization, semantic item selection, and byte-budget fitting.
- Create `tests/test_workflow_result_brief.py`: unit coverage for the contract, legacy inputs, semantic omission, ordering, and text budgets.
- Modify `src/workflow_engine/renderer.py`: render the result brief, reorder the completion card, collapse process details, and replace character-truncation fallback with whole-element degradation.
- Modify `tests/test_workflow_renderer.py`: completion-card regression coverage, including full-report sentinels and 28KB enforcement.
- Modify `src/workflow_engine/script_gen.py`: require `card_summary` in generated scripts and make the bounded fallback script return the same envelope.
- Modify `tests/test_workflow_dynamic_roles.py`: prompt contract regression.
- Modify `tests/test_workflow_subagent.py`: bounded fallback script contract regression.
- Modify `src/feishu/handlers/workflow_script.py`: shared completion-render failure fallback that never emits `result[:500]`.
- Modify `src/feishu/handlers/workflow.py`: route its duplicate callback path through the shared safe fallback.
- Modify `tests/test_workflow_confirm.py`: attachment and fallback delivery regressions.
- Modify `.Memory/2026-07-10.md` and `.Memory/Abstract.md`: record the behavior, evidence, validation, and residual risks.

### Task 1: Add the typed result-brief normalization boundary

**Files:**
- Create: `tests/test_workflow_result_brief.py`
- Create: `src/workflow_engine/result_brief.py`

- [ ] **Step 1: Write failing contract and compatibility tests**

Create `tests/test_workflow_result_brief.py` with focused tests for the public API:

```python
import json

from src.workflow_engine.result_brief import (
    BriefSeverity,
    BriefVerdict,
    build_result_brief,
    fit_result_brief,
)


def test_build_result_brief_prefers_typed_card_summary() -> None:
    raw = json.dumps(
        {
            "card_summary": {
                "verdict": "needs_attention",
                "conclusion": "两项事实错误必须修正后才能采纳。",
                "findings": [
                    {"severity": "high", "text": "Freshness Gate 已有三段式重试闭环。"},
                    {"severity": "medium", "text": "并发瓶颈缺少实测证据。"},
                ],
                "verification": [{"status": "failed", "text": "评审未通过。"}],
                "deliverables": [{"type": "document", "text": "完整审计报告。"}],
                "next_steps": ["修正事实错误后重新评审。"],
            }
        },
        ensure_ascii=False,
    )

    brief = build_result_brief(raw)

    assert brief.verdict is BriefVerdict.NEEDS_ATTENTION
    assert brief.conclusion == "两项事实错误必须修正后才能采纳。"
    assert brief.findings[0].severity is BriefSeverity.HIGH
    assert brief.verification[0].text == "评审未通过。"
    assert brief.deliverables[0].text == "完整审计报告。"
    assert brief.next_steps[0].text == "修正事实错误后重新评审。"
    assert brief.source == "contract"


def test_build_result_brief_maps_legacy_verification_and_issues() -> None:
    raw = json.dumps(
        {
            "summary": "报告主体有价值，但需要修正事实错误。",
            "verification": {
                "approved": False,
                "summary": "验证未通过。",
                "issues": [
                    {"severity": "high", "claim": "Freshness Gate 判断错误。"},
                    "缺少并发基准。",
                ],
            },
            "recommendations": ["修正后重新评审。"],
        },
        ensure_ascii=False,
    )

    brief = build_result_brief(raw)

    assert brief.verdict is BriefVerdict.NEEDS_ATTENTION
    assert brief.conclusion == "报告主体有价值，但需要修正事实错误。"
    assert [item.text for item in brief.findings] == [
        "Freshness Gate 判断错误。",
        "缺少并发基准。",
    ]
    assert brief.verification[0].text == "验证未通过。"
    assert brief.next_steps[0].text == "修正后重新评审。"
    assert brief.source == "legacy"


def test_build_result_brief_uses_neutral_conclusion_for_unstructured_long_text() -> None:
    brief = build_result_brief("没有结构的长正文" * 1000)

    assert brief.conclusion == "任务已完成，完整结果见报告。"
    assert brief.findings == []
    assert brief.source == "fallback"


def test_fit_result_brief_omits_whole_items_without_character_slicing() -> None:
    sentinel = "WHOLE_ITEM_SENTINEL"
    raw = json.dumps(
        {
            "card_summary": {
                "verdict": "needs_attention",
                "conclusion": "需要修正。",
                "findings": [
                    {"severity": "high", "text": "短而完整的发现。"},
                    {"severity": "low", "text": ("很长的完整发现" * 300) + sentinel},
                ],
            }
        },
        ensure_ascii=False,
    )

    fitted = fit_result_brief(build_result_brief(raw), max_text_bytes=300)

    assert [item.text for item in fitted.findings] == ["短而完整的发现。"]
    assert fitted.omitted_counts["findings"] == 1
    assert sentinel not in fitted.model_dump_json()


def test_fit_result_brief_orders_findings_by_severity_stably() -> None:
    raw = json.dumps(
        {
            "findings": [
                {"severity": "low", "description": "low-1"},
                {"severity": "high", "description": "high-1"},
                {"severity": "high", "description": "high-2"},
                {"severity": "medium", "description": "medium-1"},
            ]
        }
    )

    brief = build_result_brief(raw)

    assert [item.text for item in brief.findings] == [
        "high-1",
        "high-2",
        "medium-1",
        "low-1",
    ]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run python -m pytest tests/test_workflow_result_brief.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'src.workflow_engine.result_brief'`.

- [ ] **Step 3: Implement the minimal typed normalizer**

Create `src/workflow_engine/result_brief.py` with these public types and functions:

```python
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class BriefVerdict(str, Enum):
    PASSED = "passed"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BriefSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class BriefItem(BaseModel):
    text: str
    severity: BriefSeverity = BriefSeverity.INFO
    status: str = "info"
    kind: str = "other"


class WorkflowResultBrief(BaseModel):
    verdict: BriefVerdict = BriefVerdict.UNKNOWN
    conclusion: str = "任务已完成，完整结果见报告。"
    findings: list[BriefItem] = Field(default_factory=list)
    verification: list[BriefItem] = Field(default_factory=list)
    deliverables: list[BriefItem] = Field(default_factory=list)
    next_steps: list[BriefItem] = Field(default_factory=list)
    omitted_counts: dict[str, int] = Field(default_factory=dict)
    source: Literal["contract", "legacy", "fallback"] = "fallback"


def build_result_brief(raw_result: str | None) -> WorkflowResultBrief:
    payload = _parse_object(raw_result)
    if payload is None:
        return WorkflowResultBrief()

    contract = payload.get("card_summary")
    if isinstance(contract, dict):
        return _brief_from_payload(contract, source="contract")

    brief = _brief_from_payload(payload, source="legacy")
    verification = payload.get("verification")
    if isinstance(verification, dict):
        if verification.get("approved") is False:
            brief.verdict = BriefVerdict.NEEDS_ATTENTION
        summary = _first_text(verification)
        if summary:
            brief.verification.append(BriefItem(text=summary, status="failed" if verification.get("approved") is False else "info"))
        brief.findings.extend(_normalize_items(verification.get("issues")))
        brief.findings = _sort_findings(brief.findings)

    known = bool(
        brief.findings
        or brief.verification
        or brief.deliverables
        or brief.next_steps
        or brief.conclusion != _NEUTRAL_CONCLUSION
    )
    if not known:
        return WorkflowResultBrief()
    return brief


def fit_result_brief(
    brief: WorkflowResultBrief,
    *,
    max_text_bytes: int = 12_000,
    max_item_bytes: int = 900,
) -> WorkflowResultBrief:
    omitted = dict(brief.omitted_counts)
    conclusion = brief.conclusion
    if _byte_len(conclusion) > max_item_bytes:
        conclusion = _NEUTRAL_CONCLUSION
        omitted["conclusion"] = omitted.get("conclusion", 0) + 1

    used = _byte_len(conclusion)
    fitted: dict[str, list[BriefItem]] = {
        "findings": [],
        "verification": [],
        "deliverables": [],
        "next_steps": [],
    }
    for section in ("verification", "findings", "deliverables", "next_steps"):
        for item in getattr(brief, section):
            cost = _byte_len(item.text) + 32
            if cost > max_item_bytes or used + cost > max_text_bytes:
                omitted[section] = omitted.get(section, 0) + 1
                continue
            fitted[section].append(item)
            used += cost

    return brief.model_copy(
        update={
            "conclusion": conclusion,
            "findings": fitted["findings"],
            "verification": fitted["verification"],
            "deliverables": fitted["deliverables"],
            "next_steps": fitted["next_steps"],
            "omitted_counts": omitted,
        },
        deep=True,
    )


_NEUTRAL_CONCLUSION = "任务已完成，完整结果见报告。"
_TEXT_KEYS = ("text", "claim", "description", "issue", "summary", "name", "path")
_SEVERITY_ORDER = {
    BriefSeverity.HIGH: 0,
    BriefSeverity.MEDIUM: 1,
    BriefSeverity.LOW: 2,
    BriefSeverity.INFO: 3,
}


def _parse_object(raw_result: str | None) -> dict[str, Any] | None:
    text = str(raw_result or "").strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if not isinstance(value, dict):
        return ""
    for key in _TEXT_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _severity(value: Any) -> BriefSeverity:
    try:
        return BriefSeverity(str(value or "info").lower())
    except ValueError:
        return BriefSeverity.INFO


def _normalize_items(value: Any, *, kind: str = "other") -> list[BriefItem]:
    values = value if isinstance(value, list) else [value]
    items: list[BriefItem] = []
    for candidate in values:
        text = _first_text(candidate)
        if not text:
            continue
        metadata = candidate if isinstance(candidate, dict) else {}
        items.append(
            BriefItem(
                text=text,
                severity=_severity(metadata.get("severity")),
                status=str(metadata.get("status") or "info"),
                kind=str(metadata.get("type") or kind),
            )
        )
    return items


def _sort_findings(items: list[BriefItem]) -> list[BriefItem]:
    return sorted(items, key=lambda item: _SEVERITY_ORDER[item.severity])


def _brief_from_payload(
    payload: dict[str, Any],
    *,
    source: Literal["contract", "legacy"],
) -> WorkflowResultBrief:
    verdict_raw = payload.get("verdict")
    try:
        verdict = BriefVerdict(str(verdict_raw))
    except ValueError:
        verdict = BriefVerdict.FAILED if payload.get("error") else BriefVerdict.UNKNOWN

    conclusion = ""
    for key in ("conclusion", "summary"):
        conclusion = _first_text(payload.get(key))
        if conclusion:
            break

    findings: list[BriefItem] = []
    for key in ("findings", "issues", "risks"):
        findings.extend(_normalize_items(payload.get(key)))

    verification = _normalize_items(payload.get("verification"), kind="verification")
    deliverables: list[BriefItem] = []
    for key in ("deliverables", "artifacts"):
        deliverables.extend(_normalize_items(payload.get(key), kind="artifact"))
    next_steps: list[BriefItem] = []
    for key in ("next_steps", "recommendations"):
        next_steps.extend(_normalize_items(payload.get(key), kind="next_step"))

    return WorkflowResultBrief(
        verdict=verdict,
        conclusion=conclusion or _NEUTRAL_CONCLUSION,
        findings=_sort_findings(findings),
        verification=verification,
        deliverables=deliverables,
        next_steps=next_steps,
        source=source,
    )


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8", errors="surrogatepass"))
```

Implementation requirements:

- Parse only top-level JSON objects; malformed or non-object results use the neutral fallback.
- Normalize strings and known dict keys without recursively flattening arbitrary objects.
- Recognize text keys in this order: `text`, `claim`, `description`, `issue`, `summary`, `name`, `path`.
- Map `approved=false` to `needs_attention`; top-level `error` to `failed`; otherwise keep `unknown` unless the contract supplies a verdict.
- Sort findings by `high`, `medium`, `low`, `info`, preserving original order within each severity.
- `fit_result_brief()` must copy the model, reserve the conclusion first, then fit verification, high-to-low findings, deliverables, and next steps.
- Reject an overlong item as a whole and increment `omitted_counts[section]`; never slice item text.

- [ ] **Step 4: Run the unit tests and verify GREEN**

Run:

```bash
uv run python -m pytest tests/test_workflow_result_brief.py -q
```

Expected: all tests in the new file pass.

- [ ] **Step 5: Lint and commit Task 1**

Run:

```bash
uv run ruff check src/workflow_engine/result_brief.py tests/test_workflow_result_brief.py
git diff --check
```

Commit:

```bash
git add src/workflow_engine/result_brief.py tests/test_workflow_result_brief.py
git commit -m "feat(workflow): add typed result brief normalization"
```

### Task 2: Render complete semantic items in the completion card

**Files:**
- Modify: `tests/test_workflow_renderer.py`
- Modify: `src/workflow_engine/renderer.py`

- [ ] **Step 1: Replace the truncation expectation with failing result-brief tests**

Update `TestRenderCompletionCard` so the long-report regression asserts the new behavior:

```python
def test_completion_card_with_html_report_shows_complete_brief_without_truncation(self):
    sentinel = "FINAL_SENTINEL_AFTER_LONG_CONTENT"
    result = {
        "card_summary": {
            "verdict": "needs_attention",
            "conclusion": "两项事实错误必须修正。",
            "findings": [
                {"severity": "high", "text": "Freshness Gate 已有三段式重试闭环。"},
                {"severity": "low", "text": ("完整长发现" * 1200) + sentinel},
            ],
            "verification": [{"status": "failed", "text": "评审未通过。"}],
            "next_steps": ["修正事实错误后重新评审。"],
        },
        "final_report": ("完整报告 " * 1200) + sentinel,
    }
    project = self._make_project(result=json.dumps(result, ensure_ascii=False))

    card = render_completion_card(
        project,
        report_status={"generated": True, "attachment_sent": True, "html_path": "/tmp/report.html"},
    )
    all_content = self._extract_all_text(card["elements"])

    self.assertIn("两项事实错误必须修正", all_content)
    self.assertIn("Freshness Gate 已有三段式重试闭环", all_content)
    self.assertIn("评审未通过", all_content)
    self.assertIn("修正事实错误后重新评审", all_content)
    self.assertIn("另有 1 条", all_content)
    self.assertNotIn("内容已截断", all_content)
    self.assertNotIn(sentinel, all_content)


def test_completion_card_keeps_result_before_collapsed_process(self):
    project = self._make_project(
        result=json.dumps(
            {"card_summary": {"verdict": "passed", "conclusion": "目标已完成。"}},
            ensure_ascii=False,
        )
    )

    card = render_completion_card(project)

    conclusion_index = next(
        index for index, element in enumerate(card["elements"])
        if "目标已完成" in str(element)
    )
    process_index = next(
        index for index, element in enumerate(card["elements"])
        if element.get("tag") == "collapsible_panel" and "执行过程" in str(element)
    )
    self.assertLess(conclusion_index, process_index)
    self.assertFalse(card["elements"][process_index]["expanded"])


def test_completion_card_stays_under_payload_limit_without_slicing_result_items(self):
    findings = [
        {"severity": "medium", "text": f"完整发现 {index}: " + ("证据" * 120)}
        for index in range(100)
    ]
    project = self._make_project(
        result=json.dumps(
            {"card_summary": {"verdict": "needs_attention", "conclusion": "需要处理。", "findings": findings}},
            ensure_ascii=False,
        )
    )

    card = render_completion_card(project, report_status={"generated": True, "attachment_sent": True})
    payload = json.dumps(card, ensure_ascii=False).encode("utf-8")
    all_content = self._extract_all_text(card["elements"])

    self.assertLessEqual(len(payload), 28_000)
    self.assertIn("需要处理", all_content)
    self.assertIn("详见报告", all_content)
    self.assertNotIn("内容已截断", all_content)
```

Also update existing metric assertions to expect outcome metrics such as `验证` and `风险`/`交付物` instead of requiring `Token` in the terminal result card.

- [ ] **Step 2: Run the renderer tests and verify RED**

Run:

```bash
uv run python -m pytest \
  tests/test_workflow_renderer.py::TestRenderCompletionCard::test_completion_card_with_html_report_shows_complete_brief_without_truncation \
  tests/test_workflow_renderer.py::TestRenderCompletionCard::test_completion_card_keeps_result_before_collapsed_process \
  tests/test_workflow_renderer.py::TestRenderCompletionCard::test_completion_card_stays_under_payload_limit_without_slicing_result_items \
  -q
```

Expected: failures show the old `执行报告` character-truncation path and non-collapsed process layout.

- [ ] **Step 3: Refactor `renderer.py` to consume only `WorkflowResultBrief`**

Make these production changes:

- Import `BriefSeverity`, `BriefVerdict`, `WorkflowResultBrief`, `build_result_brief`, and `fit_result_brief`.
- Remove `_truncate_completion_text()`, `_format_result_value()`, and `_completion_report_markdown()` after all completion-card callers are migrated.
- Add pure helpers:

```python
def _brief_item_lines(items: list[BriefItem], *, section: str, omitted: int) -> str:
    lines = [f"- {item.text}" for item in items]
    if omitted:
        lines.append(f"- 另有 {omitted} 条完整内容，详见报告")
    return "\n".join(lines)


def _render_result_brief_elements(brief: WorkflowResultBrief) -> list[dict[str, Any]]:
    elements = [_md_element(f"**结论**\n{brief.conclusion}")]
    # Append only non-empty sections in this order:
    # findings, verification, deliverables, next_steps.
    return elements
```

- Build and fit the brief before constructing result elements:

```python
brief = fit_result_brief(build_result_brief(project.result))
```

- Use four outcome-oriented stats: elapsed time, completed phases, verdict label, and high-risk count or deliverable count.
- Place result sections immediately after stats.
- Wrap `_completion_process_markdown()` in one collapsed `collapsible_panel` titled `执行过程`.
- Put report status after the collapsed process panel.
- Do not pass completed result elements through `_enforce_card_size()`.
- Serialize the finished completion card; if it exceeds `_CARD_MAX_BYTES`, rebuild a minimal completion card from verdict, conclusion, total omitted count, and report status. The minimal path must not include raw `project.result`.
- Use `_middle_ellipsis()` only for card title/phase labels, never for result-brief item text.

- [ ] **Step 4: Run renderer tests and verify GREEN**

Run:

```bash
uv run python -m pytest tests/test_workflow_renderer.py -q
```

Expected: all renderer tests pass with no `(内容已截断)` expectation.

- [ ] **Step 5: Lint and commit Task 2**

Run:

```bash
uv run ruff check src/workflow_engine/renderer.py tests/test_workflow_renderer.py
git diff --check
```

Commit:

```bash
git add src/workflow_engine/renderer.py tests/test_workflow_renderer.py
git commit -m "fix(workflow): render complete result brief entries"
```

### Task 3: Make new and fallback workflows emit `card_summary`

**Files:**
- Modify: `tests/test_workflow_dynamic_roles.py`
- Modify: `tests/test_workflow_subagent.py`
- Modify: `src/workflow_engine/script_gen.py`

- [ ] **Step 1: Write failing prompt and fallback-script contract tests**

Add to `TestPromptStructure` in `tests/test_workflow_dynamic_roles.py`:

```python
def test_script_generation_prompt_requires_card_summary_contract(self):
    prompt = self._build_prompt()

    assert "card_summary" in prompt
    assert '"verdict"' in prompt
    assert '"conclusion"' in prompt
    assert '"findings"' in prompt
    assert '"verification"' in prompt
    assert '"deliverables"' in prompt
    assert '"next_steps"' in prompt
    assert "完整语义条目" in prompt
```

Add to `TestGenerateSimpleScriptEncouragement` in `tests/test_workflow_subagent.py`:

```python
def test_generate_simple_script_returns_card_summary_envelope():
    script = generate_simple_script("Implement and verify a focused change")

    assert "function completionEnvelope" in script
    assert "card_summary" in script
    assert "needs_attention" in script
    assert "任务已完成，完整结果见报告。" in script
```

- [ ] **Step 2: Run both tests and verify RED**

Run:

```bash
uv run python -m pytest \
  tests/test_workflow_dynamic_roles.py::TestPromptStructure::test_script_generation_prompt_requires_card_summary_contract \
  tests/test_workflow_subagent.py::TestGenerateSimpleScriptEncouragement::test_generate_simple_script_returns_card_summary_envelope \
  -q
```

Expected: prompt/script string assertions fail because the contract is not present.

- [ ] **Step 3: Add the completion contract to generated and bounded scripts**

In `_SCRIPT_GEN_PROMPT_TEMPLATE`, add a `Completion Result Contract` section before `## Now Generate`. It must require the final return value to preserve the full business result and include:

```javascript
return {
  card_summary: {
    verdict: "passed|needs_attention|failed|unknown",
    conclusion: "one complete actionable conclusion",
    findings: [{ severity: "high|medium|low|info", text: "one complete finding" }],
    verification: [{ status: "passed|failed|warning|info", text: "one complete verification result" }],
    deliverables: [{ type: "code|test|document|artifact|other", text: "one complete deliverable" }],
    next_steps: ["one complete next action"],
  },
  result: fullResult,
  verification: fullVerification,
};
```

Explicitly state that brief items must be complete semantic units and must not contain manual character truncation markers.

In `generate_simple_script()`, define a local `completionEnvelope(result, review = null)` helper that:

- Uses `review.summary` or `result.conclusion`/`result.summary` only when it is a non-empty string.
- Otherwise uses `任务已完成，完整结果见报告。`.
- Maps `review.approve === false` to `needs_attention`, successful review to `passed`, and missing review to `unknown`.
- Maps review issues into complete finding objects without slicing.
- Preserves `result` and `review` unchanged in the returned envelope.

Route the analysis-only return, verification-failure return, negative-review return, and successful return through this helper. Keep top-level execution errors on the existing failure path so Engine failure classification is unchanged.

- [ ] **Step 4: Run prompt, script-generation, and validator tests**

Run:

```bash
uv run python -m pytest \
  tests/test_workflow_dynamic_roles.py \
  tests/test_workflow_subagent.py \
  tests/test_workflow_subagent_ac5.py \
  tests/test_workflow_api_contract.py \
  tests/test_workflow_token_efficiency.py \
  -q
```

Expected: all selected tests pass; the prompt remains below the existing token-efficiency bound.

- [ ] **Step 5: Lint and commit Task 3**

Run:

```bash
uv run ruff check src/workflow_engine/script_gen.py tests/test_workflow_dynamic_roles.py tests/test_workflow_subagent.py
git diff --check
```

Commit:

```bash
git add src/workflow_engine/script_gen.py tests/test_workflow_dynamic_roles.py tests/test_workflow_subagent.py
git commit -m "feat(workflow): require structured card summaries"
```

### Task 4: Remove truncated raw-result text fallbacks from both callback paths

**Files:**
- Modify: `tests/test_workflow_confirm.py`
- Modify: `src/feishu/handlers/workflow_script.py`
- Modify: `src/feishu/handlers/workflow.py`

- [ ] **Step 1: Write failing callback fallback regressions**

Add tests that force completion rendering/delivery to fail after report generation:

```python
def test_workflow_completion_fallback_never_replies_with_partial_raw_result(self):
    handler, _ctx = self._make_handler()
    handler._send_workflow_completion_report = MagicMock(
        return_value={
            "generated": True,
            "attachment_sent": False,
            "html_filename": "wf-report.html",
            "html_path": "/tmp/wf-report.html",
            "error": "upload failed",
        }
    )
    handler._replace_or_send_workflow_rendered_card = MagicMock(side_effect=RuntimeError("card failed"))
    callbacks = handler._build_workflow_callbacks("msg_1", "chat_1", None)
    sentinel = "RAW_RESULT_SENTINEL"
    project = WorkflowProject(
        name="done workflow",
        requirement="do X",
        status=WorkflowStatus.COMPLETED,
        result=("raw body " * 1000) + sentinel,
    )

    callbacks.on_done(project)

    fallback_text = handler.reply_text.call_args.args[1]
    self.assertIn("结果卡发送失败", fallback_text)
    self.assertIn("wf-report.html", fallback_text)
    self.assertNotIn(sentinel, fallback_text)
    self.assertNotIn("raw body", fallback_text)
```

Add an equivalent test for the mixin callback path if existing tests instantiate it separately.

- [ ] **Step 2: Run the fallback regression and verify RED**

Run:

```bash
uv run python -m pytest \
  tests/test_workflow_confirm.py::TestWorkflowHandlerConfirmFlow::test_workflow_completion_fallback_never_replies_with_partial_raw_result \
  -q
```

Expected: the old `result[:500]` text appears and the assertion fails.

- [ ] **Step 3: Add one shared safe fallback helper**

Add to `WorkflowScriptMixin`:

```python
def _reply_workflow_completion_fallback(
    self,
    *,
    message_id: str,
    report_status: dict[str, Any] | None,
) -> None:
    lines = ["✅ Workflow 已结束", "结果卡发送失败，未展示不完整结果。"]
    status = report_status or {}
    if status.get("attachment_sent"):
        lines.append("完整 HTML 报告已回复到当前话题。")
    elif status.get("generated"):
        filename = status.get("html_filename") or "Workflow HTML 报告"
        lines.append(f"完整报告已保存在本地：{filename}")
    else:
        lines.append("完整报告未生成，请查看服务日志。")
    self.reply_text(message_id, "\n\n".join(lines))
```

In both `on_done()` implementations:

- Initialize `report_status: dict[str, Any] | None = None` before the `try`.
- Preserve the status returned from `_send_workflow_completion_report()`.
- Replace `result[:500]` fallback construction with
  `self._reply_workflow_completion_fallback(message_id=message_id, report_status=report_status)`.
- Keep `terminal_sent` behavior and late-progress protection unchanged.

- [ ] **Step 4: Run callback and IM report tests**

Run:

```bash
uv run python -m pytest tests/test_workflow_confirm.py tests/test_im_client_sanitize.py -q
```

Expected: all tests pass, including report upload success/failure and late-progress regressions.

- [ ] **Step 5: Lint and commit Task 4**

Run:

```bash
uv run ruff check \
  src/feishu/handlers/workflow.py \
  src/feishu/handlers/workflow_script.py \
  tests/test_workflow_confirm.py
git diff --check
```

Commit:

```bash
git add src/feishu/handlers/workflow.py src/feishu/handlers/workflow_script.py tests/test_workflow_confirm.py
git commit -m "fix(workflow): preserve complete terminal fallback semantics"
```

### Task 5: Verify the integrated behavior and record the rollout

**Files:**
- Modify: `.Memory/2026-07-10.md`
- Modify: `.Memory/Abstract.md`

- [ ] **Step 1: Run focused result-brief and delivery coverage**

Run:

```bash
uv run python -m pytest \
  tests/test_workflow_result_brief.py \
  tests/test_workflow_renderer.py \
  tests/test_workflow_confirm.py \
  tests/test_workflow_dynamic_roles.py \
  tests/test_workflow_subagent.py \
  tests/test_workflow_subagent_ac5.py \
  tests/test_workflow_api_contract.py \
  tests/test_workflow_token_efficiency.py \
  tests/test_im_client_sanitize.py \
  -q
```

Expected: zero failures.

- [ ] **Step 2: Run the broader Workflow regression set**

Run:

```bash
uv run python -m pytest \
  tests/test_workflow_*.py \
  -q
```

Expected: zero failures; existing documented skips remain skips.

- [ ] **Step 3: Run static and configuration verification**

Run:

```bash
uv run ruff check .
uv run python -m src.main --validate
git diff --check
```

Expected:

- ruff reports `All checks passed!`.
- configuration validation succeeds; the existing empty Slock-role warning may remain.
- `git diff --check` prints no output.

- [ ] **Step 4: Update project memory with fresh evidence**

Append a detailed `WF 结果简报卡去截断` section to `.Memory/2026-07-10.md` containing:

- Root cause: 4,000/2,000-character completion rendering plus generic payload truncation.
- Behavior: typed `card_summary`, legacy normalization, complete-item budgeting, result-first card, collapsed process, full report preservation, and safe terminal fallback.
- Exact test/lint/validate outputs from Steps 1–3.
- Residual risk: prompt-produced summaries vary in quality, while deterministic fallback avoids guessing.

Add one approximately 20-character summary line with a link to `2026-07-10.md` in `.Memory/Abstract.md`.

- [ ] **Step 5: Re-run checks for the memory-only diff and commit**

Run:

```bash
git diff --check
git status --short
```

Commit:

```bash
git add .Memory/2026-07-10.md .Memory/Abstract.md
git commit -m "docs(workflow): record result brief rollout"
```

- [ ] **Step 6: Final completion audit**

Run:

```bash
git status --short
git log -6 --oneline
```

Confirm all design requirements map to evidence:

- Card contains conclusion, complete findings, verification, deliverables, next steps, and report state when present.
- Card contains no `(内容已截断)` marker and no sliced result items.
- Omitted items are counted as whole items.
- Full report retains sentinel content excluded from the card.
- Existing Workflow progress, cancellation, report upload, and late-progress behavior remains green.
- Worktree is clean after commits.
