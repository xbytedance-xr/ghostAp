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
