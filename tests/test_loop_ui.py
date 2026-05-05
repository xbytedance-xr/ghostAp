from unittest.mock import MagicMock, patch

from src.card.engine_snapshot import EngineSnapshot
from src.card.events import CardEventType
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
        image_handler_factory=MagicMock(),
        working_dirs={},
        working_dir_lock=threading.Lock(),
        pending_image_keys={},
        pending_image_lock=threading.Lock(),
        enable_streaming=False,
    )
    # Mock settings
    ctx.settings.card.deep_compact_default = False
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

        # Setup EngineSnapshot for the new snapshot-based renderer API
        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/test",
            satisfied_count=0,
            total_criteria=0,
            is_running=True,
            ext={"project": proj},
        )
        ctx.loop_engine_manager.snapshot.return_value = snap

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

        handler.reply_card = MagicMock(return_value="msg_thread_root")
        handler.send_card_to_chat = MagicMock(return_value="msg_thread_root")
        handler.update_card = MagicMock(return_value=True)  # patch succeeds

        mock_proj_ctx = MagicMock(spec=ProjectContext)  # Using MagicMock to act as ProjectContext duck type
        mock_proj_ctx.project_id = "p1"
        mock_proj_ctx.root_path = "/tmp/test"
        mock_proj_ctx.project_name = "test_proj"

        # 1. Simulate on_iteration_done callback triggering a view update to "iteration_done"
        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = handler.renderer.create_loop_callbacks("msg1", "chat1", mock_proj_ctx)
            callbacks.on_iteration_done(1, record)

        # Verify state is set to iteration_done
        state = handler._get_ui_state("p1")
        assert state["view_mode"] == "iteration_done"
        assert state["view_context"]["iteration_id"] == 1

        # Verify CardSession dispatched CYCLE_DONE on the first session, then STARTED + TEXT_DELTA on new
        # (session boundary semantics verified in test_loop_patch.py)
        assert mock_session.dispatch.called

        # 2. Simulate User clicking "Expand Log" (_toggle_log)
        # This calls render_current_view which should respect "iteration_done" view mode
        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create2:
            mock_session2 = MagicMock()
            mock_create2.return_value = mock_session2

            handler._toggle_log("msg_card", "chat1", project=mock_proj_ctx, engine_project_id="p1", expanded=True)

            # Verify state updated
            state = handler._get_ui_state("p1")
            assert state["expanded"] is True
            assert state["view_mode"] == "iteration_done"  # Should remain unchanged

            # Verify session dispatched events including iteration content
            dispatched_events = [call[0][0] for call in mock_session2.dispatch.call_args_list]
            event_types = [e.type for e in dispatched_events]
            assert CardEventType.STARTED in event_types
            assert CardEventType.TEXT_DELTA in event_types

        # 3. Simulate User clicking "Show Status" (via /loop_status or show_loop_status)
        # This should reset view mode to "status"
        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create3:
            mock_session3 = MagicMock()
            mock_create3.return_value = mock_session3

            handler.show_loop_status("msg_cmd", "chat1", project=mock_proj_ctx, origin_message_id="msg_card")

            # Verify state
            state = handler._get_ui_state("p1")
            assert state["view_mode"] == "status"

            # Verify session dispatched events
            dispatched_events = [call[0][0] for call in mock_session3.dispatch.call_args_list]
            event_types = [e.type for e in dispatched_events]
            assert CardEventType.STARTED in event_types
            assert CardEventType.TEXT_DELTA in event_types
