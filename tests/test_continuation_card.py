"""Tests for auto-continuation card logic in StreamingCardManager."""

import json
import time
from unittest.mock import MagicMock, patch

from src.card.streaming import StreamingCard, StreamingCardManager


def _make_manager(max_card_chars: int = 28000, continuation_enabled: bool = True, collapsible_enabled: bool = False):
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.code = 0
    mock_resp.msg = "ok"
    mock_resp.data.message_id = "new_mid_1"
    mock_client.im.v1.message.patch.return_value = mock_resp
    mock_client.im.v1.message.reply.return_value = mock_resp
    manager = StreamingCardManager(mock_client)
    manager._max_card_chars = max_card_chars
    manager._settings.card_continuation_enabled = continuation_enabled
    manager._settings.card_collapsible_enabled = collapsible_enabled
    return manager, mock_client


def _make_card(manager, message_id="mid", chat_id="c"):
    card = manager.create_streaming_card(chat_id=chat_id)
    card.message_id = message_id
    card.reply_to_message_id = "orig_mid"
    manager._cards[message_id] = card
    return card


class TestContinuationNotTriggered:
    def test_below_threshold(self):
        manager, _ = _make_manager(max_card_chars=28000)
        card = _make_card(manager)
        content = "A" * 10000  # way below 28000 * 0.85 = 23800
        result = manager._maybe_create_continuation(card, content)
        assert result is False
        assert card.message_id == "mid"
        assert card.continuation_index == 0

    def test_continuation_disabled(self):
        manager, _ = _make_manager(continuation_enabled=False)
        card = _make_card(manager)
        content = "A" * 30000  # above threshold but disabled
        result = manager._maybe_create_continuation(card, content)
        assert result is False

    def test_max_cards_reached(self):
        manager, _ = _make_manager()
        card = _make_card(manager)
        card.continuation_index = 10  # at max
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is False


class TestContinuationTriggered:
    def test_creates_continuation(self):
        manager, mock_client = _make_manager(max_card_chars=28000)
        card = _make_card(manager)
        content = "A" * 30000  # above 28000 * 0.85 = 23800
        result = manager._maybe_create_continuation(card, content)
        assert result is True
        # Card should be mutated in-place
        assert card.message_id == "new_mid_1"
        assert card.continuation_index == 1
        assert card.full_content == ""
        assert card.last_content == ""
        assert "(续 #1)" in card.title

    def test_old_card_removed_from_dict(self):
        manager, _ = _make_manager()
        card = _make_card(manager)
        content = "A" * 30000
        manager._maybe_create_continuation(card, content)
        assert "mid" not in manager._cards
        assert "new_mid_1" in manager._cards

    def test_continuation_index_increments(self):
        manager, mock_client = _make_manager()
        card = _make_card(manager)
        card.continuation_index = 3

        # Return different message IDs for each reply
        resp1 = MagicMock()
        resp1.success.return_value = True
        resp1.data.message_id = "new_mid_4"
        mock_client.im.v1.message.reply.return_value = resp1
        mock_client.im.v1.message.patch.return_value = resp1

        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is True
        assert card.continuation_index == 4
        assert "(续 #4)" in card.title

    def test_patch_close_called(self):
        manager, mock_client = _make_manager()
        card = _make_card(manager)
        content = "A" * 30000
        manager._maybe_create_continuation(card, content)
        # PATCH should have been called (to close old card)
        assert mock_client.im.v1.message.patch.call_count >= 1
        # Reply should have been called (to create new card)
        assert mock_client.im.v1.message.reply.call_count >= 1


class TestContinuationFailure:
    def test_old_card_patch_failure_still_succeeds(self):
        """Closing old card fails but new card was created — continuation succeeds."""
        manager, mock_client = _make_manager()
        card = _make_card(manager)
        ok_resp = MagicMock()
        ok_resp.success.return_value = True
        ok_resp.data.message_id = "new_mid_1"
        fail_resp = MagicMock()
        fail_resp.success.return_value = False
        fail_resp.code = 500
        fail_resp.msg = "error"
        # reply (create new card) succeeds, patch (close old) fails
        mock_client.im.v1.message.reply.return_value = ok_resp
        mock_client.im.v1.message.patch.return_value = fail_resp
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        # New card was created, so continuation succeeds despite old card close failure
        assert result is True
        assert card.message_id == "new_mid_1"
        assert card.continuation_index == 1

    def test_reply_failure_returns_false(self):
        manager, mock_client = _make_manager()
        card = _make_card(manager)
        fail_resp = MagicMock()
        fail_resp.success.return_value = False
        fail_resp.code = 500
        fail_resp.msg = "error"
        # reply (create new card) fails — old card unchanged
        mock_client.im.v1.message.reply.return_value = fail_resp
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is False
        # Card should not be mutated
        assert card.message_id == "mid"
        assert card.continuation_index == 0

    def test_reply_exception_returns_false(self):
        manager, mock_client = _make_manager()
        card = _make_card(manager)
        mock_client.im.v1.message.reply.side_effect = RuntimeError("network")
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is False
        # Card should not be mutated
        assert card.message_id == "mid"
        assert card.continuation_index == 0

    def test_old_card_patch_exception_still_succeeds(self):
        """Closing old card throws but new card was created — continuation succeeds."""
        manager, mock_client = _make_manager()
        card = _make_card(manager)
        ok_resp = MagicMock()
        ok_resp.success.return_value = True
        ok_resp.data.message_id = "new_mid_1"
        # reply succeeds
        mock_client.im.v1.message.reply.return_value = ok_resp
        # patch throws
        mock_client.im.v1.message.patch.side_effect = RuntimeError("network")
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is True
        assert card.message_id == "new_mid_1"


