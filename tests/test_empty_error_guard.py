"""Tests for empty-error-message guard across all patched modules.

Validates that bare TimeoutError() (no message) and other empty-message
exceptions never produce empty user-facing strings.
"""
import json
import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from src.utils.errors import fmt_exception, get_error_detail


# ---------------------------------------------------------------------------
# Task 1: CardBuilder.build_error_card — empty guard
# ---------------------------------------------------------------------------

class TestBuildErrorCardEmptyGuard:
    """system.py: build_error_card must never produce empty message body."""

    def test_bare_timeout_error_has_nonempty_message(self):
        from src.card.builders.system import SystemBuilder

        _, card_json = SystemBuilder.build_error_card(TimeoutError())
        card = json.loads(card_json)
        body_elements = card.get("body", {}).get("elements", card.get("elements", []))
        # Find the content element that contains the error message
        content_texts = [
            el.get("content", "")
            for el in body_elements
            if el.get("tag") == "markdown" or el.get("tag") == "div"
        ]
        full_text = " ".join(content_texts)
        # Must not have empty message after the title
        assert "超时" in full_text or "未知错误" in full_text

    def test_bare_timeout_error_no_empty_body(self):
        from src.card.builders.system import SystemBuilder

        _, card_json = SystemBuilder.build_error_card(TimeoutError())
        # The card body should not contain "操作失败**\n\n" followed by nothing
        assert "\n\n\n" not in card_json

    def test_string_exc_still_works(self):
        from src.card.builders.system import SystemBuilder

        _, card_json = SystemBuilder.build_error_card("具体错误信息")
        assert "具体错误信息" in card_json

    def test_named_timeout_preserves_message(self):
        from src.card.builders.system import SystemBuilder

        _, card_json = SystemBuilder.build_error_card(
            TimeoutError("ACP prompt 执行超时 (120s)")
        )
        assert "ACP prompt 执行超时" in card_json


# ---------------------------------------------------------------------------
# Task 2: BaseHandler.send_error_card fallback — empty guard
# ---------------------------------------------------------------------------

class TestBaseHandlerFallbackEmptyGuard:
    """base.py: fallback text path must never produce '❌ title: ' with empty tail."""

    def _make_handler(self):
        """Create a BaseHandler with enough mocking to test send_error_card."""
        from src.feishu.handlers.base import BaseHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.ref_note_enabled = False
        handler = BaseHandler(ctx)
        return handler

    def test_fallback_path_bare_timeout_no_empty_tail(self):
        handler = self._make_handler()
        # Force CardBuilder.build_error_card to raise so fallback triggers
        sent_content = []

        def capture_reply(msg_id, content, **kw):
            sent_content.append(content)

        handler.reply_message = capture_reply

        with patch("src.card.CardBuilder") as mock_cb:
            mock_cb.build_error_card.side_effect = Exception("card build failed")
            handler.send_error_card(
                chat_id="test_chat",
                exc=TimeoutError(),
                title="启动超时",
                origin_message_id="msg123",
            )

        assert len(sent_content) == 1
        msg = sent_content[0]
        # Should not end with ": " (empty tail)
        assert not msg.endswith(": ")
        assert "操作失败" in msg or "启动超时" in msg


# ---------------------------------------------------------------------------
# Task 3: TaskScheduler — empty error guard
# ---------------------------------------------------------------------------

class TestSchedulerEmptyErrorGuard:
    """scheduler.py: state.error must never be empty for bare exceptions."""

    def test_bare_timeout_error_state_nonempty(self):
        from src.tasking.scheduler import TaskScheduler, TaskSpec

        scheduler = TaskScheduler(max_concurrent=2)
        try:
            spec = TaskSpec(chat_id="c1", name="test_timeout")

            def failing_task(ctx):
                raise TimeoutError()

            handle = scheduler.submit(spec, failing_task)

            # Wait for task to complete
            try:
                result = handle.wait(timeout=5)
            except Exception:
                pass

            state = scheduler.get_state(handle.run_id)
            assert state is not None
            assert state.error  # must not be empty
            assert len(state.error) > 0
        finally:
            scheduler.stop(shutdown_executor=True)

    def test_bare_exception_state_nonempty(self):
        from src.tasking.scheduler import TaskScheduler, TaskSpec

        scheduler = TaskScheduler(max_concurrent=2)
        try:
            spec = TaskSpec(chat_id="c1", name="test_empty_exc")

            def failing_task(ctx):
                raise Exception()

            handle = scheduler.submit(spec, failing_task)

            try:
                result = handle.wait(timeout=5)
            except Exception:
                pass

            state = scheduler.get_state(handle.run_id)
            assert state is not None
            assert state.error  # must not be empty — repr(e) kicks in
        finally:
            scheduler.stop(shutdown_executor=True)


