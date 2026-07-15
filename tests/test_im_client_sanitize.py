"""Tests for surrogate sanitization and file helpers in FeishuIMClient."""

import json
from unittest.mock import MagicMock

import pytest

from src.feishu.im_client import FeishuIMClient, _sanitize_content


class TestSanitizeContent:
    """Test _sanitize_content handles surrogate code points."""

    def test_clean_string_passes_through(self):
        """Normal strings are returned unchanged."""
        assert _sanitize_content("hello world") == "hello world"

    def test_chinese_passes_through(self):
        """Chinese characters pass through unchanged."""
        text = "你好世界 🎉 测试"
        assert _sanitize_content(text) == text

    def test_emoji_passes_through(self):
        """Proper emoji (not surrogates) pass through unchanged."""
        text = "status: ✅ done 🚀"
        assert _sanitize_content(text) == text

    def test_surrogate_replaced(self):
        """Unpaired surrogate code points are replaced."""
        # Create a string with an unpaired surrogate using surrogatepass
        bad = "hello \ud800 world"
        result = _sanitize_content(bad)
        # The surrogate should be replaced (not present in result)
        assert "\ud800" not in result
        assert "hello" in result
        assert "world" in result

    def test_surrogate_pair_replaced(self):
        """Surrogate pairs in isolation are handled."""
        bad = "prefix𐀀suffix"
        result = _sanitize_content(bad)
        # Should not raise, and should contain prefix/suffix
        assert "prefix" in result
        assert "suffix" in result

    def test_empty_string(self):
        """Empty string returns empty."""
        assert _sanitize_content("") == ""

    def test_json_content_with_surrogates(self):
        """JSON-like content (typical for card messages) with surrogates is sanitized."""
        # Simulate what happens when AI output with surrogates gets embedded in card JSON
        bad = '{"text": "result: \ud83d value"}'
        result = _sanitize_content(bad)
        assert "\ud83d" not in result
        assert "result:" in result
        assert "value" in result


class _Response:
    def __init__(self, *, data=None, ok=True):
        self.data = data
        self._ok = ok
        self.code = 0 if ok else 1
        self.msg = "ok" if ok else "failed"

    def success(self):
        return self._ok


def test_upload_file_returns_file_key(tmp_path):
    """FeishuIMClient should upload a local file and expose file_key."""
    report = tmp_path / "report.html"
    report.write_text("<html>report</html>", encoding="utf-8")
    file_api = MagicMock()
    file_api.create.return_value = _Response(data=MagicMock(file_key="file_key_123"))
    client_obj = MagicMock()
    client_obj.im.v1.file = file_api

    client = FeishuIMClient(lambda: client_obj, MagicMock(im_api_max_retries=1))

    file_key = client.upload_file(str(report), file_type="stream")

    assert file_key == "file_key_123"
    file_api.create.assert_called_once()


def test_reply_file_sends_file_message_with_file_key():
    """FeishuIMClient.reply_file should reply with msg_type=file and file_key content."""
    message_api = MagicMock()
    message_api.reply.return_value = _Response(data=MagicMock(message_id="reply_1"))
    client_obj = MagicMock()
    client_obj.im.v1.message = message_api
    client = FeishuIMClient(lambda: client_obj, MagicMock(im_api_max_retries=1))

    response = client.reply_file("msg_1", "file_key_123", reply_in_thread=True)

    assert response is not None
    request = message_api.reply.call_args.args[0]
    body = request.request_body
    assert body.msg_type == "file"
    assert json.loads(body.content) == {"file_key": "file_key_123"}


def test_all_main_bot_message_mutations_are_audited_before_dispatch():
    message_api = MagicMock()
    message_api.create.return_value = _Response(data=MagicMock(message_id="created"))
    message_api.reply.return_value = _Response(data=MagicMock(message_id="replied"))
    message_api.patch.return_value = _Response(data=MagicMock(message_id="patched"))
    client_obj = MagicMock()
    client_obj.im.v1.message = message_api
    events: list[tuple[str, str, str]] = []
    client = FeishuIMClient(
        lambda: client_obj,
        MagicMock(im_api_max_retries=1),
        outbound_audit=lambda tenant, operation, target: events.append(
            (tenant, operation, target)
        ),
        tenant_key_resolver=lambda: "tenant-a",
    )

    client.send_message("chat_id", "oc_chat", '{"text":"x"}')
    client.reply_message(
        "om_message",
        '{"text":"x"}',
        audit_aliases=("oc_requester_dm", "ou_requester"),
    )
    client.reply_file(
        "om_message",
        "file_key",
        audit_aliases=("oc_requester_dm", "ou_requester"),
    )
    client.patch_message(
        "om_card",
        "{}",
        audit_aliases=("oc_requester_dm", "ou_requester"),
    )

    assert events == [
        ("tenant-a", "create", "oc_chat"),
        ("tenant-a", "reply", "om_message"),
        ("tenant-a", "reply", "oc_requester_dm"),
        ("tenant-a", "reply", "ou_requester"),
        ("tenant-a", "reply", "om_message"),
        ("tenant-a", "reply", "oc_requester_dm"),
        ("tenant-a", "reply", "ou_requester"),
        ("tenant-a", "patch", "om_card"),
        ("tenant-a", "patch", "oc_requester_dm"),
        ("tenant-a", "patch", "ou_requester"),
    ]


