import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.feishu.handlers.spec import SpecHandler
from src.feishu.renderers.spec_renderer import SpecRenderer
from src.feishu.ws_client import FeishuWSClient
from src.spec_engine.engine import SpecEngineCallbacks
from src.spec_engine.reporter import SpecReporter


class TestSpecInteraction(unittest.TestCase):
    def test_spec_handler_uses_standard_dispatch(self):
        """验证 SpecHandler 调用 _dispatch_standard_card_action"""
        mock_ctx = MagicMock()
        mock_ctx.settings.card_deep_compact_default = False

        handler = SpecHandler(mock_ctx)
        # Mock the dispatch method
        handler._dispatch_standard_card_action = MagicMock(return_value=True)

        # Test spec_pause action
        handler.handle_card_action("mid", "cid", "spec_pause", {"action": "spec_pause", "project_id": "p1"})

        # Verify dispatch called with correct args
        handler._dispatch_standard_card_action.assert_called_once()
        call_args = handler._dispatch_standard_card_action.call_args
        self.assertEqual(call_args[1]["prefix"], "spec")
        self.assertIn("spec_pause", call_args[1]["action_map"])
        self.assertIn("spec_resume", call_args[1]["action_map"])
        self.assertIn("spec_stop", call_args[1]["action_map"])
        self.assertEqual(call_args[1]["toggle_log_method"], handler.toggle_spec_log)
        self.assertEqual(call_args[1]["toggle_ac_method"], handler.toggle_spec_ac)
        self.assertEqual(call_args[1]["switch_mode_method"], handler.switch_spec_card_mode)

    def test_standard_dispatch_handles_expand_ac(self):
        """验证 BaseHandler 标准分发支持 *_expand_ac / *_collapse_ac"""
        mock_ctx = MagicMock()
        mock_ctx.settings.card_deep_compact_default = False

        handler = SpecHandler(mock_ctx)
        toggle_ac = MagicMock()
        project = MagicMock()

        handled = handler._dispatch_standard_card_action(
            "mid",
            "cid",
            "spec_expand_ac",
            {"deep_project_id": "root"},
            prefix="spec",
            action_map={},
            toggle_ac_method=toggle_ac,
            project=project,
        )

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
    """验收：错误提示含关键字且错误卡片包含可用的重试按钮（携带 task_id）。"""
    mock_handler = MagicMock()
    mock_handler.ctx = MagicMock()
    mock_handler.ctx.spec_reporter = SpecReporter()
    mock_handler.settings = MagicMock()
    mock_handler.settings.card_deep_compact_default = False
    mock_handler.settings.default_reply_mode = "chat"
    mock_handler.settings.deep_stream_interval = 0
    mock_handler.settings.deep_stream_min_chars = 0
    mock_handler.ensure_request_id = MagicMock(return_value=None)
    mock_handler.get_working_dir = MagicMock(return_value="/tmp")

    sent = {}

    def _send_message(chat_id, card_content, msg_type, origin_message_id=None, request_id=None):
        sent["card"] = card_content
        sent["msg_type"] = msg_type
        return "m2"

    mock_handler.send_message = MagicMock(side_effect=_send_message)
    mock_handler.reply_message = MagicMock(return_value="m2")
    mock_handler.patch_message = MagicMock(return_value=False)
    mock_handler.add_reaction = MagicMock()

    renderer = SpecRenderer(mock_handler)
    callbacks: SpecEngineCallbacks = renderer.create_spec_callbacks(
        message_id="mid",
        chat_id="cid",
        project=None,
        engine_name="Coco",
    )

    error = "Spec执行异常: Phase build 失败，任务已保存(task_id=f5f3dcb4): Internal error"
    callbacks.on_error(error)

    assert "card" in sent
    assert "Phase build 失败" in sent["card"]
    assert "Internal error" in sent["card"]
    assert "f5f3dcb4" in sent["card"]

    payload = json.loads(sent["card"])

    def _walk(x):
        if isinstance(x, dict):
            yield x
            for v in x.values():
                yield from _walk(v)
        elif isinstance(x, list):
            for v in x:
                yield from _walk(v)

    hits = [
        d for d in _walk(payload) if d.get("tag") == "button" and (d.get("value") or {}).get("action") == "spec_retry"
    ]
    assert hits, "missing spec_retry button"
    assert (hits[0].get("value") or {}).get("task_id") == "f5f3dcb4"