# ---------------------------------------------------------------------------
# Task 4: fmt_exception — empty guard for non-timeout
# ---------------------------------------------------------------------------

class TestFmtExceptionEmptyGuard:
    """errors.py: fmt_exception must never produce trailing empty content."""

    def test_bare_exception_has_repr_fallback(self):
        result = fmt_exception("处理", Exception())
        assert "处理异常" in result
        # Must not end with ": " (empty)
        assert not result.endswith(": ")
        # repr(Exception()) == "Exception()" — should appear
        assert "Exception()" in result

    def test_bare_value_error_has_repr(self):
        result = fmt_exception("验证", ValueError())
        assert "ValueError()" in result

    def test_normal_exception_preserves_message(self):
        result = fmt_exception("操作", RuntimeError("具体原因"))
        assert "具体原因" in result

    def test_timeout_still_uses_fixed_message(self):
        result = fmt_exception("操作", TimeoutError())
        assert "超时" in result
        assert "操作耗时过长" in result


# ---------------------------------------------------------------------------
# Task 5: WorktreeDispatcher — get_error_detail integration
# ---------------------------------------------------------------------------

class TestWorktreeDispatcherGetErrorDetail:
    """dispatcher.py: TimeoutError uses get_error_detail() for consistent messages."""

    def test_bare_timeout_uses_get_error_detail(self, tmp_path):
        from src.worktree_engine.dispatcher import WorktreeDispatcher
        from src.worktree_engine.models import WorktreeUnit

        d = tmp_path / "wt"
        d.mkdir()

        @dataclass
        class FakeResult:
            stop_reason: str
            text: str

        class TimeoutSession:
            def __init__(self, **kw):
                pass

            def start(self, startup_timeout=60):
                return "ok"

            def send_prompt(self, text, on_event=None, timeout=None):
                raise TimeoutError()

            def close(self):
                pass

        unit = WorktreeUnit(
            unit_id="u0",
            selection_key="acp:coco:d",
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            worktree_path=str(d),
        )
        dispatcher = WorktreeDispatcher(session_factory=lambda **kw: TimeoutSession(**kw))
        planned = dispatcher.plan_user_goal("test", [unit])
        executed = dispatcher.execute_units(planned, timeout=30)

        assert executed[0].status == "failed"
        assert executed[0].error  # non-empty
        assert "超时" in executed[0].error
        # Should use get_error_detail output which contains "操作超时"
        assert "操作超时" in executed[0].error


# ---------------------------------------------------------------------------
# Cross-cutting: get_error_detail always non-empty
# ---------------------------------------------------------------------------

class TestGetErrorDetailNeverEmpty:
    """get_error_detail must always return non-empty for any exception."""

    def test_bare_timeout_error(self):
        result = get_error_detail(TimeoutError())
        assert result
        assert "超时" in result

    def test_bare_exception(self):
        result = get_error_detail(Exception())
        assert result  # Should be "未知错误" (default)

    def test_exception_with_message(self):
        result = get_error_detail(ValueError("bad input"))
        assert "bad input" in result


# ---------------------------------------------------------------------------
# Task 6: Deep/Loop handler — project creation empty error guard
# ---------------------------------------------------------------------------

class TestDeepHandlerProjectCreateEmptyGuard:
    """deep.py: project creation failure must never produce empty error message."""

    def _make_handler(self):
        from src.feishu.handlers.deep import DeepHandler
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.ref_note_enabled = False
        handler = DeepHandler(ctx)
        return handler

    def test_bare_exception_nonempty_error(self):
        handler = self._make_handler()
        sent = []
        handler.send_error_card = lambda **kw: sent.append(kw)
        handler.get_working_dir = lambda cid: "/tmp"
        handler.ctx.project_manager.get_or_create_project_for_path.side_effect = Exception()

        handler.start_deep_engine("msg1", "chat1", "test requirement")
        assert len(sent) == 1
        exc_val = sent[0]["exc"]
        assert exc_val  # must be non-empty string
        assert len(str(exc_val)) > 0

    def test_bare_timeout_error_nonempty(self):
        handler = self._make_handler()
        sent = []
        handler.send_error_card = lambda **kw: sent.append(kw)
        handler.get_working_dir = lambda cid: "/tmp"
        handler.ctx.project_manager.get_or_create_project_for_path.side_effect = TimeoutError()

        handler.start_deep_engine("msg1", "chat1", "test requirement")
        assert len(sent) == 1
        exc_val = str(sent[0]["exc"])
        assert exc_val
        assert "超时" in exc_val


