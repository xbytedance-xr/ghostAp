import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.card import CardBuilder
from src.card.models import EngineCardState
from src.feishu.handlers.loop import LoopHandler
from src.feishu.ws_client import FeishuWSClient
from src.loop_engine.models import LoopProject, LoopProjectStatus


class TestLoopInteraction(unittest.TestCase):
    def test_card_builder_generates_loop_actions(self):
        """验证 CardBuilder 使用 action_prefix='loop' 生成 loop_ 前缀的 action"""
        project = LoopProject(name="test_proj", root_path="/tmp", project_id="p1")
        project.status = LoopProjectStatus.RUNNING

        # build_engine_card calls _build_deep_buttons internally
        # We need to ensure we can parse the result even if it's complex
        _, card_content = CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title="Loop Status",
                content="Running...",
                action_prefix="loop",
                show_buttons=True,
                is_executing=True,  # Important: Must set this to True to get control buttons
            ),
        )

        card_json = json.loads(card_content)
        print(json.dumps(card_json, indent=2, ensure_ascii=False))
        actions = []

        # Traverse card elements to find buttons
        # Note: elements are inside body
        elements = []
        if "body" in card_json and "elements" in card_json["body"]:
            elements = card_json["body"]["elements"]
        elif "elements" in card_json:
            elements = card_json["elements"]

        if elements:
            for el in elements:
                # Actions might be nested in columns/divs if responsive layout is used
                # Or they might be top-level "action" elements
                if el["tag"] == "action":
                    for action in el["actions"]:
                        if "value" in action:
                            val = action["value"]
                            # value might be string or dict
                            if isinstance(val, str):
                                try:
                                    val = json.loads(val)
                                except Exception:
                                    pass
                            if isinstance(val, dict) and "action" in val:
                                actions.append(val["action"])
                elif el["tag"] == "div":
                    # Check for fields or text, but buttons are usually in 'action' tag elements
                    # But maybe responsive layout uses something else?
                    pass
                # What if it's a column_set?
                elif el["tag"] == "column_set":
                    print(f"Found column_set: {el}")
                    for col in el.get("columns", []):
                        print(f"  Column: {col}")
                        for col_el in col.get("elements", []):
                            print(f"    Element: {col_el}")
                            # Buttons are elements with tag="button" inside columns
                            if col_el["tag"] == "button":
                                val = col_el.get("value")
                                print(f"      Button value: {val}, type: {type(val)}")
                                if isinstance(val, str):
                                    try:
                                        val = json.loads(val)
                                    except Exception:
                                        pass
                                if isinstance(val, dict) and "action" in val:
                                    actions.append(val["action"])
                                    print(f"      Action found: {val['action']}")
                            # Also handle "action" container inside columns if any (unlikely for buttons in responsive layout)
                            elif col_el["tag"] == "action":
                                for action in col_el["actions"]:
                                    if "value" in action:
                                        val = action["value"]
                                        if isinstance(val, str):
                                            try:
                                                val = json.loads(val)
                                            except Exception:
                                                pass
                                        if isinstance(val, dict) and "action" in val:
                                            actions.append(val["action"])

        # Verify actions start with loop_
        # Loop actions typically include loop_pause, loop_stop, loop_log, loop_card_mode
        self.assertTrue(any(a == "loop_pause" for a in actions), f"Should contain loop_pause, found: {actions}")
        self.assertTrue(any(a == "loop_stop" for a in actions), f"Should contain loop_stop, found: {actions}")
        self.assertTrue(
            all(not a.startswith("deep_") for a in actions), f"Should NOT contain deep_ prefix, found: {actions}"
        )

    def test_loop_handler_uses_loop_prefix(self):
        """验证 LoopHandler 调用 CardBuilder 时传入 action_prefix='loop'"""
        mock_ctx = MagicMock()
        # Mock settings
        mock_ctx.settings.card_deep_compact_default = False

        handler = LoopHandler(mock_ctx)

        # Mock project
        project = LoopProject(name="test_proj", root_path="/tmp", project_id="p1")

        # Mock engine project attributes for renderer
        mock_engine = mock_ctx.loop_engine_manager.get.return_value
        mock_engine.project.satisfied_count = 0
        mock_engine.project.total_criteria = 10
        mock_engine.project.name = "test_proj"

        # Patch CardBuilder.build_engine_card
        with patch("src.card.CardBuilder.build_engine_card") as mock_build:
            mock_build.return_value = ("interactive", "{}")

            # Call show_loop_status which calls build_engine_card
            # Note: We mock reply_message to avoid network calls
            handler.reply_message = MagicMock()
            handler.show_loop_status("msg_id", "chat_id", project)

            # Verify action_prefix="loop" was passed
            mock_build.assert_called()
            call_args = mock_build.call_args
            # Check kwargs
            state = call_args.kwargs.get("state")
            self.assertIsNotNone(state)
            self.assertEqual(state.action_prefix, "loop")

    def test_ws_client_routes_loop_actions(self):
        """验证 FeishuWSClient 正确路由 loop_pause/resume/stop 动作"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
            patch("src.feishu.handlers.LoopHandler"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "app_id"
            mock_settings.app_secret = "app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            # Mock the loop handler instance
            client._loop_handler = MagicMock()

            # Since _init_action_registry uses a lambda that calls self._loop_handler,
            # mocking client._loop_handler works even after registration.

            # Test loop_pause
            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value='{"action":"loop_pause","project_id":"p1"}', tag="button", name="pause"
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)

            # Verify handler called
            client._loop_handler.handle_card_action.assert_called()
            args = client._loop_handler.handle_card_action.call_args
            # args: (mid, cid, type, val)
            self.assertEqual(args[0][0], "om_1")
            self.assertEqual(args[0][1], "oc_1")
            # type arg might be None or passed depending on implementation
            # In ws_client: lambda ..., type=None: ... handle_card_action(..., type, ...)
            # Wait, the lambda signature in ws_client is:
            # lambda mid, cid, pid, val, type=None: ...
            # And it's called with:
            # handler(open_message_id, open_chat_id, project_id, value, type=action_type)
            # So type should be "loop_pause"
            self.assertEqual(args[0][2], "loop_pause")


if __name__ == "__main__":
    unittest.main()
