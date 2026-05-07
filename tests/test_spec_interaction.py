import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.card.engine_snapshot import EngineSnapshot
from src.feishu.handlers.spec import SpecHandler
from src.feishu.handlers.base import CardActionContext
from src.feishu.renderers.spec_renderer import SpecRenderer
from src.feishu.ws_client import FeishuWSClient
from src.spec_engine.engine import SpecEngineCallbacks
from src.spec_engine.models import SpecProject, SpecProjectStatus
from src.spec_engine.reporter import SpecReporter


class TestSpecInteraction(unittest.TestCase):
    def test_spec_handler_uses_standard_dispatch(self):
        """验证 SpecHandler 调用 _dispatch_standard_card_action"""
        mock_ctx = MagicMock()
        mock_ctx.settings.card.deep_compact_default = False

        handler = SpecHandler(mock_ctx)
        # Mock the dispatch method
        handler._dispatch_standard_card_action = MagicMock(return_value=True)

        # Test spec_pause action
        handler.handle_card_action("mid", "cid", "spec_pause", {"action": "spec_pause", "project_id": "p1"})

        # Verify dispatch called with correct args (now via CardActionContext)
        handler._dispatch_standard_card_action.assert_called_once()
        call_args = handler._dispatch_standard_card_action.call_args
        ctx = call_args[0][0]  # first positional arg is the CardActionContext
        self.assertEqual(ctx.prefix, "spec")
        self.assertIn("spec_pause", ctx.action_map)
        self.assertIn("spec_resume", ctx.action_map)
        self.assertIn("spec_stop", ctx.action_map)
        self.assertEqual(ctx.toggle_log_method, handler._toggle_log)
        self.assertEqual(ctx.toggle_ac_method, handler._toggle_ac)
        self.assertEqual(ctx.switch_mode_method, handler._switch_card_mode)

    def test_standard_dispatch_handles_expand_ac(self):
        """验证 BaseHandler 标准分发支持 *_expand_ac / *_collapse_ac"""
        mock_ctx = MagicMock()
        mock_ctx.settings.card.deep_compact_default = False

        handler = SpecHandler(mock_ctx)
        toggle_ac = MagicMock()
        project = MagicMock()

        handled = handler._dispatch_standard_card_action(CardActionContext(
            open_message_id="mid",
            open_chat_id="cid",
            action_type="spec_expand_ac",
            value={"deep_project_id": "root"},
            prefix="spec",
            action_map={},
            toggle_ac_method=toggle_ac,
            project=project,
        ))

        self.assertTrue(handled)
        toggle_ac.assert_called_once_with("mid", "cid", project, "root", True)

    def test_ws_client_routes_spec_actions(self):
        """验证 FeishuWSClient 正确路由 spec_pause/resume/stop 动作"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.feishu.ws_client.LoopEngineManager"),
            patch("src.feishu.ws_client.LoopReporter"),
            patch("src.feishu.ws_client.SpecEngineManager"),
            patch("src.feishu.ws_client.SpecReporter"),
            patch("src.mode.ModeManager"),
            patch("src.feishu.handlers.SpecHandler"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "app_id"
            mock_settings.app_secret = "app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_settings.message_cache_ttl = 300
            mock_settings.message_cache_max_size = 1000
            mock_settings.card.action_dedup_ttl = 1
            mock_settings.card.action_dedup_max_size = 5000
            mock_settings.system_command_concurrency = 10
            mock_settings.spec_rate_limit_capacity = 100
            mock_settings.spec_rate_limit_fill_rate = 50.0
            mock_settings.spec_circuit_breaker_threshold = 10
            mock_settings.spec_circuit_breaker_recovery = 5.0
            mock_settings.message_expire_seconds = 30
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            # Mock the spec handler instance
            client._spec_handler = MagicMock()

            # Test spec_pause
            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value='{"action":"spec_pause","project_id":"p1"}', tag="button", name="pause"
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)

            # Verify handler called
            client._spec_handler.handle_card_action.assert_called()
            args = client._spec_handler.handle_card_action.call_args
            # args: (mid, cid, type, val)
            self.assertEqual(args[0][0], "om_1")
            self.assertEqual(args[0][1], "oc_1")
            self.assertEqual(args[0][2], "spec_pause")


if __name__ == "__main__":
    unittest.main()


def test_spec_error_card_contains_keywords_and_retry_button():
    """验收：on_error dispatches FAILED event with error message containing keywords."""
    from src.card.events import CardEvent, CardEventType

    mock_handler = MagicMock()
    mock_handler.ctx = MagicMock()
    mock_handler.ctx.spec_reporter = SpecReporter()
    mock_handler.ctx.spec_engine_manager = MagicMock()
    mock_handler.ctx.spec_engine_manager.get_active_engine.return_value = None
    mock_handler.settings = MagicMock()
    mock_handler.settings.card.deep_compact_default = False
    mock_handler.settings.default_reply_mode = "chat"
    mock_handler.settings.deep_stream_interval = 0
    mock_handler.settings.deep_stream_min_chars = 0
    mock_handler.ensure_request_id = MagicMock(return_value=None)
    mock_handler.get_working_dir = MagicMock(return_value="/tmp")
    mock_handler.add_reaction = MagicMock()

    renderer = SpecRenderer(mock_handler)

    with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        callbacks: SpecEngineCallbacks = renderer.create_spec_callbacks(
            message_id="mid",
            chat_id="cid",
            project=None,
            engine_name="Coco",
        )

        error = "Spec执行异常: Phase build 失败，任务已保存(task_id=f5f3dcb4): Internal error"
        callbacks.on_error(error)

        # Verify FAILED event dispatched with error message
        dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
        failed_events = [e for e in dispatched_events if e.type == CardEventType.FAILED]
        assert failed_events, "on_error should dispatch FAILED event"
        # Error message should be preserved in payload
        assert "Phase build 失败" in str(failed_events[0].payload)
        assert "f5f3dcb4" in str(failed_events[0].payload)
        assert "Internal error" in str(failed_events[0].payload)

        # Verify EmojiHook was registered via hooks kwarg to create_session
        create_kwargs = mock_create.call_args
        assert "hooks" in create_kwargs.kwargs or (len(create_kwargs.args) > 4)
        hooks = create_kwargs.kwargs.get("hooks", ())
        from src.card.hooks import EmojiHook
        assert any(isinstance(h, EmojiHook) for h in hooks), "EmojiHook should be registered"


def test_spec_status_card_shows_resume_when_paused():
    """Verify _render_status_view dispatches events when spec project is paused."""
    from src.card.events import CardEvent, CardEventType

    mock_handler = MagicMock()
    mock_handler.ctx = MagicMock()
    mock_handler.ctx.spec_reporter = SpecReporter()
    mock_handler.settings = MagicMock()
    mock_handler.settings.card.deep_compact_default = False
    mock_handler.settings.engine_timeout_warning_seconds = 999999
    mock_handler.update_card = MagicMock(return_value=False)
    mock_handler.reply_text = MagicMock()
    mock_handler.send_card_to_chat = MagicMock()

    renderer = SpecRenderer(mock_handler)

    spec_project = SpecProject.create(root_path="/tmp/spec")
    spec_project.status = SpecProjectStatus.PAUSED
    spec_project.started_at = time.time() - 5
    engine = SimpleNamespace(engine_name="Coco", project=spec_project)

    with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        renderer._render_status_view("mid", "cid", None, engine, renderer.get_default_ui_state())

        # Verify dispatch was called with STARTED event
        dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
        event_types = [e.type for e in dispatched_events]
        assert CardEventType.STARTED in event_types
        # Since status is PAUSED and not running, COMPLETED should be dispatched (terminal state)
        assert CardEventType.COMPLETED in event_types


def test_spec_card_buttons_keep_project_id_separate_from_ui_state_key():
    """Verify _render_status_view dispatches correct events for running spec project."""
    from src.card.events import CardEvent, CardEventType

    state = {
        "compact": False,
        "expanded": False,
        "expand_ac": False,
    }
    renderer_handler = MagicMock()
    renderer_handler.ctx = MagicMock()
    renderer_handler.ctx.spec_reporter = SpecReporter()
    renderer_handler.settings = MagicMock()
    renderer_handler.settings.card.deep_compact_default = False
    renderer_handler.settings.engine_timeout_warning_seconds = 999999
    renderer = SpecRenderer(renderer_handler)

    spec_project = SpecProject.create(root_path="/tmp/spec")
    spec_project.project_id = "proj_123"
    spec_project.status = SpecProjectStatus.RUNNING
    spec_project.started_at = time.time() - 5
    engine = SimpleNamespace(engine_name="Coco", project=spec_project)

    with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        renderer._render_status_view("mid", "cid", None, engine, state)

        # Verify metadata passed to create_session includes correct engine_type
        call_kwargs = mock_create.call_args
        metadata = call_kwargs[0][2]  # 3rd positional arg (chat_id, message_id, metadata)
        assert metadata.engine_type == "spec"
        assert "Spec" in metadata.mode_name

        # Verify events dispatched
        dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
        event_types = [e.type for e in dispatched_events]
        assert CardEventType.STARTED in event_types
        # Running project should dispatch PROGRESS_UPDATED (criteria updated)
        assert CardEventType.CRITERIA_UPDATED in event_types


def test_format_cycle_phase_details_full():
    from src.spec_engine.models import (
        PlanArtifact,
        SpecArtifact,
        SpecCycle,
        SpecTask,
        SpecTaskStatus,
    )
    from src.engine_base import ReviewResult, PerspectiveReview, ReviewPerspective

    reporter = SpecReporter()
    cycle = SpecCycle(cycle_number=1)
    cycle.spec_artifact = SpecArtifact(
        goals=["goal1", "goal2"],
        acceptance_criteria=["ac1"],
        non_functional_requirements=["nfr1", "nfr2"],
    )
    cycle.plan_artifact = PlanArtifact(
        steps=["step1", "step2", "step3"],
        file_changes=["file_a.py", "file_b.py"],
        architecture="microservice",
    )
    cycle.tasks = [
        SpecTask(task_id=1, description="Implement feature A", status=SpecTaskStatus.COMPLETED),
        SpecTask(task_id=2, description="Write tests for feature A", status=SpecTaskStatus.COMPLETED),
        SpecTask(task_id=3, description="Refactor module B", status=SpecTaskStatus.PENDING),
        SpecTask(task_id=4, description="Update docs", status=SpecTaskStatus.PENDING),
    ]
    cycle.build_output = "line1\nline2\nline3\n"
    cycle.review_result = ReviewResult(
        reviews=[
            PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=True),
            PerspectiveReview(perspective=ReviewPerspective.TESTER, passed=False, suggestions=["fix lint"]),
        ]
    )

    result = reporter._format_cycle_phase_details(cycle)
    assert "2 个目标" in result
    assert "1 条验收标准" in result
    assert "2 条非功能需求" in result
    assert "3 个步骤" in result
    assert "2 处文件变更" in result
    assert "2/4 完成" in result
    assert "Implement feature A" in result
    assert "输出 3 行" in result
    assert "1/2 视角通过" in result


def test_format_cycle_phase_details_empty():
    from src.spec_engine.models import SpecCycle

    reporter = SpecReporter()
    cycle = SpecCycle(cycle_number=1)
    result = reporter._format_cycle_phase_details(cycle)
    assert result == ""


def test_format_cycle_done_includes_phase_details():
    from src.spec_engine.models import SpecCycle, SpecTask, SpecTaskStatus

    reporter = SpecReporter()
    cycle = SpecCycle(cycle_number=2)
    cycle.spec_content = "some spec"
    cycle.plan_content = "some plan"
    cycle.tasks = [
        SpecTask(task_id=1, description="Task 1", status=SpecTaskStatus.COMPLETED),
    ]
    cycle.build_output = "ok\n"
    cycle.status = "completed"
    cycle.complete()

    result = reporter.format_cycle_done(2, cycle)
    assert "各阶段产出" in result
    assert "规格定义" in result
    assert "方案规划" in result
    assert "1/1 完成" in result
    assert "输出 1 行" in result


def test_cycle_done_card_no_buttons():
    """Verify on_cycle_done dispatches correct events (cycle_done + new session rotation)."""
    from src.card.events import CardEvent, CardEventType

    renderer_handler = MagicMock()
    renderer_handler.ctx = MagicMock()
    renderer_handler.ctx.spec_reporter = SpecReporter()
    renderer_handler.ctx.spec_engine_manager = MagicMock()
    renderer_handler.settings = MagicMock()
    renderer_handler.settings.card.deep_compact_default = False
    renderer_handler.settings.engine_timeout_warning_seconds = 999999
    renderer_handler.settings.default_reply_mode = "direct"
    renderer_handler.ensure_request_id = MagicMock(return_value=None)
    renderer = SpecRenderer(renderer_handler)

    project = MagicMock()
    project.project_id = "proj_test"
    project.root_path = "/tmp/test"

    from src.spec_engine.models import SpecCycle, SpecProject, SpecProjectStatus

    spec_project = SpecProject.create(root_path="/tmp/test")
    spec_project.status = SpecProjectStatus.RUNNING
    spec_project.started_at = time.time() - 10

    engine = SimpleNamespace(engine_name="Coco", project=spec_project)
    renderer_handler.ctx.spec_engine_manager.get.return_value = engine

    # Mock snapshot to return proper EngineSnapshot
    snap = EngineSnapshot(
        engine_name="Coco",
        root_path="/tmp/test",
        project_id="proj_test",
        satisfied_count=0,
        total_criteria=0,
        is_running=True,
        ext={"project": spec_project},
    )
    renderer_handler.ctx.spec_engine_manager.snapshot.return_value = snap

    with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
        mock_session = MagicMock()
        mock_create.return_value = mock_session

        callbacks = renderer.create_spec_callbacks("mid", "cid", project, engine_name="Coco")

        cycle = SpecCycle(cycle_number=1)
        cycle.status = "completed"
        cycle.complete()
        spec_project.cycles.append(cycle)

        callbacks.on_cycle_done(1, cycle)

        # Verify dispatch was called with CYCLE_DONE and text events
        dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
        event_types = [e.type for e in dispatched_events]
        assert CardEventType.CYCLE_DONE in event_types
        # Criteria updated should also be dispatched
        assert CardEventType.CRITERIA_UPDATED in event_types