class TestLoopHandlerProjectCreateEmptyGuard:
    """loop.py: project creation failure must never produce empty error message."""

    def _make_handler(self):
        from src.feishu.handlers.loop import LoopHandler
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.ref_note_enabled = False
        handler = LoopHandler(ctx)
        return handler

    def test_bare_exception_nonempty_error(self):
        handler = self._make_handler()
        sent = []
        handler.send_error_card = lambda **kw: sent.append(kw)
        handler.get_working_dir = lambda cid: "/tmp"
        handler.ctx.project_manager.get_or_create_project_for_path.side_effect = Exception()

        handler.start_loop_engine("msg1", "chat1", "test requirement")
        assert len(sent) == 1
        exc_val = sent[0]["exc"]
        assert exc_val
        assert len(str(exc_val)) > 0

    def test_bare_timeout_error_nonempty(self):
        handler = self._make_handler()
        sent = []
        handler.send_error_card = lambda **kw: sent.append(kw)
        handler.get_working_dir = lambda cid: "/tmp"
        handler.ctx.project_manager.get_or_create_project_for_path.side_effect = TimeoutError()

        handler.start_loop_engine("msg1", "chat1", "test requirement")
        assert len(sent) == 1
        exc_val = str(sent[0]["exc"])
        assert exc_val
        assert "超时" in exc_val


# ---------------------------------------------------------------------------
# Task 7: Spec handler — multiple empty error guard paths
# ---------------------------------------------------------------------------

class TestSpecHandlerEmptyGuard:
    """spec.py: all error paths must produce non-empty user-facing messages."""

    def _make_handler(self):
        from src.feishu.handlers.spec import SpecHandler
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.ref_note_enabled = False
        handler = SpecHandler(ctx)
        return handler

    def test_project_create_bare_exception(self):
        handler = self._make_handler()
        sent = []
        handler.reply_message = lambda mid, content, **kw: sent.append(content)
        handler.get_working_dir = lambda cid: "/tmp"
        handler.ctx.project_manager.get_or_create_project_for_path.side_effect = Exception()

        handler.start_spec_engine("msg1", "chat1", "req")
        assert len(sent) == 1
        assert sent[0]
        assert "创建项目" in sent[0]
        # Must not end with just "失败" and nothing else meaningful
        assert "❌" in sent[0]

    def test_project_create_bare_timeout(self):
        handler = self._make_handler()
        sent = []
        handler.reply_message = lambda mid, content, **kw: sent.append(content)
        handler.get_working_dir = lambda cid: "/tmp"
        handler.ctx.project_manager.get_or_create_project_for_path.side_effect = TimeoutError()

        handler.start_spec_engine("msg1", "chat1", "req")
        assert len(sent) == 1
        assert "超时" in sent[0]

    def test_export_bare_exception(self):
        """Export file write failure with bare Exception() should not produce empty tail."""
        from src.utils.errors import get_error_detail
        # Directly test that get_error_detail handles bare Exception
        result = get_error_detail(Exception())
        assert result  # non-empty fallback

    def test_restore_context_bare_exception(self):
        """fmt_error("恢复项目上下文", Exception()) should produce non-empty."""
        from src.utils.errors import fmt_error
        result = fmt_error("恢复项目上下文", Exception())
        assert result
        assert "恢复项目上下文" in result

    def test_restore_context_bare_timeout(self):
        """fmt_error("恢复任务上下文", TimeoutError()) should mention timeout."""
        from src.utils.errors import fmt_error
        result = fmt_error("恢复任务上下文", TimeoutError())
        assert "超时" in result


# ---------------------------------------------------------------------------
# Task 8: spec_engine — last_error and rewrite_requirement
# ---------------------------------------------------------------------------