class TestCollapsibleFallback:
    def test_collapsible_patch_failed_flag(self):
        """When _collapsible_patch_failed is set, update_structured falls back to flat."""
        manager, _ = _make_manager(collapsible_enabled=True)
        card = _make_card(manager)
        card._collapsible_patch_failed = True

        from src.acp.renderer import ContentSection, RenderedContent
        rc = RenderedContent(sections=[ContentSection(section_type="text", markdown="hello")])
        manager.update_structured(card, rc)
        # structured_sections should NOT be set when fallback is active
        assert card.structured_sections is None

    def test_collapsible_disabled_skips_structured(self):
        manager, _ = _make_manager(collapsible_enabled=False)
        card = _make_card(manager)

        from src.acp.renderer import ContentSection, RenderedContent
        rc = RenderedContent(sections=[ContentSection(section_type="text", markdown="hello")])
        manager.update_structured(card, rc)
        assert card.structured_sections is None


class TestContinuationCallback:
    def test_on_continuation_callback_invoked(self):
        """Verify on_continuation callback is called when continuation triggers."""
        manager, mock_client = _make_manager(max_card_chars=28000)
        card = _make_card(manager)

        callback_called = [False]

        def my_callback():
            callback_called[0] = True

        card.on_continuation = my_callback
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is True
        assert callback_called[0] is True

    def test_on_continuation_callback_exception_handled(self):
        """Verify callback exceptions don't prevent continuation from succeeding."""
        manager, mock_client = _make_manager(max_card_chars=28000)
        card = _make_card(manager)

        def bad_callback():
            raise RuntimeError("callback error")

        card.on_continuation = bad_callback
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        # Continuation should still succeed despite callback error
        assert result is True
        assert card.continuation_index == 1

    def test_no_callback_still_works(self):
        """Verify continuation works fine without on_continuation set (default None)."""
        manager, mock_client = _make_manager(max_card_chars=28000)
        card = _make_card(manager)
        assert card.on_continuation is None
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is True
        assert card.continuation_index == 1

    def test_callback_with_renderer_integration(self):
        """End-to-end: callback resets renderer, new events produce only summary + new content."""
        from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo
        from src.acp.renderer import ACPEventRenderer

        renderer = ACPEventRenderer()

        # Simulate accumulated state in renderer
        renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="old content "))
        tc = ToolCallInfo(id="t1", title="Read", kind="read", status="completed", locations=["a.py"])
        renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))

        # Register the callback (same pattern as programming.py)
        manager, mock_client = _make_manager(max_card_chars=28000)
        card = _make_card(manager)

        def _on_continuation():
            summary = renderer.render_continuation_summary()
            renderer.reset_for_continuation(summary)

        card.on_continuation = _on_continuation

        # Trigger continuation
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is True

        # After continuation, renderer should be reset with summary
        new_rendered = renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="new stuff"))
        assert "new stuff" in new_rendered
        assert "前文摘要" in new_rendered
        # Old content should NOT appear in the new render
        assert "old content" not in new_rendered


class TestStalePage:
    """Verify that old card receives a stub message after continuation."""

    def test_old_card_patched_with_stub(self):
        """When continuation triggers, old card PATCH body should contain the stale stub text."""
        manager, mock_client = _make_manager(max_card_chars=28000)
        card = _make_card(manager)
        content = "A" * 30000
        result = manager._maybe_create_continuation(card, content)
        assert result is True

        # The patch call closes the old card — inspect its content
        patch_call = mock_client.im.v1.message.patch
        assert patch_call.call_count >= 1
        patch_req = patch_call.call_args[0][0]
        patch_body = patch_req.body.content
        card_json = json.loads(patch_body)
        # Find markdown element in the card body
        body_elements = card_json.get("body", {}).get("elements", [])
        all_text = json.dumps(body_elements, ensure_ascii=False)
        assert "此页已收起" in all_text
        # Should NOT contain the original long content
        assert "AAAAAAAAAA" not in all_text

    def test_stub_text_from_ui_text(self):
        """Stub text should match the UI_TEXT constant."""
        from src.card.styles import UI_TEXT
        expected = UI_TEXT.get("continuation_stale_stub", "")
        assert "此页已收起" in expected
        assert "下方" in expected
