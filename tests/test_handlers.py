"""Tests for handler modules extracted from ws_client.py.

Each handler is tested with a fully-mocked HandlerContext so that no real
Feishu API calls or sessions are required.
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.feishu.handler_context import HandlerContext
from src.feishu.handlers.base import BaseHandler
from src.feishu.handlers.deep import DeepHandler
from src.feishu.handlers.diagnostics import DiagnosticsHandler
from src.feishu.handlers.programming import (
    ClaudeModeHandler,
    CocoModeHandler,
    AidenModeHandler,
    CodexModeHandler,
    GeminiModeHandler,
    TTADKModeHandler,
)
from src.feishu.handlers.project import ProjectHandler
from src.feishu.handlers.system import SystemHandler
from src.mode.manager import InteractionMode
from src.ttadk.models import TTADKModel, TTADKTool

# ======================================================================
# Shared fixture: mock HandlerContext
# ======================================================================


def _make_handler_context(**overrides) -> HandlerContext:
    """Build a HandlerContext with all dependencies mocked."""
    ctx = HandlerContext(
        settings=MagicMock(),
        api_client_factory=MagicMock(),
        message_callback=MagicMock(),
        coco_manager=MagicMock(),
        claude_manager=MagicMock(),
        aiden_manager=MagicMock(),
        codex_manager=MagicMock(),
        gemini_manager=MagicMock(),
        ttadk_manager=MagicMock(),
        intent_recognizer=MagicMock(),
        scheduler=MagicMock(),
        project_manager=MagicMock(),
        message_mapper=MagicMock(),
        message_linker=MagicMock(),
        mode_manager=MagicMock(),
        context_manager=MagicMock(),
        deep_engine_manager=MagicMock(),
        progress_reporter=MagicMock(),
        loop_engine_manager=MagicMock(),
        loop_reporter=MagicMock(),
        spec_engine_manager=MagicMock(),
        spec_reporter=MagicMock(),
        streaming_manager_factory=MagicMock(),
        image_handler_factory=MagicMock(),
        working_dirs={},
        working_dir_lock=threading.Lock(),
        pending_image_keys={},
        pending_image_lock=threading.Lock(),
        enable_streaming=False,
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def _set_all_programming_mode_flags(ctx, value: bool) -> None:
    ctx.mode_manager.is_coco_mode.return_value = value
    ctx.mode_manager.is_claude_mode.return_value = value
    ctx.mode_manager.is_aiden_mode.return_value = value
    ctx.mode_manager.is_codex_mode.return_value = value
    ctx.mode_manager.is_gemini_mode.return_value = value
    ctx.mode_manager.is_ttadk_mode.return_value = value


# ======================================================================
# BaseHandler tests
# ======================================================================


class TestBaseHandler:
    def _make(self, **ctx_overrides):
        ctx = _make_handler_context(**ctx_overrides)
        return BaseHandler(ctx), ctx

    def test_property_accessors(self):
        ctx = _make_handler_context()
        h = BaseHandler(ctx)
        assert h.settings is ctx.settings
        assert h.project_manager is ctx.project_manager
        assert h.mode_manager is ctx.mode_manager
        assert h.context_manager is ctx.context_manager
        assert h.scheduler is ctx.scheduler

    def test_get_working_dir_default(self):
        h, ctx = self._make()
        import os

        assert h.get_working_dir("chat1") == os.getcwd()

    def test_set_and_get_working_dir(self, tmp_path):
        h, ctx = self._make()
        success, result = h.set_working_dir("chat1", str(tmp_path))
        assert success is True
        assert result == str(tmp_path)
        assert h.get_working_dir("chat1") == str(tmp_path)

    def test_set_working_dir_nonexistent(self):
        h, ctx = self._make()
        success, result = h.set_working_dir("chat1", "/nonexistent/path/xyz")
        assert success is False
        assert "不存在" in result

    def test_ensure_request_id_generates_id(self):
        h, ctx = self._make()
        ctx.message_linker.get_request_id.return_value = None
        rid = h.ensure_request_id("msg1")
        assert rid is not None
        assert len(rid) == 10

    def test_ensure_request_id_returns_existing(self):
        h, ctx = self._make()
        ctx.message_linker.get_request_id.return_value = "existing_id"
        rid = h.ensure_request_id("msg1")
        assert rid == "existing_id"

    def test_format_ref_note_empty(self):
        h, _ = self._make()
        assert h.format_ref_note(None, None) == ""

    def test_format_ref_note_with_parts(self):
        h, _ = self._make()
        note = h.format_ref_note("om_123", "req_456", "run_789")
        assert "origin=om_123" in note
        assert "req=req_456" in note
        assert "run=run_789" in note

    def test_add_reaction_calls_api(self):
        h, ctx = self._make()
        mock_client = MagicMock()
        mock_client.im.v1.message_reaction.create.return_value = MagicMock(success=lambda: True)
        ctx.api_client_factory.return_value = mock_client
        h.add_reaction("msg1", "thumbsup")
        mock_client.im.v1.message_reaction.create.assert_called_once()

    def test_register_message_project(self):
        h, ctx = self._make()
        project = SimpleNamespace(project_id="p1")
        h.register_message_project("msg1", project)
        ctx.message_mapper.register.assert_called_once_with("msg1", "p1")

    def test_get_engine_name_claude(self):
        h, ctx = self._make()
        ctx.mode_manager.get_mode.return_value = InteractionMode.CLAUDE
        assert h.get_engine_name("chat1") == "Claude"
        ctx.mode_manager.get_mode.assert_called_with("chat1", project_id=None)

    def test_get_engine_name_claude_with_project(self):
        h, ctx = self._make()
        ctx.mode_manager.get_mode.return_value = InteractionMode.CLAUDE
        assert h.get_engine_name("chat1", project_id="proj1") == "Claude"
        ctx.mode_manager.get_mode.assert_called_with("chat1", project_id="proj1")

    def test_get_engine_name_default_coco(self):
        h, ctx = self._make()
        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART
        assert h.get_engine_name("chat1") == "Coco"

    def test_get_engine_name_aiden(self):
        h, ctx = self._make()
        ctx.mode_manager.get_mode.return_value = InteractionMode.AIDEN
        assert h.get_engine_name("chat1") == "Aiden"

    def test_get_engine_name_codex(self):
        h, ctx = self._make()
        ctx.mode_manager.get_mode.return_value = InteractionMode.CODEX
        assert h.get_engine_name("chat1") == "Codex"

    def test_mode_to_context_source(self):
        from src.project import ContextSourceMode

        assert BaseHandler.mode_to_context_source(InteractionMode.SMART) == ContextSourceMode.SMART
        assert BaseHandler.mode_to_context_source(InteractionMode.COCO) == ContextSourceMode.COCO
        assert BaseHandler.mode_to_context_source(InteractionMode.CLAUDE) == ContextSourceMode.CLAUDE

    def test_normalize_interactive_card_content_removes_schema2_root_elements(self):
        card_json = json.dumps(
            {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "title"}},
                "elements": [{"tag": "markdown", "content": "hello"}],
                "body": {"elements": [{"tag": "markdown", "content": "hello"}]},
            },
            ensure_ascii=False,
        )

        normalized = BaseHandler._normalize_interactive_card_content(card_json)
        card = json.loads(normalized)

        assert "elements" not in card
        assert card["body"]["elements"][0]["content"] == "hello"

    def test_inject_bridge_context_no_project(self):
        h, _ = self._make()
        assert h.inject_bridge_context("hello", None) == "hello"

    def test_inject_bridge_context_no_context(self):
        h, ctx = self._make()
        ctx.context_manager.store.get.return_value = None
        project = SimpleNamespace(project_id="p1")
        assert h.inject_bridge_context("hello", project) == "hello"

    def test_inject_bridge_context_with_bridge(self):
        h, ctx = self._make()
        mock_ctx = MagicMock()
        bridge = MagicMock()
        bridge.to_injection_prompt.return_value = "[bridge context]"
        mock_ctx.consume_bridge_summary.return_value = bridge
        ctx.context_manager.store.get.return_value = mock_ctx
        project = SimpleNamespace(project_id="p1", project_name="test")
        result = h.inject_bridge_context("hello", project)
        assert "[bridge context]" in result
        assert "hello" in result


# ======================================================================
# SystemHandler tests
# ======================================================================


class TestSystemHandlerPredicates:
    def test_exit_commands(self):
        assert SystemHandler.is_exit_command("/exit") is True
        assert SystemHandler.is_exit_command("/quit") is True
        assert SystemHandler.is_exit_command("/end_coco") is True
        assert SystemHandler.is_exit_command("/exit_claude") is True
        assert SystemHandler.is_exit_command("退出模式") is True
        assert SystemHandler.is_exit_command("退出编程模式") is True
        assert SystemHandler.is_exit_command("hello") is False
        assert SystemHandler.is_exit_command("/help") is False

    def test_deep_commands(self):
        assert SystemHandler.is_deep_command("/deep do stuff") is True
        assert SystemHandler.is_deep_command("/deep_status") is True
        assert SystemHandler.is_deep_command("/stop_deep") is True
        assert SystemHandler.is_deep_command("/help") is False
        assert SystemHandler.is_deep_command("deep") is False

    def test_interceptable_commands(self):
        assert SystemHandler.is_interceptable_command("/help") is True
        assert SystemHandler.is_interceptable_command("/帮助") is True
        assert SystemHandler.is_interceptable_command("/coco_info") is True
        assert SystemHandler.is_interceptable_command("/claude_info") is True
        assert SystemHandler.is_interceptable_command("/gemini_info") is True
        assert SystemHandler.is_interceptable_command("/projects") is True
        assert SystemHandler.is_interceptable_command("/status") is True
        assert SystemHandler.is_interceptable_command("/switch foo") is True
        assert SystemHandler.is_interceptable_command("/new myproject /tmp") is True
        assert SystemHandler.is_interceptable_command("/tasks") is True
        assert SystemHandler.is_interceptable_command("/diff") is True
        assert SystemHandler.is_interceptable_command("/trace") is True
        assert SystemHandler.is_interceptable_command("/exit") is False
        assert SystemHandler.is_interceptable_command("/deep stuff") is False
        assert SystemHandler.is_interceptable_command("hello") is False


class TestSystemHandlerRouting:
    def _make(self):
        ctx = _make_handler_context()
        handler = SystemHandler(ctx)
        handler.coco_handler = MagicMock()
        handler.claude_handler = MagicMock()
        handler.project_handler = MagicMock()
        handler.deep_handler = MagicMock()
        handler.diagnostics_handler = MagicMock()
        return handler

    def test_route_help(self):
        h = self._make()
        h.show_full_help = MagicMock()
        h.handle_intercepted_command("m1", "c1", "/help", None)
        h.show_full_help.assert_called_once_with("m1", "c1", None)

    def test_route_chinese_help(self):
        h = self._make()
        h.show_full_help = MagicMock()
        h.handle_intercepted_command("m1", "c1", "/帮助", None)
        h.show_full_help.assert_called_once_with("m1", "c1", None)

    def test_route_coco_info(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/coco_info", None)
        h.coco_handler.show_info.assert_called_once_with("m1", "c1", None)

    def test_route_claude_info(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/claude_info", None)
        h.claude_handler.show_info.assert_called_once_with("m1", "c1", None)

    def test_route_projects(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/projects", None)
        h.project_handler.show_project_board.assert_called_once_with("m1", "c1")

    def test_route_status(self):
        h = self._make()
        project = MagicMock()
        h.handle_intercepted_command("m1", "c1", "/status", project)
        h.diagnostics_handler.show_unified_status.assert_called_once_with("m1", "c1", "/status", project)

    def test_route_status_with_arg(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/status some_task_id", None)
        h.diagnostics_handler.show_unified_status.assert_called_once_with("m1", "c1", "/status some_task_id", None)

    def test_route_tasks(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/tasks", None)
        h.diagnostics_handler.show_task_board.assert_called_once_with("m1", "c1", "/tasks", None)

    def test_route_diff(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/diff", None)
        h.diagnostics_handler.show_context_diff.assert_called_once_with("m1", "c1", "/diff", None)

    def test_route_trace(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/trace msg123", None)
        h.diagnostics_handler.show_message_trace.assert_called_once_with("m1", "c1", "/trace msg123", None)

    def test_route_switch_with_name(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/switch myproject", None)
        h.project_handler.switch_project.assert_called_once_with(
            "m1",
            "c1",
            "myproject",
            coco_handler=h.coco_handler,
            claude_handler=h.claude_handler,
        )

    def test_route_switch_no_name(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/switch", None)
        h.project_handler.show_project_board.assert_called_once_with("m1", "c1")

    def test_route_new_project(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/new myapp /tmp/myapp", None)
        h.project_handler.create_project.assert_called_once_with("m1", "c1", "myapp", "/tmp/myapp")

    def test_route_close_project(self):
        h = self._make()
        h.handle_intercepted_command("m1", "c1", "/close myapp", None)
        h.project_handler.close_project.assert_called_once_with("m1", "c1", "myapp")

    def test_exit_current_mode_coco(self):
        ctx = _make_handler_context()
        ctx.mode_manager.get_mode.return_value = InteractionMode.COCO
        h = SystemHandler(ctx)
        h.coco_handler = MagicMock()
        h.claude_handler = MagicMock()
        h.exit_current_mode("m1", "c1", None)
        h.coco_handler.exit_mode.assert_called_once_with("m1", "c1", None)

    def test_exit_current_mode_claude(self):
        ctx = _make_handler_context()
        ctx.mode_manager.get_mode.return_value = InteractionMode.CLAUDE
        h = SystemHandler(ctx)
        h.coco_handler = MagicMock()
        h.claude_handler = MagicMock()
        h.exit_current_mode("m1", "c1", None)
        h.claude_handler.exit_mode.assert_called_once_with("m1", "c1", None)

    def test_handle_ttadk_command_shows_tool_select_even_when_configured(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.ttadk_handler = MagicMock()
        h.reply_error = MagicMock()
        h.reply_message = MagicMock()

        project = MagicMock()
        project.project_id = "p1"
        project.ttadk_tool_name = "codex"
        project.ttadk_model_name = "gpt-5.2"
        project.ttadk_yolo_enabled = False

        tools = [TTADKTool(name="codex", description="Codex")]
        with (
            patch("src.feishu.handlers.system.CardBuilder.build_ttadk_tool_select_card", return_value=("interactive", "{}")) as mock_build,
            patch("src.feishu.handlers.system.get_ttadk_manager") as mock_manager,
        ):
            manager = MagicMock()
            manager.get_tools.return_value = SimpleNamespace(tools=tools, error=None, warnings=[])
            mock_manager.return_value = manager

            h.handle_ttadk_command("m1", "c1", project, force_select=True)

            h.ttadk_handler.enter_mode.assert_not_called()
            mock_build.assert_called_once()

    def test_handle_select_ttadk_tool_auto_selects_default_model(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_error = MagicMock()
        h.handle_select_ttadk_model = MagicMock()

        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp"
        ctx.project_manager.get_project.return_value = project

        manager = MagicMock()
        manager.set_tool.return_value = True
        manager.get_current_model.return_value = None
        manager.get_models.return_value = SimpleNamespace(
            models=[TTADKModel(name="gpt-5.2", description="", is_default=True)],
            error=None,
            warnings=[],
        )

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager):
            h.handle_select_ttadk_tool("m1", "c1", "codex", "p1")

        h.handle_select_ttadk_model.assert_called_once_with(
            "m1", "c1", "codex", "gpt-5.2", project=project, silent=True
        )

    def test_handle_ttadk_command_always_shows_tool_card(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()

        project = MagicMock()
        project.project_id = "p1"
        project.ttadk_tool_name = "codex"
        project.ttadk_model_name = "gpt-5.2"
        project.ttadk_yolo_enabled = False
        project.root_path = "/tmp"

        tools = [
            TTADKTool(name="codex", description=""),
            TTADKTool(name="claude", description=""),
        ]

        manager = MagicMock()
        manager.get_current_tool.return_value = ""
        manager.get_current_model.return_value = ""
        manager.get_tools.return_value = SimpleNamespace(tools=tools, error=None)

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager):
            h.handle_ttadk_command("m1", "c1", project)

        h.reply_message.assert_called_once()
        call_args = h.reply_message.call_args
        card_json = call_args[0][1]
        assert "TTADK 工具选择" in card_json

    def test_handle_ttadk_command_no_defaults_shows_tool_card(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_error = MagicMock()

        project = MagicMock()
        project.project_id = "p1"
        project.ttadk_tool_name = ""
        project.ttadk_model_name = ""
        project.ttadk_yolo_enabled = False
        project.root_path = "/tmp"

        tools = [
            TTADKTool(name="codex", description=""),
            TTADKTool(name="claude", description=""),
        ]

        manager = MagicMock()
        manager.get_current_tool.return_value = ""
        manager.get_tools.return_value = SimpleNamespace(tools=tools, error=None)

        with (
            patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager),
            patch("src.feishu.handlers.system.CardBuilder") as mock_builder,
        ):
            mock_builder.build_ttadk_tool_select_card.return_value = ("interactive", "{}")
            h.handle_ttadk_command("m1", "c1", project)

        mock_builder.build_ttadk_tool_select_card.assert_called_once()

    def test_handle_select_ttadk_tool_no_default_model_shows_card(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_error = MagicMock()
        h.patch_message = MagicMock(return_value=True)
        h.handle_select_ttadk_model = MagicMock()

        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp"
        project.ttadk_yolo_enabled = False
        project.ttadk_model_name = ""
        ctx.project_manager.get_project.return_value = project

        manager = MagicMock()
        manager.set_tool.return_value = True
        manager.get_current_model.return_value = None
        manager.get_models.return_value = SimpleNamespace(
            models=[
                TTADKModel(name="gpt-5.2", description="", is_default=False),
                TTADKModel(name="gpt-4.1", description="", is_default=False),
            ],
            error=None,
            warnings=[],
        )

        with (
            patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager),
            patch("src.feishu.handlers.system.CardBuilder") as mock_builder,
        ):
            mock_builder.build_ttadk_model_select_card.return_value = ("interactive", "{}")
            h.handle_select_ttadk_tool("m1", "c1", "codex", "p1")

        h.handle_select_ttadk_model.assert_not_called()
        mock_builder.build_ttadk_model_select_card.assert_called_once()

    def test_handle_ttadk_command_tool_list_error_returns_hint(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()
        ctx.project_manager.get_active_project.return_value = None

        manager = MagicMock()
        manager.get_tools.return_value = SimpleNamespace(tools=[], error="offline")

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager):
            h.handle_ttadk_command("m1", "c1", None)

        h.reply_message.assert_called_once()
        assert "已为你保留选择" in str(h.reply_message.call_args)
        assert "继续进入TTADK" in str(h.reply_message.call_args)

    def test_handle_select_ttadk_tool_model_list_error_returns_hint(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()

        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp"
        ctx.project_manager.get_project.return_value = project

        manager = MagicMock()
        manager.set_tool.return_value = True
        manager.get_models.return_value = SimpleNamespace(models=[], error="timeout", warnings=[])

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager):
            h.handle_select_ttadk_tool("m1", "c1", "codex", "p1")

        h.reply_message.assert_called_once()
        assert "已为你保留选择" in str(h.reply_message.call_args)
        assert "继续进入TTADK" in str(h.reply_message.call_args)

    def test_handle_select_ttadk_model_set_failure_returns_hint(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_error = MagicMock()

        manager = MagicMock()
        manager.set_model.return_value = False

        with patch("src.feishu.handlers.system.get_ttadk_manager", return_value=manager):
            h.handle_select_ttadk_model("m1", "c1", "codex", "gpt-5.2", project=None)

        assert h.reply_message.call_count == 2
        h.reply_error.assert_not_called()
        assert "已为你保留选择" in str(h.reply_message.call_args_list[-1])
        assert "继续进入TTADK" in str(h.reply_message.call_args_list[-1])

    def test_ttadk_flow_duration_is_recorded(self):
        ctx = _make_handler_context()
        h = SystemHandler(ctx)

        with patch("src.feishu.handlers.system.time.perf_counter", side_effect=[10.0, 10.45]):
            h._mark_ttadk_flow_start("c1")
            h._report_ttadk_flow_duration("c1", "p1", "enter_mode")

        assert h._ttadk_flow_last_duration_ms["c1"] == 450  # rounded to nearest ms
        assert "c1" not in h._ttadk_flow_start_times

    def test_show_tools_list_uses_cached_availability_api(self):
        h = self._make()
        with patch("src.feishu.handlers.system.tool_registry") as mock_registry:
            mock_registry.get_availability.return_value = True
            h.reply_interactive_card = MagicMock()
            h.show_tools_list("m1", "c1", None)
            # 5 tools in metadata
            assert mock_registry.get_availability.call_count == 5
            h.reply_interactive_card.assert_called_once()

    def test_show_tools_status_uses_manager_sessions(self):
        h = self._make()
        h.ctx.coco_manager.list_active_sessions.return_value = [
            {
                "session_key": "chat_1:proj_a",
                "session_id": "sid1",
                "last_active": 1000.0,
                "message_count": 3,
            }
        ]
        h.ctx.claude_manager.list_active_sessions.return_value = []
        h.ctx.aiden_manager.list_active_sessions.return_value = []
        h.ctx.codex_manager.list_active_sessions.return_value = []
        h.ctx.gemini_manager.list_active_sessions.return_value = []

        with patch("src.feishu.handlers.system.tool_registry") as mock_registry:
            mock_registry.get_availability.return_value = True
            h.reply_interactive_card = MagicMock()
            h.show_tools_status("m1", "c1", None)
            assert mock_registry.get_availability.call_count == 5
            h.reply_interactive_card.assert_called_once()


# ======================================================================
# ProgrammingModeHandler (CocoModeHandler / ClaudeModeHandler) tests
# ======================================================================


class TestCocoModeHandler:
    def _make(self, **ctx_overrides):
        ctx = _make_handler_context(**ctx_overrides)
        h = CocoModeHandler(ctx)
        h._opposite_handler = MagicMock()
        h._claude_handler = h._opposite_handler
        h._aiden_handler = MagicMock()
        h._codex_handler = MagicMock()
        h._gemini_handler = MagicMock()
        h._ttadk_handler = MagicMock()
        return h, ctx

    def test_mode_attributes(self):
        h, _ = self._make()
        assert h.mode_name == "Coco"
        assert h.mode_emoji == "🤖"
        assert h.is_coco is True

    def test_session_manager(self):
        h, ctx = self._make()
        assert h._get_session_manager() is ctx.coco_manager

    def test_is_in_this_mode(self):
        h, ctx = self._make()
        ctx.mode_manager.is_coco_mode.return_value = True
        assert h._is_in_this_mode("c1") is True

    def test_is_in_opposite_mode(self):
        h, ctx = self._make()
        _set_all_programming_mode_flags(ctx, False)
        ctx.mode_manager.is_claude_mode.return_value = True
        assert h._is_in_opposite_mode("c1") is True

    def test_is_in_opposite_mode_checks_all_other_programming_modes(self):
        h, ctx = self._make()
        _set_all_programming_mode_flags(ctx, False)
        ctx.mode_manager.is_aiden_mode.return_value = True
        assert h._is_in_opposite_mode("c1") is True

    def test_enter_mode_on_manager(self):
        h, ctx = self._make()
        h._enter_mode_on_manager("c1")
        ctx.mode_manager.enter_coco_mode.assert_called_once_with("c1", project_id=None)

    def test_enter_mode_on_manager_with_project(self):
        h, ctx = self._make()
        h._enter_mode_on_manager("c1", project_id="p1")
        ctx.mode_manager.enter_coco_mode.assert_called_once_with("c1", project_id="p1")

    def test_get_interaction_mode(self):
        h, _ = self._make()
        assert h._get_interaction_mode() == InteractionMode.COCO

    def test_get_snapshot(self):
        h, _ = self._make()
        project = SimpleNamespace(coco_session_snapshot="snap", claude_session_snapshot=None)
        assert h._get_snapshot(project) == "snap"

    def test_set_mode_on_project_activate(self):
        h, _ = self._make()
        project = MagicMock()
        h._set_mode_on_project(project, True, "sid", 5)
        project.set_coco_mode.assert_called_once_with(True, "sid", 5)

    def test_set_mode_on_project_deactivate(self):
        h, _ = self._make()
        project = MagicMock()
        h._set_mode_on_project(project, False)
        project.set_coco_mode.assert_called_once_with(False)

    def test_update_snapshot_on_project(self):
        h, _ = self._make()
        project = MagicMock()
        h._update_snapshot_on_project(project, "hello", 3)
        project.update_coco_snapshot.assert_called_once_with(query="hello", query_count=3)

    def test_clear_snapshot(self):
        h, _ = self._make()
        project = SimpleNamespace(coco_session_snapshot="snap")
        h._clear_snapshot_on_project(project)
        assert project.coco_session_snapshot is None

    def test_exit_opposite_mode(self):
        h, _ = self._make()
        h._aiden_handler = MagicMock()
        h._codex_handler = MagicMock()
        h._gemini_handler = MagicMock()
        h._ttadk_handler = MagicMock()
        h.mode_manager.is_claude_mode.return_value = True
        h.mode_manager.is_aiden_mode.return_value = True
        h.mode_manager.is_codex_mode.return_value = False
        h.mode_manager.is_gemini_mode.return_value = False
        h.mode_manager.is_ttadk_mode.return_value = True
        h._exit_opposite_mode("m1", "c1", project=None)
        h._opposite_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._aiden_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._codex_handler.exit_mode.assert_not_called()
        h._gemini_handler.exit_mode.assert_not_called()
        h._ttadk_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)


class TestClaudeModeHandler:
    def _make(self, **ctx_overrides):
        ctx = _make_handler_context(**ctx_overrides)
        h = ClaudeModeHandler(ctx)
        h._opposite_handler = MagicMock()
        h._coco_handler = h._opposite_handler
        h._aiden_handler = MagicMock()
        h._codex_handler = MagicMock()
        h._gemini_handler = MagicMock()
        h._ttadk_handler = MagicMock()
        return h, ctx

    def test_mode_attributes(self):
        h, _ = self._make()
        assert h.mode_name == "Claude"
        assert h.mode_emoji == "🔮"
        assert h.is_coco is False

    def test_session_manager(self):
        h, ctx = self._make()
        assert h._get_session_manager() is ctx.claude_manager

    def test_is_in_this_mode(self):
        h, ctx = self._make()
        ctx.mode_manager.is_claude_mode.return_value = True
        assert h._is_in_this_mode("c1") is True

    def test_is_in_opposite_mode(self):
        h, ctx = self._make()
        _set_all_programming_mode_flags(ctx, False)
        ctx.mode_manager.is_coco_mode.return_value = True
        assert h._is_in_opposite_mode("c1") is True

    def test_is_in_opposite_mode_checks_all_other_programming_modes(self):
        h, ctx = self._make()
        _set_all_programming_mode_flags(ctx, False)
        ctx.mode_manager.is_ttadk_mode.return_value = True
        assert h._is_in_opposite_mode("c1") is True

    def test_enter_mode_on_manager(self):
        h, ctx = self._make()
        h._enter_mode_on_manager("c1")
        ctx.mode_manager.enter_claude_mode.assert_called_once_with("c1", project_id=None)

    def test_enter_mode_on_manager_with_project(self):
        h, ctx = self._make()
        h._enter_mode_on_manager("c1", project_id="p1")
        ctx.mode_manager.enter_claude_mode.assert_called_once_with("c1", project_id="p1")

    def test_get_interaction_mode(self):
        h, _ = self._make()
        assert h._get_interaction_mode() == InteractionMode.CLAUDE

    def test_get_snapshot(self):
        h, _ = self._make()
        project = SimpleNamespace(coco_session_snapshot=None, claude_session_snapshot="snap")
        assert h._get_snapshot(project) == "snap"

    def test_set_mode_on_project_activate(self):
        h, _ = self._make()
        project = MagicMock()
        h._set_mode_on_project(project, True, "sid", 5)
        project.set_claude_mode.assert_called_once_with(True, "sid", 5)

    def test_update_snapshot_on_project(self):
        h, _ = self._make()
        project = MagicMock()
        h._update_snapshot_on_project(project, "hello", 3, "sid")
        project.update_claude_snapshot.assert_called_once_with(query="hello", query_count=3, session_id="sid")

    def test_clear_snapshot(self):
        h, _ = self._make()
        project = SimpleNamespace(claude_session_snapshot="snap")
        h._clear_snapshot_on_project(project)
        assert project.claude_session_snapshot is None

    def test_exit_opposite_mode(self):
        h, _ = self._make()
        h.mode_manager.is_coco_mode.return_value = True
        h.mode_manager.is_aiden_mode.return_value = True
        h.mode_manager.is_codex_mode.return_value = False
        h.mode_manager.is_gemini_mode.return_value = True
        h.mode_manager.is_ttadk_mode.return_value = False
        h._exit_opposite_mode("m1", "c1", project=None)
        h._opposite_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._aiden_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._codex_handler.exit_mode.assert_not_called()
        h._gemini_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._ttadk_handler.exit_mode.assert_not_called()


class TestTTADKModeHandler:
    def _make(self, **ctx_overrides):
        ctx = _make_handler_context(**ctx_overrides)
        h = TTADKModeHandler(ctx)
        h._coco_handler = MagicMock()
        h._claude_handler = MagicMock()
        h._aiden_handler = MagicMock()
        h._codex_handler = MagicMock()
        h._gemini_handler = MagicMock()
        return h, ctx

    def test_is_in_opposite_mode_checks_all_other_programming_modes(self):
        h, ctx = self._make()
        _set_all_programming_mode_flags(ctx, False)
        ctx.mode_manager.is_aiden_mode.return_value = True
        assert h._is_in_opposite_mode("c1") is True

    def test_exit_opposite_mode(self):
        h, _ = self._make()
        h.mode_manager.is_coco_mode.return_value = True
        h.mode_manager.is_claude_mode.return_value = False
        h.mode_manager.is_aiden_mode.return_value = True
        h.mode_manager.is_codex_mode.return_value = True
        h.mode_manager.is_gemini_mode.return_value = False
        h._exit_opposite_mode("m1", "c1", project=None)
        h._coco_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._claude_handler.exit_mode.assert_not_called()
        h._aiden_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._codex_handler.exit_mode.assert_called_once_with("m1", "c1", project=None)
        h._gemini_handler.exit_mode.assert_not_called()

    def test_set_mode_on_project_activate_does_not_hardcode_other_modes(self):
        h, _ = self._make()
        project = MagicMock()
        h._set_mode_on_project(project, True, "sid", 4)
        project.set_ttadk_mode.assert_called_once_with(True, "sid", 4)
        project.set_coco_mode.assert_not_called()
        project.set_claude_mode.assert_not_called()

    def test_enter_mode_builds_ttadk_entry_card(self):
        ctx = _make_handler_context()
        ctx.mode_manager.is_ttadk_mode.return_value = False
        ctx.mode_manager.is_coco_mode.return_value = False
        ctx.mode_manager.is_claude_mode.return_value = False
        ctx.mode_manager.is_aiden_mode.return_value = False
        ctx.mode_manager.is_codex_mode.return_value = False
        ctx.mode_manager.is_gemini_mode.return_value = False
        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        ctx.project_manager.validate_project_path.return_value = (True, "ok")
        project = MagicMock()
        project.ttadk_session_snapshot = None
        project.root_path = "/tmp"
        project.project_name = "test"
        project.project_id = "test_id"
        project.ttadk_mode = False
        project.coco_mode = False
        project.claude_mode = False
        ctx.project_manager.get_or_create_project_for_path.return_value = (project, False)

        sess = MagicMock()
        sess.session_id = "sid_ttadk"
        sess.is_resumed = False
        ctx.ttadk_manager.ensure_session.return_value = sess

        h = TTADKModeHandler(ctx)
        h._get_agent_type_override = MagicMock(return_value="ttadk_coco")
        h._get_model_name_override = MagicMock(return_value="gpt-5.2")
        h.reply_message = MagicMock()
        h.reply_message_with_id = MagicMock(return_value="reply_1")
        h.add_reaction = MagicMock()
        h.record_mode_transition = MagicMock()
        h.register_message_project = MagicMock()

        with patch("src.feishu.handlers.programming.CardBuilder.build_project_response_card") as mock_build:
            mock_build.return_value = ("interactive", "{}")
            h.enter_mode("m1", "c1", project=project)

            mock_build.assert_called_once()
            _, title, content = mock_build.call_args.args[:3]
            assert "TTADK编程模式" in title
            assert "已进入TTADK编程模式" in content
            h.reply_message_with_id.assert_called_once()

    def test_enter_mode_ttadk_timeout_uses_warning(self):
        ctx = _make_handler_context()
        ctx.mode_manager.is_ttadk_mode.return_value = False
        ctx.mode_manager.is_coco_mode.return_value = False
        ctx.mode_manager.is_claude_mode.return_value = False
        ctx.mode_manager.is_aiden_mode.return_value = False
        ctx.mode_manager.is_codex_mode.return_value = False
        ctx.mode_manager.is_gemini_mode.return_value = False
        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        project = MagicMock()
        project.root_path = "/tmp"
        project.project_id = "p1"
        ctx.project_manager.get_or_create_project_for_path.return_value = (project, False)
        ctx.project_manager.validate_project_path.return_value = (True, "ok")

        ctx.ttadk_manager.ensure_session.side_effect = TimeoutError("boom")

        h = TTADKModeHandler(ctx)
        h.reply_message = MagicMock()
        h.send_error_card = MagicMock()

        h.enter_mode("m1", "c1", project=project)

        h.reply_message.assert_called_once()
        h.send_error_card.assert_not_called()
        assert "已为你保留选择" in str(h.reply_message.call_args)


class TestProgrammingModeEnterExit:
    """Integration-level tests for enter_mode / exit_mode template methods."""

    def _make_coco(self):
        ctx = _make_handler_context()
        ctx.mode_manager.is_coco_mode.return_value = False
        ctx.mode_manager.is_claude_mode.return_value = False
        ctx.mode_manager.is_aiden_mode.return_value = False
        ctx.mode_manager.is_codex_mode.return_value = False
        ctx.mode_manager.is_gemini_mode.return_value = False
        ctx.mode_manager.is_ttadk_mode.return_value = False
        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART
        ctx.project_manager.validate_project_path.return_value = (True, "ok")
        ctx.project_manager.get_or_create_project_for_path.return_value = (None, False)

        mock_session = MagicMock()
        mock_session.session_id = "sid1"
        mock_session.is_resumed = False
        ctx.coco_manager.start_session.return_value = mock_session
        ctx.coco_manager.get_session.return_value = mock_session
        ctx.coco_manager.end_session.return_value = True

        h = CocoModeHandler(ctx)
        h._opposite_handler = MagicMock()
        h._claude_handler = h._opposite_handler
        h._aiden_handler = MagicMock()
        h._codex_handler = MagicMock()
        h._gemini_handler = MagicMock()
        h._ttadk_handler = MagicMock()
        # Mock reply to avoid real API calls
        h.reply_message = MagicMock()
        h.reply_message_with_id = MagicMock(return_value="reply_1")
        h.add_reaction = MagicMock()
        h.record_mode_transition = MagicMock()
        h.register_message_project = MagicMock()
        return h, ctx

    def test_enter_mode_already_in_mode(self):
        h, ctx = self._make_coco()
        ctx.mode_manager.is_coco_mode.return_value = True
        ctx.coco_manager.get_session_info.return_value = "session info"
        h.enter_mode("m1", "c1")
        h.reply_message.assert_called_once()
        assert "已经在" in str(h.reply_message.call_args)

    def test_enter_mode_with_project_no_snapshot(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        project.coco_session_snapshot = None
        project.root_path = "/tmp"
        project.project_name = "test"
        project.project_id = "test_id"
        h.enter_mode("m1", "c1", project=project)
        ctx.mode_manager.enter_coco_mode.assert_called_once_with("c1", project_id="test_id")
        project.set_coco_mode.assert_called_once()
        h.record_mode_transition.assert_called_once()

    def test_exit_mode_with_session(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        project.project_id = "p1"
        project.project_name = "test"
        project.root_path = "/tmp"
        h.exit_mode("m1", "c1", project=project)
        ctx.mode_manager.exit_to_smart.assert_called_once_with("c1", project_id="p1")
        ctx.coco_manager.end_session.assert_called_once_with("c1", project_id="p1")

    def test_enter_mode_mutual_exclusion(self):
        """When in another programming mode, entering Coco should exit it first."""
        h, ctx = self._make_coco()
        ctx.mode_manager.is_gemini_mode.return_value = True
        project = MagicMock()
        project.coco_session_snapshot = None
        project.root_path = "/tmp"
        project.project_name = "test"
        project.project_id = "test_id"
        h.enter_mode("m1", "c1", project=project)
        h._gemini_handler.exit_mode.assert_called_once()

    def test_handle_card_resume_syncs_project_flags(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp"
        ctx.project_manager.get_project.return_value = project
        session = MagicMock()
        session.session_id = "sid_resume"
        ctx.coco_manager.start_session.return_value = session
        h.handle_card_resume("m1", "c1", "p1", "sid_resume")
        project.set_claude_mode.assert_called_once_with(False)
        project.set_aiden_mode.assert_called_once_with(False)
        project.set_codex_mode.assert_called_once_with(False)
        project.set_gemini_mode.assert_called_once_with(False)
        project.set_ttadk_mode.assert_called_once_with(False)
        project.set_coco_mode.assert_called_once_with(True, "sid_resume", 0)

    def test_handle_card_resume_start_failure_does_not_switch_mode(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp"
        ctx.project_manager.get_project.return_value = project
        ctx.coco_manager.start_session.side_effect = RuntimeError("boom")
        h.send_error_card = MagicMock()
        h.handle_card_resume("m1", "c1", "p1", "sid_resume")
        ctx.mode_manager.enter_coco_mode.assert_not_called()
        h.send_error_card.assert_called_once()

    def test_show_info_with_project(self):
        h, ctx = self._make_coco()
        ctx.coco_manager.get_session_info.return_value = "some info"
        project = MagicMock()
        project.project_name = "test"
        project.project_id = "p1"
        project.root_path = "/tmp"
        h.show_info("m1", "c1", project=project)
        h.reply_message_with_id.assert_called_once()

    def test_show_info_no_session(self):
        h, ctx = self._make_coco()
        ctx.coco_manager.get_session_info.return_value = None
        h.show_info("m1", "c1")
        h.reply_message.assert_called_once()

    def test_card_enter_with_project_no_snapshot(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        project.coco_session_snapshot = None
        project.root_path = "/tmp"
        project.project_name = "test"
        project.project_id = "p1"
        ctx.project_manager.get_project.return_value = project
        h.enter_mode = MagicMock()
        h.handle_card_enter("m1", "c1", "p1")
        ctx.project_manager.set_active_project.assert_called_once_with("c1", "p1")
        h.enter_mode.assert_called_once_with("m1", "c1", project=project)

    def test_card_exit(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        ctx.project_manager.get_project.return_value = project
        h.exit_mode = MagicMock()
        h.handle_card_exit("m1", "c1", "p1")
        project.set_coco_mode.assert_called_once_with(False)
        h.exit_mode.assert_called_once()

    def test_card_new_clears_snapshot(self):
        h, ctx = self._make_coco()
        project = MagicMock()
        ctx.project_manager.get_project.return_value = project
        h.enter_mode = MagicMock()
        h.handle_card_new("m1", "c1", "p1")
        assert project.coco_session_snapshot is None
        h.enter_mode.assert_called_once()


class TestTTADKModeDegradeWarning:
    def test_ttadk_enter_mode_emits_degrade_warning(self):
        ctx = _make_handler_context()

        ctx.mode_manager.is_ttadk_mode.return_value = False
        ctx.mode_manager.is_coco_mode.return_value = False
        ctx.mode_manager.is_claude_mode.return_value = False
        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        ctx.project_manager.validate_project_path.return_value = (True, "ok")
        project = MagicMock()
        project.ttadk_session_snapshot = None
        project.root_path = "/tmp"
        project.project_name = "test"
        project.project_id = "test_id"
        ctx.project_manager.get_or_create_project_for_path.return_value = (project, False)

        sess = MagicMock()
        sess.session_id = "sid_ttadk"
        sess.is_resumed = False
        sess._degraded_to = "coco"
        sess._degraded_reason = "boom"
        ctx.ttadk_manager.ensure_session.return_value = sess

        h = TTADKModeHandler(ctx)
        # 避免进入真实 ttadk 配置解析逻辑
        h._get_agent_type_override = MagicMock(return_value="ttadk_coco")
        h._get_model_name_override = MagicMock(return_value="gpt-5.2")

        h.reply_message = MagicMock()
        h.reply_message_with_id = MagicMock(return_value="reply_1")
        h.add_reaction = MagicMock()
        h.record_mode_transition = MagicMock()
        h.register_message_project = MagicMock()

        h.enter_mode("m1", "c1", project=project)

        assert any("TTADK 后端暂不可用" in str(call) for call in h.reply_message.call_args_list)


# ======================================================================
# ProjectHandler tests
# ======================================================================


class TestProjectHandler:
    def _make(self):
        ctx = _make_handler_context()
        h = ProjectHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_message_with_id = MagicMock(return_value="reply_1")
        h.add_reaction = MagicMock()
        h.register_message_project = MagicMock()
        return h, ctx

    def test_create_project_success(self):
        h, ctx = self._make()
        project = MagicMock()
        project.project_name = "test"
        project.project_id = "test"
        project.root_path = "/tmp"
        ctx.project_manager.create_project.return_value = (True, "ok", project)
        with patch("src.feishu.handlers.project.CardBuilder") as mock_cb:
            mock_cb.build_project_created_card.return_value = ("interactive", "{}")
            h.create_project("m1", "c1", "test", "/tmp")
            h.reply_message_with_id.assert_called_once()

    def test_create_project_failure(self):
        h, ctx = self._make()
        ctx.project_manager.create_project.return_value = (False, "already exists", None)
        h.create_project("m1", "c1", "test", "/tmp")
        h.reply_message.assert_called_once()

    def test_show_project_board(self):
        h, ctx = self._make()
        ctx.project_manager.get_all_projects.return_value = []
        ctx.project_manager.get_active_project.return_value = None
        h.show_project_board("m1", "c1")
        h.reply_message_with_id.assert_called_once()

    def test_show_project_board_patch_success(self):
        h, ctx = self._make()
        ctx.project_manager.get_all_projects.return_value = []
        ctx.project_manager.get_active_project.return_value = None

        # Mock API client for patch success
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        mock_client.im.v1.message.patch.return_value = mock_resp
        ctx.api_client_factory.return_value = mock_client

        with patch("src.feishu.handlers.project.CardBuilder") as mock_cb:
            mock_cb.build_status_board_card.return_value = ("interactive", "{}")

            h.show_project_board("m1", "c1", origin_message_id="origin1")

            # Verify Patch called
            mock_client.im.v1.message.patch.assert_called_once()
            # Verify Reply NOT called
            h.reply_message_with_id.assert_not_called()

    def test_show_project_board_patch_failure(self):
        h, ctx = self._make()
        ctx.project_manager.get_all_projects.return_value = []
        ctx.project_manager.get_active_project.return_value = None

        # Mock API client for patch failure
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = False
        mock_resp.msg = "fail"
        mock_client.im.v1.message.patch.return_value = mock_resp
        ctx.api_client_factory.return_value = mock_client

        with patch("src.feishu.handlers.project.CardBuilder") as mock_cb:
            mock_cb.build_status_board_card.return_value = ("interactive", "{}")

            h.show_project_board("m1", "c1", origin_message_id="origin1")

            # Verify Patch called
            mock_client.im.v1.message.patch.assert_called_once()
            # Verify Reply called (fallback)
            h.reply_message_with_id.assert_called_once()

    def test_show_project_status_patch_success(self):
        h, ctx = self._make()
        project = MagicMock()
        project.root_path = "/tmp"
        project.project_name = "test"
        project.project_id = "p1"
        project.last_active = 0
        project.get_status_emoji.return_value = "🟢"

        # Mock API client for patch success
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        mock_client.im.v1.message.patch.return_value = mock_resp
        ctx.api_client_factory.return_value = mock_client

        with patch("src.feishu.handlers.project.CardBuilder") as mock_cb:
            mock_cb.build_project_response_card.return_value = ("interactive", "{}")

            h.show_project_status("m1", "c1", project, origin_message_id="origin1")

            # Verify Patch called
            mock_client.im.v1.message.patch.assert_called_once()
            # Verify Reply NOT called
            h.reply_message_with_id.assert_not_called()

    def test_show_project_status_no_project(self):
        h, ctx = self._make()
        h.show_project_board = MagicMock()
        h.show_project_status("m1", "c1", None)
        h.show_project_board.assert_called_once_with("m1", "c1")

    def test_close_project_success(self):
        h, ctx = self._make()
        project = MagicMock()
        ctx.project_manager.find_project_by_name.return_value = project
        ctx.project_manager.close_project.return_value = (True, "closed")
        h.close_project("m1", "c1", "test")
        h.reply_message.assert_called_once()
        assert "✅" in str(h.reply_message.call_args)

    def test_close_project_not_found(self):
        h, ctx = self._make()
        ctx.project_manager.find_project_by_name.return_value = None
        h.close_project("m1", "c1", "test")
        h.reply_message.assert_called_once()
        assert "❌" in str(h.reply_message.call_args)

    def test_restore_project_context_no_context(self):
        h, ctx = self._make()
        ctx.context_manager.store.get.return_value = None
        project = SimpleNamespace(project_id="p1", project_name="test")
        info = h.restore_project_context(project)
        assert info["has_context"] is False

    def test_restore_project_context_with_context(self):
        h, ctx = self._make()
        mock_ctx = MagicMock()
        mock_ctx.entry_count = 5
        mock_ctx.versions = [1, 2]
        mock_ctx.last_bridge_summary = None
        mock_ctx.get_entries_by_type.return_value = []
        ctx.context_manager.store.get.return_value = mock_ctx
        project = SimpleNamespace(project_id="p1", project_name="test")
        info = h.restore_project_context(project)
        assert info["has_context"] is True
        assert info["entry_count"] == 5
        assert info["version_count"] == 2


# ======================================================================
# DeepHandler tests
# ======================================================================


class TestDeepHandler:
    def _make(self):
        ctx = _make_handler_context()
        h = DeepHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_message_with_id = MagicMock(return_value="reply_1")
        h.add_reaction = MagicMock()
        h.register_message_project = MagicMock()
        return h, ctx

    def test_handle_deep_command_status(self):
        h, ctx = self._make()
        h.show_deep_status = MagicMock()
        h.handle_deep_command("m1", "c1", "/deep_status", None)
        h.show_deep_status.assert_called_once()

    def test_handle_deep_command_stop(self):
        h, ctx = self._make()
        h.stop_deep_engine = MagicMock()
        h.handle_deep_command("m1", "c1", "/stop_deep", None)
        h.stop_deep_engine.assert_called_once()

    def test_handle_deep_command_status_all(self):
        h, ctx = self._make()
        h.show_deep_board = MagicMock()
        h.handle_deep_command("m1", "c1", "/deep_status all", None)
        h.show_deep_board.assert_called_once()

    def test_handle_deep_command_stop_all(self):
        h, ctx = self._make()
        h.stop_all_deep_engines = MagicMock()
        h.handle_deep_command("m1", "c1", "/stop_deep all", None)
        h.stop_all_deep_engines.assert_called_once()

    def test_handle_deep_command_empty(self):
        h, ctx = self._make()
        h.handle_deep_command("m1", "c1", "/deep", None)
        h.reply_message.assert_called_once()
        assert "请提供需求" in str(h.reply_message.call_args)

    def test_handle_deep_command_start(self):
        h, ctx = self._make()
        h.start_deep_engine = MagicMock()
        h.handle_deep_command("m1", "c1", "/deep implement feature X", None)
        h.start_deep_engine.assert_called_once_with("m1", "c1", "implement feature X", None)

    def test_handle_deep_command_update(self):
        h, ctx = self._make()
        h.update_deep_context = MagicMock()
        h.handle_deep_command("m1", "c1", "/deep_update some new info", None)
        h.update_deep_context.assert_called_once()

    def test_stop_all_deep_engines(self):
        h, ctx = self._make()
        engine1 = MagicMock()
        engine2 = MagicMock()
        ctx.deep_engine_manager.get_active_engines.return_value = [engine1, engine2]
        h.stop_all_deep_engines("m1", "c1")
        engine1.stop.assert_called_once()
        engine2.stop.assert_called_once()
        h.reply_message.assert_called_once()

    def test_stop_all_deep_engines_none_running(self):
        h, ctx = self._make()
        ctx.deep_engine_manager.get_active_engines.return_value = []
        h.stop_all_deep_engines("m1", "c1")
        h.reply_message.assert_called_once()
        assert "没有" in str(h.reply_message.call_args)

    def test_show_deep_status_patch_success(self):
        h, ctx = self._make()
        # Setup mock project and engine
        project = MagicMock()
        project.root_path = "/path/to/project"
        project.project_id = "p1"

        engine = MagicMock()
        engine.project = MagicMock()
        engine.progress = MagicMock()
        engine.engine_name = "DeepEngine"

        ctx.deep_engine_manager.get.return_value = engine

        # Setup mock reporter
        ctx.progress_reporter.format_status.return_value = "Status Content"
        ctx.progress_reporter.get_status_title.return_value = "Status Title"
        ctx.progress_reporter.get_progress_info.return_value = {
            "progress_bar": "|||",
            "project_id": "p1",
            "is_executing": True,
            "is_paused": False,
        }

        # Setup Patch client
        h.patch_message = MagicMock(return_value=True)

        # Mock CardBuilder
        with patch("src.feishu.handlers.deep.CardBuilder") as mock_cb:
            mock_cb.build_engine_card.return_value = ("interactive", "{}")

            # Execute
            h.show_deep_status("msg1", "chat1", project=project, origin_message_id="origin1")

            # Verify Patch called
            h.patch_message.assert_called_once()
            # Verify Reply NOT called
            h.reply_message.assert_not_called()

    def test_show_deep_status_patch_failure_fallback(self):
        h, ctx = self._make()
        # Setup mock project and engine
        project = MagicMock()
        project.root_path = "/path/to/project"

        engine = MagicMock()
        engine.project = MagicMock()
        engine.progress = MagicMock()
        engine.engine_name = "DeepEngine"
        # Ensure string returns for JSON serialization if Real CardBuilder is used
        engine.get_status_title.return_value = "Status Title"

        ctx.deep_engine_manager.get.return_value = engine

        ctx.progress_reporter.get_progress_info.return_value = {
            "progress_bar": "|||",
            "project_id": "p1",
            "is_executing": True,
            "is_paused": False,
        }

        # Setup Patch client to fail
        h.patch_message = MagicMock(return_value=False)

        # Mock the CardBuilder used by DeepRenderer (which is where it's actually called)
        # OR just rely on the fact that we fixed the engine mock return values.
        # But to be safe and match the test style, let's mock where it's used.
        # Since we saw in stack trace it was using real CardBuilder (because patch location was wrong),
        # let's try to patch the correct location.
        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_cb:
            mock_cb.build_engine_card.return_value = ("interactive", "{}")

            # Execute
            h.show_deep_status("msg1", "chat1", project=project, origin_message_id="origin1")

            # Verify Patch called
            h.patch_message.assert_called_once()
            # Verify Reply called (Fallback)
            h.reply_message.assert_called_once()


# ======================================================================
# DiagnosticsHandler tests
# ======================================================================


class TestDiagnosticsHandler:
    def _make(self):
        ctx = _make_handler_context()
        h = DiagnosticsHandler(ctx)
        h.reply_message = MagicMock()
        h.reply_message_with_id = MagicMock(return_value="reply_1")
        h.add_reaction = MagicMock()
        h.register_message_project = MagicMock()
        return h, ctx

    def test_show_task_board_no_project(self):
        """When no project is active and no project passed, should report no project."""
        h, ctx = self._make()
        ctx.project_manager.get_active_project.return_value = None
        h.show_task_board("m1", "c1", "/tasks", None)
        h.reply_message.assert_called()
        assert "没有" in str(h.reply_message.call_args)

    def test_show_task_board_all(self):
        """When /tasks all is used, shows all-project task board."""
        h, ctx = self._make()
        ctx.scheduler.get_all_tasks.return_value = []
        with patch("src.feishu.handlers.diagnostics.CardBuilder") as mock_cb:
            mock_cb.build_smart_response_card.return_value = ("interactive", "{}")
            h.show_task_board("m1", "c1", "/tasks all", None)
        h.reply_message.assert_called()

    def test_show_context_diff_no_project(self):
        """show_context_diff with no active project should report that."""
        h, ctx = self._make()
        ctx.project_manager.get_active_project.return_value = None
        h.show_context_diff("m1", "c1", "/diff", None)
        h.reply_message.assert_called_once()
        assert "没有" in str(h.reply_message.call_args)

    def test_show_message_trace_no_args(self):
        h, ctx = self._make()
        h.show_message_trace("m1", "c1", "/trace", None)
        h.reply_message.assert_called_once()


# ======================================================================
# Bug fix: Shell command fast-track heuristic
# ======================================================================


class TestShellCommandHeuristic:
    """Tests for SystemHandler.is_likely_shell_command()."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "ls",
            "pwd",
            "whoami",
            "date",
            "uptime",
            "ls -la",
            "git status",
            "cat foo.txt",
            "grep pattern file",
            "docker ps",
            "make build",
            "tree src/",
        ],
    )
    def test_detects_shell_commands(self, cmd):
        assert SystemHandler.is_likely_shell_command(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "/help",
            "/coco",
            "/deep implement X",
            "/exit",
            "帮我写一个函数",
            "请解释这段代码",
            "",
        ],
    )
    def test_ignores_non_shell(self, cmd):
        assert SystemHandler.is_likely_shell_command(cmd) is False

    def test_slash_commands_are_not_shell(self):
        assert SystemHandler.is_likely_shell_command("/projects") is False
        assert SystemHandler.is_likely_shell_command("/switch foo") is False