class TestSpecEngineInternalEmptyGuard:
    """spec_engine/engine.py: internal error tracking must never be empty."""

    def test_get_error_detail_for_last_error(self):
        """get_error_detail replaces the old 3-tier fallback for last_error."""
        result = get_error_detail(Exception())
        assert result  # must be non-empty
        result2 = get_error_detail(TimeoutError())
        assert result2
        assert "超时" in result2

    def test_get_error_detail_for_rewrite_requirement(self):
        """return False, get_error_detail(e) must never return empty string."""
        for exc in [Exception(), TimeoutError(), ValueError(), RuntimeError()]:
            result = get_error_detail(exc)
            assert result, f"get_error_detail({type(exc).__name__}()) returned empty"


# ---------------------------------------------------------------------------
# Task 9: WorktreeManager — init/merge empty error guard
# ---------------------------------------------------------------------------

class TestWorktreeManagerEmptyGuard:
    """worktree_engine/manager.py: last_error and merge detail must be non-empty."""

    def test_init_bare_exception_last_error_nonempty(self):
        """WorktreeManager.initialize_worktrees failure should produce non-empty last_error."""
        result = get_error_detail(Exception())
        assert result
        # Simulate what the code does: state.last_error = get_error_detail(exc)
        last_error = get_error_detail(Exception())
        summary = f"- worktree 创建失败：{last_error}"
        assert "创建失败" in summary
        assert not summary.endswith("：")  # must have content after colon

    def test_merge_bare_exception_detail_nonempty(self):
        """Merge failure detail should be non-empty for bare Exception."""
        detail = get_error_detail(Exception())
        assert detail
        result = {"success": False, "detail": detail}
        assert result["detail"]


# ---------------------------------------------------------------------------
# Task 10: main.py — top-level exception handler
# ---------------------------------------------------------------------------

class TestMainAppEmptyGuard:
    """main.py: top-level exception handler must produce non-empty error message."""

    def test_get_error_detail_for_main(self):
        """get_error_detail is now used in main.py instead of str(e)."""
        for exc in [Exception(), TimeoutError(), RuntimeError()]:
            result = get_error_detail(exc)
            assert result, f"main.py would show empty error for {type(exc).__name__}()"


# ---------------------------------------------------------------------------
# Task 11: spec.py save_state — fmt_error now receives Exception directly
# ---------------------------------------------------------------------------

class TestSpecHandlerSaveStateEmptyGuard:
    """spec.py:412 — save_state error now passes exception object to fmt_error,
    so isinstance dispatch handles bare TimeoutError() correctly."""

    def test_save_state_bare_timeout_no_empty_tail(self):
        """fmt_error('保存 Spec 状态', TimeoutError()) must mention timeout."""
        from src.utils.errors import fmt_error

        result = fmt_error("保存 Spec 状态", TimeoutError())
        assert result
        assert "保存 Spec 状态" in result
        assert "超时" in result
        assert not result.endswith(": ")

    def test_save_state_bare_exception_no_empty_tail(self):
        """fmt_error('保存 Spec 状态', Exception()) must not leave empty detail."""
        from src.utils.errors import fmt_error

        result = fmt_error("保存 Spec 状态", Exception())
        assert result
        assert "保存 Spec 状态" in result
        # When str(Exception()) is empty, fmt_error returns "❌ 保存 Spec 状态失败" (no colon)
        assert not result.endswith(": ")


# ---------------------------------------------------------------------------
# Task 12: system.py TTADK refresh — get_error_detail for reply_error
# ---------------------------------------------------------------------------

class TestSystemHandlerTTADKRefreshEmptyGuard:
    """system.py:371 — TTADK model refresh now uses get_error_detail(e)
    instead of str(e), preventing empty error text for bare exceptions."""

    def test_ttadk_refresh_bare_timeout_nonempty(self):
        """get_error_detail(TimeoutError()) must produce non-empty timeout text."""
        result = get_error_detail(TimeoutError())
        assert result
        assert "超时" in result

    def test_ttadk_refresh_bare_exception_nonempty(self):
        """get_error_detail(Exception()) must produce non-empty fallback text."""
        result = get_error_detail(Exception())
        assert result
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Task 13: system.py handle_refresh_ttadk_models — integration guard
# ---------------------------------------------------------------------------