def test_reply_audits_literal_and_aliases_before_creating_network_client():
    events: list[tuple[str, str, str]] = []

    def client_factory():
        assert events == [
            ("tenant-a", "reply", "om_sibling"),
            ("tenant-a", "reply", "oc_requester_dm"),
            ("tenant-a", "reply", "ou_requester"),
        ]
        message_api = MagicMock()
        message_api.reply.return_value = _Response(
            data=MagicMock(message_id="replied")
        )
        client_obj = MagicMock()
        client_obj.im.v1.message = message_api
        return client_obj

    client = FeishuIMClient(
        client_factory,
        MagicMock(im_api_max_retries=1),
        outbound_audit=lambda tenant, operation, target: events.append(
            (tenant, operation, target)
        ),
        tenant_key_resolver=lambda: "tenant-a",
    )

    response = client.reply_message(
        "om_sibling",
        '{"text":"x"}',
        audit_aliases=("oc_requester_dm", "ou_requester"),
    )

    assert response is not None


def test_audited_reply_without_recipient_scope_fails_before_network():
    api_client_factory = MagicMock()
    audit = MagicMock()
    client = FeishuIMClient(
        api_client_factory,
        MagicMock(im_api_max_retries=1),
        outbound_audit=audit,
    )

    with pytest.raises(RuntimeError, match="recipient scope"):
        client.reply_message("om_unscoped", '{"text":"x"}')

    audit.assert_not_called()
    api_client_factory.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda client: client.reply_file("om_unscoped", "file_key"),
        lambda client: client.patch_message("om_unscoped", "{}"),
    ),
)
def test_audited_file_reply_and_patch_without_recipient_scope_fail_before_network(
    mutation,
):
    api_client_factory = MagicMock()
    audit = MagicMock()
    client = FeishuIMClient(
        api_client_factory,
        MagicMock(im_api_max_retries=1),
        outbound_audit=audit,
    )

    with pytest.raises(RuntimeError, match="recipient scope"):
        mutation(client)

    audit.assert_not_called()
    api_client_factory.assert_not_called()


def test_reply_message_sets_optional_idempotency_uuid_on_request_body():
    message_api = MagicMock()
    message_api.reply.return_value = _Response(data=MagicMock(message_id="replied"))
    client_obj = MagicMock()
    client_obj.im.v1.message = message_api
    client = FeishuIMClient(lambda: client_obj, MagicMock(im_api_max_retries=1))

    client.reply_message(
        "om_message",
        '{"text":"x"}',
        idempotency_key="stable-reply-uuid",
    )

    request = message_api.reply.call_args.args[0]
    assert request.request_body.uuid == "stable-reply-uuid"


def test_reply_message_rejects_uuid_over_feishu_limit_before_network():
    api_client_factory = MagicMock()
    client = FeishuIMClient(
        api_client_factory,
        MagicMock(im_api_max_retries=1),
    )

    with pytest.raises(ValueError, match="message UUID"):
        client.reply_message(
            "om_message",
            '{"text":"x"}',
            idempotency_key="x" * 51,
        )

    api_client_factory.assert_not_called()


def test_audit_failure_blocks_main_bot_network_dispatch_and_is_reported():
    message_api = MagicMock()
    message_api.reply.return_value = _Response(data=MagicMock(message_id="replied"))
    client_obj = MagicMock()
    client_obj.im.v1.message = message_api
    failures: list[Exception] = []
    client = FeishuIMClient(
        lambda: client_obj,
        MagicMock(im_api_max_retries=1),
        outbound_audit=lambda *_args: (_ for _ in ()).throw(OSError("audit disk")),
        outbound_audit_failure=failures.append,
    )

    with pytest.raises(OSError, match="audit disk"):
        client.reply_message(
            "om_message",
            '{"text":"x"}',
            audit_aliases=("oc_requester_dm",),
        )

    message_api.reply.assert_not_called()
    assert len(failures) == 1
    assert all(isinstance(failure, OSError) for failure in failures)
