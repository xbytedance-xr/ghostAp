"""Deep must not equate transport limits with completed user work."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.acp.models import PlanEntryInfo, PlanInfo, PromptResult
from src.deep_engine.engine import DeepEngine
from src.deep_engine.models import DeepProject, DeepProjectStatus, EngineRunState


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        coco_execution_timeout=300,
        claude_execution_timeout=600,
        deep_memory_threshold=99.0,
    )


_FOLLOW_UP_FAILURES = [
    pytest.param(
        TimeoutError("follow-up timed out"),
        "pending_context_timeout",
        id="timeout",
    ),
    pytest.param(
        RuntimeError("follow-up failed"),
        "pending_context_error",
        id="error",
    ),
]


@pytest.mark.parametrize(
    "result",
    [
        PromptResult(stop_reason="max_turn_requests"),
        PromptResult(
            stop_reason="end_turn",
            plan=PlanInfo(
                entries=[
                    PlanEntryInfo(
                        content="运行真实验收",
                        status="in_progress",
                    )
                ]
            ),
        ),
    ],
)
def test_deep_fails_when_prompt_result_still_has_unfinished_work(
    result: PromptResult,
) -> None:
    session = MagicMock()
    session.send_prompt_with_retry.return_value = result

    with (
        patch("src.engine_base.get_settings", return_value=_settings()),
        patch("src.deep_engine.engine.create_engine_session", return_value=session),
        patch("src.deep_engine.engine.get_gc_monitor") as gc_monitor,
    ):
        engine = DeepEngine(
            chat_id="chat-1",
            root_path="/repo",
            agent_type="codex",
            engine_name="Codex",
        )
        project = engine.plan_and_execute("完成所有验收项")

    assert project.status is DeepProjectStatus.FAILED
    assert project.error
    gc_monitor.assert_called()


@pytest.mark.parametrize(("follow_up_error", "expected_reason"), _FOLLOW_UP_FAILURES)
def test_deep_initial_run_preserves_pending_context_and_fails_closed(
    follow_up_error: Exception,
    expected_reason: str,
) -> None:
    session = MagicMock()
    session.send_prompt_with_retry.side_effect = [
        PromptResult(stop_reason="end_turn"),
        follow_up_error,
    ]

    with (
        patch("src.engine_base.get_settings", return_value=_settings()),
        patch("src.deep_engine.engine.create_engine_session", return_value=session),
        patch("src.deep_engine.engine.get_gc_monitor"),
    ):
        engine = DeepEngine(
            chat_id="chat-1",
            root_path="/repo",
            agent_type="codex",
            engine_name="Codex",
        )
        engine.inject_guidance("新增验收条件")
        project = engine.plan_and_execute("完成所有验收项")

    assert project.status is DeepProjectStatus.FAILED
    assert project.error == f"执行未完成: ACP 停止原因：{expected_reason}"
    assert engine._pending_context == ["新增验收条件"]
    assert session.send_prompt_with_retry.call_count == 2


@pytest.mark.parametrize(("follow_up_error", "expected_reason"), _FOLLOW_UP_FAILURES)
def test_deep_resume_preserves_pending_context_and_fails_closed(
    follow_up_error: Exception,
    expected_reason: str,
) -> None:
    session = MagicMock()
    session.send_prompt_with_retry.side_effect = [
        PromptResult(stop_reason="end_turn"),
        follow_up_error,
    ]

    with (
        patch("src.engine_base.get_settings", return_value=_settings()),
        patch("src.deep_engine.engine.create_engine_session", return_value=session),
        patch("src.deep_engine.engine.get_gc_monitor"),
    ):
        engine = DeepEngine(
            chat_id="chat-1",
            root_path="/repo",
            agent_type="codex",
            engine_name="Codex",
        )
        engine._project = DeepProject.create(name="repo", root_path="/repo")
        engine._project.pause()
        engine.inject_guidance("恢复时新增的验收条件")
        project = engine.resume()

    assert project is not None
    assert project.status is DeepProjectStatus.FAILED
    assert project.error == f"执行未完成: ACP 停止原因：{expected_reason}"
    assert engine._pending_context == ["恢复时新增的验收条件"]
    assert session.send_prompt_with_retry.call_count == 2


@pytest.mark.parametrize(
    "follow_up_result",
    [
        PromptResult(stop_reason="timeout"),
        PromptResult(stop_reason="cancelled"),
        PromptResult(stop_reason="failed"),
        PromptResult(
            stop_reason="end_turn",
            plan=PlanInfo(
                entries=[
                    PlanEntryInfo(
                        content="完成新增验收条件",
                        status="in_progress",
                    )
                ]
            ),
        ),
    ],
)
def test_deep_preserves_pending_context_when_follow_up_returns_incomplete(
    follow_up_result: PromptResult,
) -> None:
    """Return-shaped backend failures must not consume injected guidance."""
    session = MagicMock()
    session.send_prompt_with_retry.return_value = follow_up_result

    with patch("src.engine_base.get_settings", return_value=_settings()):
        engine = DeepEngine(
            chat_id="chat-1",
            root_path="/repo",
            agent_type="codex",
            engine_name="Codex",
        )
    engine._run_state = EngineRunState.RUNNING
    engine._session = session
    engine._pending_context = ["不能丢失的新增验收条件"]

    result = engine._drain_pending_context(
        on_event=lambda _event: None,
        timeout=10,
        last_result=PromptResult(stop_reason="end_turn"),
    )

    assert result is follow_up_result
    assert engine._pending_context == ["不能丢失的新增验收条件"]