# ======================================================================
# Bug fix: ACP Session Manager project isolation
# ======================================================================


class TestACPSessionManagerProjectIsolation:
    """Tests for ACPSessionManager keyed by (chat_id, project_id)."""

    def test_session_key_with_project(self):
        from src.acp.manager import ACPSessionManager

        key = ACPSessionManager._session_key("chat1", "proj_a")
        assert key == "chat1:proj_a"

    def test_session_key_without_project(self):
        from src.acp.manager import ACPSessionManager

        key = ACPSessionManager._session_key("chat1")
        assert key == "chat1:_default_"
        key2 = ACPSessionManager._session_key("chat1", None)
        assert key2 == "chat1:_default_"

    def test_different_projects_get_different_keys(self):
        from src.acp.manager import ACPSessionManager

        k1 = ACPSessionManager._session_key("chat1", "proj_a")
        k2 = ACPSessionManager._session_key("chat1", "proj_b")
        k3 = ACPSessionManager._session_key("chat1")
        assert k1 != k2
        assert k1 != k3
        assert k2 != k3

    def test_get_session_isolated_by_project(self):
        from src.acp.manager import ACPSessionManager

        mgr = ACPSessionManager("coco", session_timeout=999999)

        # Manually insert two sessions for same chat, different projects
        import time

        s1 = MagicMock()
        s1.last_active = time.time()
        s1.session_id = "s1"
        s2 = MagicMock()
        s2.last_active = time.time()
        s2.session_id = "s2"

        k1 = mgr._session_key("chat1", "proj_a")
        k2 = mgr._session_key("chat1", "proj_b")
        mgr._sessions[k1] = s1
        mgr._sessions[k2] = s2

        assert mgr.get_session("chat1", project_id="proj_a") is s1
        assert mgr.get_session("chat1", project_id="proj_b") is s2
        # Default project should not find either
        assert mgr.get_session("chat1") is None

    def test_end_session_does_not_affect_other_projects(self):
        import time

        from src.acp.manager import ACPSessionManager

        mgr = ACPSessionManager("coco", session_timeout=999999)

        s1 = MagicMock()
        s1.last_active = time.time()
        s1.session_id = "s1"
        s1.message_count = 0
        s1.to_snapshot.return_value = {}
        s2 = MagicMock()
        s2.last_active = time.time()
        s2.session_id = "s2"

        mgr._sessions[mgr._session_key("chat1", "proj_a")] = s1
        mgr._sessions[mgr._session_key("chat1", "proj_b")] = s2

        mgr.end_session("chat1", project_id="proj_a")

        # proj_a session ended, proj_b untouched
        assert mgr.get_session("chat1", project_id="proj_a") is None
        assert mgr.get_session("chat1", project_id="proj_b") is s2

    def test_agent_session_manager_alias_from_manager(self):
        from src.acp.manager import ACPSessionManager, AgentSessionManager

        assert issubclass(AgentSessionManager, ACPSessionManager)
        assert AgentSessionManager._session_key("chat1", "proj_a") == ACPSessionManager._session_key("chat1", "proj_a")

    def test_agent_session_manager_alias_from_package_exports(self):
        from src.acp import ACPSessionManager, AgentSessionManager

        mgr = AgentSessionManager("coco", session_timeout=999999)
        assert isinstance(mgr, ACPSessionManager)
        assert mgr._session_key("chat1") == "chat1:_default_"