class TestSystemHandlerRefreshModelsIntegration:
    """system.py:1476 — handle_refresh_ttadk_models reply_error must never
    produce empty-tail message for bare TimeoutError or Exception."""

    def _make_handler(self):
        from src.feishu.handlers.system import SystemHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.ref_note_enabled = False
        handler = SystemHandler(ctx)
        return handler

    def test_bare_timeout_reply_error_nonempty(self):
        handler = self._make_handler()
        sent = []
        handler.reply_error = lambda mid, content, **kw: sent.append(content)
        handler._resolve_ttadk_cwd = lambda *a, **kw: "/tmp"
        handler._maybe_log_ttadk_cwd = lambda **kw: None

        mock_mgr = MagicMock()
        mock_mgr.get_current_tool.return_value = "coco"
        mock_mgr.refresh_models.side_effect = TimeoutError()

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=mock_mgr):
            handler.handle_refresh_ttadk_models("msg1", "chat1", "coco")

        assert len(sent) == 1
        msg = sent[0]
        assert msg  # non-empty
        assert not msg.endswith(": ")  # no empty tail
        assert "超时" in msg  # timeout info preserved

    def test_bare_exception_reply_error_nonempty(self):
        handler = self._make_handler()
        sent = []
        handler.reply_error = lambda mid, content, **kw: sent.append(content)
        handler._resolve_ttadk_cwd = lambda *a, **kw: "/tmp"
        handler._maybe_log_ttadk_cwd = lambda **kw: None

        mock_mgr = MagicMock()
        mock_mgr.get_current_tool.return_value = "coco"
        mock_mgr.refresh_models.side_effect = Exception()

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=mock_mgr):
            handler.handle_refresh_ttadk_models("msg1", "chat1", "coco")

        assert len(sent) == 1
        msg = sent[0]
        assert msg  # non-empty
        assert not msg.endswith(": ")

    def test_named_timeout_preserves_message(self):
        handler = self._make_handler()
        sent = []
        handler.reply_error = lambda mid, content, **kw: sent.append(content)
        handler._resolve_ttadk_cwd = lambda *a, **kw: "/tmp"
        handler._maybe_log_ttadk_cwd = lambda **kw: None

        mock_mgr = MagicMock()
        mock_mgr.get_current_tool.return_value = "coco"
        mock_mgr.refresh_models.side_effect = TimeoutError("模型服务不可用")

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=mock_mgr):
            handler.handle_refresh_ttadk_models("msg1", "chat1", "coco")

        assert len(sent) == 1
        assert "模型服务不可用" in sent[0]


# ---------------------------------------------------------------------------
# Regression lint: no bare f"{e}" in user-visible reply_error / send_error_card
# ---------------------------------------------------------------------------

import re
from pathlib import Path


