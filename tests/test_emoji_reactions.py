from types import SimpleNamespace
from unittest.mock import MagicMock

from src.feishu.emoji import EmojiReaction
from src.feishu.handlers.base import BaseHandler
from src.feishu.im_client import FeishuIMClient


def test_only_typing_reaction_is_allowed():
    assert EmojiReaction.should_send(EmojiReaction.on_processing())

    blocked = [
        EmojiReaction.on_smart_mode(),
        EmojiReaction.on_coco_mode(),
        EmojiReaction.on_multi_task_start(),
        EmojiReaction.on_multi_task_done(),
        EmojiReaction.on_error(),
    ]
    assert all(not EmojiReaction.should_send(emoji) for emoji in blocked)


def test_base_handler_filters_non_typing_reactions():
    handler = BaseHandler.__new__(BaseHandler)
    handler.im_client = MagicMock()

    handler.add_reaction("msg_1", EmojiReaction.on_smart_mode())
    handler.im_client.add_reaction.assert_not_called()

    handler.add_reaction("msg_1", EmojiReaction.on_processing())
    handler.im_client.add_reaction.assert_called_once_with("msg_1", "Typing")


def test_im_client_filters_non_typing_reactions_before_api_call():
    api_client_factory = MagicMock()
    settings = SimpleNamespace(im_api_max_retries=1)
    client = FeishuIMClient(api_client_factory, settings)

    client.add_reaction("msg_1", EmojiReaction.on_multi_task_done())
    api_client_factory.assert_not_called()

    client._execute_with_retry = MagicMock()
    client.add_reaction("msg_1", EmojiReaction.on_processing())

    api_client_factory.assert_called_once()
    client._execute_with_retry.assert_called_once()