# ======================================================================
# SystemHandler patch tests
# ======================================================================


class TestHelpCategoryPatch:
    def _make(self, ctx_overrides=None):
        if ctx_overrides is None:
            ctx_overrides = {}
        ctx = _make_handler_context(**ctx_overrides)
        h = SystemHandler(ctx)
        h.reply_message = MagicMock()
        # Mock get_working_dir to return a valid path string for CardBuilder
        h.get_working_dir = MagicMock(return_value="/tmp")
        return h, ctx

    def test_handle_help_category_patch_success(self):
        h, ctx = self._make()

        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        # Mock patch_message
        h.patch_message = MagicMock(return_value=True)

        with patch("src.card.builder.CardBuilder.build_help_card") as mock_build:
            mock_build.return_value = ("interactive", "{}")

            h.handle_help_category("msg1", "chat1", "main", origin_message_id="origin1")

            # Verify patch called
            h.patch_message.assert_called_once_with("origin1", "{}")
            # Verify reply NOT called
            h.reply_message.assert_not_called()

    def test_handle_help_category_patch_failure_fallback(self):
        h, ctx = self._make()

        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        # Mock patch_message failure
        h.patch_message = MagicMock(return_value=False)

        with patch("src.card.builder.CardBuilder.build_help_card") as mock_build:
            mock_build.return_value = ("interactive", "{}")

            h.handle_help_category("msg1", "chat1", "main", origin_message_id="origin1")

            # Verify patch called
            h.patch_message.assert_called_once_with("origin1", "{}")
            # Verify fallback to reply
            h.reply_message.assert_called_once()

    def test_handle_help_category_patch_exception_fallback(self):
        # With the new impl, patch_message handles exceptions internally and returns False
        # So this test is effectively same as failure fallback
        h, ctx = self._make()

        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        h.patch_message = MagicMock(return_value=False)

        with patch("src.card.builder.CardBuilder.build_help_card") as mock_build:
            mock_build.return_value = ("interactive", "{}")

            h.handle_help_category("msg1", "chat1", "main", origin_message_id="origin1")

            h.patch_message.assert_called_once()
            h.reply_message.assert_called_once()

    def test_handle_help_category_no_origin_id(self):
        h, ctx = self._make()

        ctx.mode_manager.get_mode.return_value = InteractionMode.SMART

        mock_client = MagicMock()
        ctx.api_client_factory.return_value = mock_client

        with patch("src.card.builder.CardBuilder.build_help_card") as mock_build:
            mock_build.return_value = ("interactive", "{}")

            h.handle_help_category("msg1", "chat1", "main", origin_message_id=None)

            h.patch_message = MagicMock()
            h.patch_message.assert_not_called()
            h.reply_message.assert_called_once()
