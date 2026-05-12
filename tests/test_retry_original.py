from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.feishu.retry_original import RetryDecision, RetryDecisionStatus, RetryOriginalModeUseCase


class _Gateway:
    def __init__(self, decision) -> None:
        self.decision = decision
        self.calls: list[dict] = []

    def retry(self, *, message_id: str, chat_id: str, project_id: str | None, payload: dict):
        self.calls.append({"message_id": message_id, "chat_id": chat_id, "project_id": project_id, "payload": payload})
        return self.decision


def test_retry_original_without_gateway_returns_manual_retry_feedback() -> None:
    use_case = RetryOriginalModeUseCase()

    decision = use_case(
        "m1",
        "c1",
        "p1",
        {"original_mode": "Claude CLI", "degraded_to": "Coco", "retry_mode": "Claude CLI"},
    )

    assert decision.status is RetryDecisionStatus.MANUAL_REQUIRED
    assert decision.message == "已收到重试请求，但当前卡片无法安全自动恢复 Claude CLI。请重新发送原命令、查看诊断，或在卡片存在可继续模式时使用该模式。"


def test_retry_original_missing_mode_returns_context_missing_feedback() -> None:
    use_case = RetryOriginalModeUseCase()

    decision = use_case("m1", "c1", None, {})

    assert decision.status is RetryDecisionStatus.CONTEXT_MISSING
    assert decision.message == "当前降级卡缺少可自动重试的原模式上下文，请重新发送原命令或查看诊断。"


def test_retry_original_uses_gateway_decision_when_available() -> None:
    gateway = _Gateway(RetryOriginalModeUseCase.accepted("Claude CLI"))
    use_case = RetryOriginalModeUseCase(gateway=gateway)

    decision = use_case(
        "m1",
        "c1",
        "p1",
        {"original_mode": "Claude CLI", "degraded_to": "Coco", "retry_mode": "Claude CLI", "request_id": "req-1"},
    )

    assert decision.status is RetryDecisionStatus.ACCEPTED
    assert gateway.calls == [
        {
            "message_id": "m1",
            "chat_id": "c1",
            "project_id": "p1",
            "payload": {
                "original_mode": "Claude CLI",
                "degraded_to": "Coco",
                "retry_mode": "Claude CLI",
                "request_id": "req-1",
            },
        }
    ]
    assert decision.message == "🔄 已开始重试原模式：Claude CLI。请继续关注当前会话反馈。"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (RetryDecisionStatus.NOT_RETRYABLE, "当前模式暂不支持自动重试，请重新发送原命令。"),
        (RetryDecisionStatus.COOLDOWN, "原模式正在冷却中，请稍后重试、查看诊断或使用卡片上的可继续模式。"),
        (RetryDecisionStatus.DUPLICATE, "已收到相同重试请求，请勿重复点击。"),
        (RetryDecisionStatus.ACCEPTED, "🔄 已开始重试原模式：Claude。请继续关注当前会话反馈。"),
        (RetryDecisionStatus.MANUAL_REQUIRED, "请重新发送原命令以手动恢复 Claude。"),
    ],
)
def test_retry_original_gateway_decision_statuses_return_user_feedback(status, message) -> None:
    gateway = _Gateway(RetryDecision(status=status, mode="Claude", message=message))
    use_case = RetryOriginalModeUseCase(gateway=gateway)

    decision = use_case(
        "m1",
        "c1",
        "p1",
        {"original_mode": "Claude", "degraded_to": "Coco", "retry_mode": "Claude"},
    )

    assert decision.status is status
    assert decision.message == message


def test_retry_original_use_case_returns_decision_without_reply_side_effect() -> None:
    use_case = RetryOriginalModeUseCase()

    decision = use_case(
        "m1",
        "c1",
        "p1",
        {"original_mode": "Claude", "degraded_to": "Aiden", "retry_mode": "Claude"},
    )

    assert isinstance(decision, RetryDecision)
    assert decision.status is RetryDecisionStatus.MANUAL_REQUIRED


def test_retry_original_use_case_has_no_feishu_client_private_adapter() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "feishu" / "retry_original.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    private_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_")
    }

    assert "ClientReplyPort" not in class_names
    assert "_reply_text" not in source
    assert "_client" not in private_attrs


@pytest.mark.parametrize(
    "payload",
    [
        {"degraded_to": "Coco", "retry_mode": "Claude"},
        {"original_mode": "Claude", "retry_mode": "Claude"},
        {"original_mode": "Claude", "degraded_to": "Coco"},
        {"original_mode": "Claude", "degraded_to": "", "retry_mode": "Claude"},
        {"original_mode": "Claude", "degraded_to": "Coco", "retry_mode": "rm -rf /"},
        {"original_mode": "Coco", "degraded_to": "Coco", "retry_mode": "Claude"},
        {"original_mode": "Unknown", "degraded_to": "Coco", "retry_mode": "Unknown"},
    ],
)
def test_retry_original_payload_schema_rejects_missing_invalid_or_confused_modes(payload: dict) -> None:
    from src.feishu.retry_original import validate_retry_original_payload

    assert validate_retry_original_payload(payload) is None


def test_retry_original_payload_schema_accepts_explicit_original_degraded_and_retry_modes() -> None:
    from src.feishu.retry_original import RetryOriginalPayload, validate_retry_original_payload

    result = validate_retry_original_payload(
        {"original_mode": "Claude", "degraded_to": "Coco", "retry_mode": "Claude", "request_id": "req-1"}
    )

    assert result == RetryOriginalPayload(original_mode="Claude", degraded_to="Coco", retry_mode="Claude")
