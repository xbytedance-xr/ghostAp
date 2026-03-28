import json
import time
from unittest.mock import MagicMock

from src.card.streaming import StreamingCardManager


def _wait_patch(mock_client, *, min_calls: int = 1, timeout_s: float = 2.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if mock_client.im.v1.message.patch.call_count >= min_calls:
            return True
        time.sleep(0.02)
    return False


def _extract_markdown_content(body: dict) -> str:
    # Streaming cards include multiple lark_md blocks (header/status + content).
    # Pick the longest one as the main output content.
    candidates: list[str] = []
    for el in body.get("body", {}).get("elements", []):
        if el.get("tag") == "div":
            txt = el.get("text", {})
            if txt.get("tag") == "lark_md":
                candidates.append(txt.get("content", ""))
        if el.get("tag") == "markdown":
            candidates.append(el.get("content", ""))
    if not candidates:
        return ""
    return max(candidates, key=lambda s: len(s or ""))


def test_output_is_fully_accessible_via_pagination_sliding_window():
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.code = 0
    mock_resp.msg = "ok"
    mock_client.im.v1.message.patch.return_value = mock_resp

    manager = StreamingCardManager(mock_client)
    manager._max_card_chars = 28000

    card = manager.create_streaming_card(chat_id="c")
    card.message_id = "mid"
    card.visible_chars = 5000
    card.pagination_step = 5000
    manager._cards[card.message_id] = card

    content = ("A" * 60000) + "TAIL"
    manager.update_content(card, content)
    assert _wait_patch(mock_client)

    # initial view: prefix
    req = mock_client.im.v1.message.patch.call_args[0][0]
    body = json.loads(req.request_body.content)
    shown = _extract_markdown_content(body)
    assert "TAIL" not in shown
    assert shown == content[: card.visible_chars]

    # page forward until we can see the tail
    found = False
    for _ in range(30):
        mock_client.im.v1.message.patch.reset_mock()
        manager.increase_pagination(card.message_id)
        assert _wait_patch(mock_client)
        req = mock_client.im.v1.message.patch.call_args[0][0]
        body = json.loads(req.request_body.content)
        shown = _extract_markdown_content(body)
        if "TAIL" in shown:
            found = True
            break
    assert found is True


def test_output_can_still_be_paged_after_close_streaming():
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.code = 0
    mock_resp.msg = "ok"
    mock_client.im.v1.message.patch.return_value = mock_resp

    manager = StreamingCardManager(mock_client)
    manager._max_card_chars = 28000

    card = manager.create_streaming_card(chat_id="c")
    card.message_id = "mid"
    card.visible_chars = 5000
    card.pagination_step = 5000
    manager._cards[card.message_id] = card

    content = ("B" * 60000) + "TAIL"
    assert manager.close_streaming(card, final_content=content) is True

    # When pagination is needed, card should be retained for a while.
    assert card.message_id in manager._cards

    mock_client.im.v1.message.patch.reset_mock()
    manager.increase_pagination(card.message_id)
    assert _wait_patch(mock_client)