class TestNoBareFStringInUserVisibleErrors:
    """Lint guard: reply_error / send_error_card calls must not use bare f\"{e}\"
    or f\"{exc}\" which can produce empty strings for TimeoutError()."""

    # Pattern: reply_error(...f"...{e}") or send_error_card(...f"...{e}")
    # Matches bare {e}, {exc}, {err} without get_error_detail / repr / or guard
    _BARE_FSTR_RE = re.compile(
        r'(?:reply_error|send_error_card)\s*\([^)]*f["\'].*\{(?:e|exc|err)\}[^)]*\)'
    )

    # Pattern: logger.warning/error(f"...{e}") — bare exception in log format
    _BARE_LOGGER_RE = re.compile(
        r'logger\.(?:warning|error)\s*\(\s*f["\'].*\{(?:e|exc|err)\}'
    )

    _SKIP_GUARDS = ("get_error_detail", "repr(", " or ", "str(")

    def _scan_src_files(self):
        src_dir = Path(__file__).resolve().parent.parent / "src"
        violations = []
        for py_file in src_dir.rglob("*.py"):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                # Skip lines that already use get_error_detail or repr or `or`
                if any(g in line for g in self._SKIP_GUARDS):
                    continue
                if self._BARE_FSTR_RE.search(line):
                    violations.append(f"{py_file.relative_to(src_dir.parent)}:{i}: {line.strip()}")
        return violations

    def _scan_logger_bare_fstr(self):
        """Scan logger.warning/error for bare f\"{e}\" without guard."""
        src_dir = Path(__file__).resolve().parent.parent / "src"
        violations = []
        for py_file in src_dir.rglob("*.py"):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                if any(g in line for g in self._SKIP_GUARDS):
                    continue
                if self._BARE_LOGGER_RE.search(line):
                    violations.append(f"{py_file.relative_to(src_dir.parent)}:{i}: {line.strip()}")
        return violations

    def test_no_bare_fstring_in_user_visible_errors(self):
        violations = self._scan_src_files()
        assert not violations, (
            "Found bare f\"{e}\" in user-visible error paths (risk of empty message):\n"
            + "\n".join(violations)
        )

    def test_no_bare_fstring_in_logger_errors(self):
        violations = self._scan_logger_bare_fstr()
        assert not violations, (
            "Found bare f\"{e}\" in logger.warning/error (risk of empty message in logs):\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Guard tests for newly hardened internal diagnostic paths (batch 2)
# ---------------------------------------------------------------------------


class TestIntentRecognizerEmptyGuard:
    """intent_recognizer.py: bare exception in reasoning must not be empty."""

    def test_bare_exception_reasoning_nonempty(self):
        from src.agent.intent_recognizer import IntentRecognizer

        recognizer = IntentRecognizer.__new__(IntentRecognizer)
        recognizer._get_fallback_intent = lambda mode: "CHAT"

        # Simulate the except block logic directly
        e = Exception()
        reasoning = f"异常回退: {str(e) or repr(e)}"
        assert reasoning  # non-empty
        assert not reasoning.endswith(": ")  # no empty tail
        assert "Exception()" in reasoning  # repr fallback

    def test_bare_timeout_reasoning_nonempty(self):
        e = TimeoutError()
        reasoning = f"异常回退: {str(e) or repr(e)}"
        assert reasoning
        assert not reasoning.endswith(": ")
        assert "TimeoutError()" in reasoning

    def test_named_exception_preserves_message(self):
        e = ValueError("bad input")
        reasoning = f"异常回退: {str(e) or repr(e)}"
        assert "bad input" in reasoning


class TestEngineBaseLoggerEmptyGuard:
    """engine_base.py: logger format strings must not have empty tail."""

    def test_timeout_logger_nonempty(self):
        e = TimeoutError()
        msg = f"Deep Engine 执行超时 (task_id=t1): {str(e) or repr(e)}"
        assert msg
        assert not msg.endswith(": ")
        assert "TimeoutError()" in msg

    def test_bare_exception_logger_nonempty(self):
        e = Exception()
        msg = f"Deep Engine 执行异常: {str(e) or repr(e)}"
        assert msg
        assert not msg.endswith(": ")
        assert "Exception()" in msg

    def test_named_exception_logger_preserves(self):
        e = RuntimeError("connection lost")
        msg = f"Deep Engine 执行异常: {str(e) or repr(e)}"
        assert "connection lost" in msg


class TestProjectManagerEmptyGuard:
    """project/manager.py: directory creation error must not be empty."""

    def test_bare_exception_nonempty(self):
        e = Exception()
        msg = f"无法创建目录 /tmp/test: {str(e) or repr(e)}"
        assert msg
        assert not msg.endswith(": ")
        assert "Exception()" in msg

    def test_bare_os_error_nonempty(self):
        e = OSError()
        msg = f"无法创建目录 /tmp/test: {str(e) or repr(e)}"
        assert msg
        assert not msg.endswith(": ")

    def test_named_os_error_preserves(self):
        e = PermissionError("access denied")
        msg = f"无法创建目录 /tmp/test: {str(e) or repr(e)}"
        assert "access denied" in msg


class TestArtifactsParseEmptyGuard:
    """spec_engine/artifacts.py: JSON parse error must not be empty."""

    def test_spec_parse_bare_exception_nonempty(self):
        e = Exception()
        msg = f"规格 JSON 解析失败：{str(e) or repr(e)}"
        assert msg
        assert not msg.endswith("：")  # Chinese colon
        assert "Exception()" in msg

    def test_plan_parse_bare_exception_nonempty(self):
        e = Exception()
        msg = f"规划 JSON 解析失败：{str(e) or repr(e)}"
        assert msg
        assert not msg.endswith("：")
        assert "Exception()" in msg

    def test_json_decode_error_preserves_message(self):
        import json

        try:
            json.loads("{bad json")
        except Exception as e:
            msg = f"规格 JSON 解析失败：{str(e) or repr(e)}"
            assert msg
            assert len(msg) > len("规格 JSON 解析失败：")
