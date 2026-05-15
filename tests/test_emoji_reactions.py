from types import SimpleNamespace
from unittest.mock import MagicMock

from src.feishu.emoji import EmojiReaction
from src.feishu.handlers.base import BaseHandler
from src.feishu.im_client import FeishuIMClient


def test_task_lifecycle_reactions_are_allowed():
    assert EmojiReaction.should_send(EmojiReaction.on_processing())
    assert EmojiReaction.should_send(EmojiReaction.on_message_received())
    assert EmojiReaction.should_send(EmojiReaction.on_coco_mode())
    assert EmojiReaction.should_send(EmojiReaction.on_multi_task_done())

    blocked = [
        EmojiReaction.on_smart_mode(),
        EmojiReaction.on_multi_task_start(),
        EmojiReaction.on_error(),
    ]
    assert all(not EmojiReaction.should_send(emoji) for emoji in blocked)


def test_base_handler_keeps_task_lifecycle_reactions():
    handler = BaseHandler.__new__(BaseHandler)
    handler.im_client = MagicMock()

    handler.add_reaction("msg_1", EmojiReaction.on_smart_mode())
    handler.im_client.add_reaction.assert_not_called()

    handler.add_reaction("msg_1", EmojiReaction.on_processing())
    handler.im_client.add_reaction.assert_called_once_with("msg_1", "Typing")

    handler.add_reaction("msg_1", EmojiReaction.on_coco_mode())
    handler.im_client.add_reaction.assert_called_with("msg_1", "OnIt")

    handler.add_reaction("msg_1", EmojiReaction.on_multi_task_done())
    handler.im_client.add_reaction.assert_called_with("msg_1", "PARTY")


def test_im_client_keeps_task_lifecycle_reactions_before_api_call():
    api_client_factory = MagicMock()
    settings = SimpleNamespace(im_api_max_retries=1)
    client = FeishuIMClient(api_client_factory, settings)

    client.add_reaction("msg_1", EmojiReaction.on_multi_task_start())
    api_client_factory.assert_not_called()

    client._execute_with_retry = MagicMock()
    client.add_reaction("msg_1", EmojiReaction.on_processing())
    client.add_reaction("msg_1", EmojiReaction.on_message_received())
    client.add_reaction("msg_1", EmojiReaction.on_multi_task_done())

    assert api_client_factory.call_count == 3
    assert client._execute_with_retry.call_count == 3
