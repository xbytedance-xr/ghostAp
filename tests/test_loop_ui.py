from unittest.mock import MagicMock

from src.feishu.handler_context import HandlerContext
from src.feishu.handlers.loop import LoopHandler
from src.loop_engine.models import IterationRecord, IterationStatus, LoopProject, LoopProjectStatus
from src.project import ProjectContext


def _make_handler_context(**overrides) -> HandlerContext:
    import threading

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
        thread_manager=MagicMock(),
        streaming_manager_factory=MagicMock(),
        image_handler_factory=MagicMock(),
        working_dirs={},
        working_dir_lock=threading.Lock(),
        pending_image_keys={},
        pending_image_lock=threading.Lock(),
        enable_streaming=False,
    )
    # Mock settings
    ctx.settings.card_deep_compact_default = False
    ctx.settings.default_reply_mode = "thread"

    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestLoopUI:
    def test_card_view_persistence(self):
        """Verify view state is persisted across toggle actions."""
        ctx = _make_handler_context()
        handler = LoopHandler(ctx)

        # Setup Mock Project and Engine
        proj = LoopProject(name="test_proj", root_path="/tmp/test", project_id="p1")
        proj.status = LoopProjectStatus.RUNNING

        # Add an iteration record
        record = IterationRecord(iteration=1, status=IterationStatus.SUCCESS, summary="Iter 1 Done")
        proj.iterations.append(record)

        mock_engine = MagicMock()
        mock_engine.project = proj
        mock_engine.engine_name = "Coco"

        ctx.loop_engine_manager.get.return_value = mock_engine

        # Mock Reporter
        ctx.loop_reporter.format_iteration_done.return_value = "Iteration 1 Content"
        ctx.loop_reporter.get_iteration_done_title.return_value = "Iter 1 Title"
        ctx.loop_reporter.format_status.return_value = "Status Content"
        ctx.loop_reporter.get_status_title.return_value = "Status Title"
        ctx.loop_reporter._make_progress_bar.return_value = "[====]"
        ctx.loop_reporter.format_status_line.return_value = "Status Line"
        ctx.loop_reporter.format_duration_line.return_value = "Duration Line"
        ctx.loop_reporter.format_criteria_section.return_value = "Criteria Section"
        ctx.loop_reporter.get_progress_info.return_value = {
            "progress_bar": "[=]",
            "is_running": True,
            "is_paused": False,
        }

        handler.reply_message = MagicMock(return_value="msg_thread_root")
        handler.send_message = MagicMock(return_value="msg_thread_root")
        handler.patch_message = MagicMock(return_value=True)  # patch succeeds

        mock_proj_ctx = MagicMock(spec=ProjectContext)  # Using MagicMock to act as ProjectContext duck type
        mock_proj_ctx.project_id = "p1"
        mock_proj_ctx.root_path = "/tmp/test"
        mock_proj_ctx.project_name = "test_proj"

        # 1. Simulate on_iteration_done callback triggering a view update to "iteration_done"
        callbacks = handler.renderer.create_loop_callbacks("msg1", "chat1", mock_proj_ctx)
        callbacks.on_iteration_done(1, record)

        # Verify state is set to iteration_done
        state = handler._get_ui_state("p1")
        assert state["view_mode"] == "iteration_done"
        assert state["view_context"]["iteration_id"] == 1

        # Verify card content was iteration content
        # _create_loop_callbacks sends a NEW message (reply_message) first time
        assert handler.reply_message.called
        content_json = handler.reply_message.call_args[0][1]
        assert "Iteration 1 Content" in content_json

        # 2. Simulate User clicking "Expand Log" (toggle_loop_log)
        # This calls _render_current_view which should respect "iteration_done" view mode
        handler.patch_message.reset_mock()

        # toggle_loop_log will call _render_current_view -> _render_iteration_view
        # _render_iteration_view calls _patch_or_send -> patch_message
        handler.toggle_loop_log("msg_card", "chat1", project=mock_proj_ctx, loop_project_id="p1", expanded=True)

        # Verify state updated
        state = handler._get_ui_state("p1")
        assert state["expanded"] is True
        assert state["view_mode"] == "iteration_done"  # Should remain unchanged

        # Verify patched content is still Iteration Content
        assert handler.patch_message.called
        patch_content = handler.patch_message.call_args[0][1]
        assert "Iteration 1 Content" in patch_content

        # 3. Simulate User clicking "Show Status" (via /loop_status or show_loop_status)
        # This should reset view mode to "status"
        handler.patch_message.reset_mock()
        handler.show_loop_status("msg_cmd", "chat1", project=mock_proj_ctx, origin_message_id="msg_card")

        # Verify state
        state = handler._get_ui_state("p1")
        assert state["view_mode"] == "status"

        # Verify patched content is Status Content
        patch_content = handler.patch_message.call_args[0][1]
        assert "Status Content" in patch_content
