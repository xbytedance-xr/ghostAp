from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.autonomous.context import (
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeMessageScope,
    EmployeeThreadContext,
)
from src.autonomous.context.lark_source import LarkEmployeeMessageSourceFactory
from src.autonomous.domain.employees import BotPrincipal


def _scope(**overrides: str) -> EmployeeMessageScope:
    values = {
        "tenant_key": "tenant_1",
        "agent_id": "agt_1",
        "bot_principal_id": "bot_1",
        "app_id": "cli_1",
        "chat_id": "oc_1",
        "thread_root_message_id": "om_root",
        "current_message_id": "om_current",
    }
    values.update(overrides)
    return EmployeeMessageScope(**values)


def _principal(**overrides: object) -> BotPrincipal:
    values: dict[str, object] = {
        "bot_principal_id": "bot_1",
        "tenant_key": "tenant_1",
        "agent_id": "agt_1",
        "app_id": "cli_1",
        "credential_ref": "cred_1",
    }
    values.update(overrides)
    return BotPrincipal(**values)


def _message(
    message_id: str = "om_current",
    *,
    root_id: str = "om_root",
    thread_id: str = "omt_1",
    position: int = 1,
    message_position: int = 11,
    content: object = None,
    msg_type: str = "text",
    updated: bool = False,
    deleted: bool = False,
    create_time: object = "1700000000000",
    update_time: object = "1700000000000",
) -> SimpleNamespace:
    if content is None:
        content = {"text": message_id}
    return SimpleNamespace(
        message_id=message_id,
        root_id=root_id,
        parent_id="" if message_id == "om_root" else root_id,
        thread_id=thread_id,
        msg_type=msg_type,
        create_time=create_time,
        update_time=update_time,
        deleted=deleted,
        updated=updated,
        chat_id="oc_1",
        sender=SimpleNamespace(
            id="ou_1",
            id_type="open_id",
            sender_type="user",
            tenant_key="tenant_1",
        ),
        body=SimpleNamespace(content=json.dumps(content)),
        message_position=message_position,
        thread_message_position=position,
    )


class _Response:
    def __init__(self, *, items=(), code: int = 0, has_more: bool = False, page_token: str = ""):
        self.code = code
        self.msg = "unsafe upstream detail"
        self.data = SimpleNamespace(
            items=list(items),
            has_more=has_more,
            page_token=page_token,
        )

    def success(self) -> bool:
        return self.code == 0


class _MessageAPI:
    def __init__(self, *, get_responses, list_responses=()):
        self.get_responses = list(get_responses)
        self.list_responses = list(list_responses)
        self.get_requests = []
        self.list_requests = []

    def get(self, request):
        self.get_requests.append(request)
        return self.get_responses.pop(0)

    def list(self, request):
        self.list_requests.append(request)
        return self.list_responses.pop(0)


class _Client:
    def __init__(self, api: _MessageAPI):
        self.im = SimpleNamespace(v1=SimpleNamespace(message=api))


class _Vault:
    def __init__(self, secret: str = "employee-secret") -> None:
        self.secret = secret
        self.calls = []

    def resolve(self, credential_ref: str, agent_id: str, app_id: str) -> str:
        self.calls.append((credential_ref, agent_id, app_id))
        return self.secret


def _open_source(*, scope=None, principal=None, get_responses=None, list_responses=()):
    api = _MessageAPI(
        get_responses=get_responses or [_Response(items=[_message()])],
        list_responses=list_responses,
    )
    vault = _Vault()
    builds = []

    def build_client(*, app_id: str, app_secret: str, timeout: float):
        builds.append((app_id, app_secret, timeout))
        return _Client(api)

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=build_client,
        request_timeout_seconds=7.5,
    )
    return factory.open(scope=scope or _scope(), principal=principal or _principal()), api, vault, builds


def test_get_resolves_reply_thread_and_requires_exact_binding() -> None:
    source, api, _, _ = _open_source()
    with source:
        resolved = source.resolve_thread()

    assert resolved.thread_root_message_id == "om_root"
    assert resolved.feishu_thread_id == "omt_1"
    request = api.get_requests[0]
    assert request.paths == {"message_id": "om_current"}
    assert request.queries == [
        ("user_id_type", "open_id"),
        ("card_msg_content_type", "user_card_content"),
    ]


def test_get_accepts_root_message_with_empty_root_id() -> None:
    root = _message("om_root", root_id="", position=0, message_position=10)
    source, _, _, _ = _open_source(
        scope=_scope(current_message_id="om_root"),
        get_responses=[_Response(items=[root])],
    )
    with source:
        assert source.resolve_thread().feishu_thread_id == "omt_1"


def test_plain_group_root_is_exposed_as_stable_singleton_thread() -> None:
    root = _message(
        "om_root",
        root_id=None,  # type: ignore[arg-type]
        thread_id=None,  # type: ignore[arg-type]
        position=0,
        message_position=10,
    )
    source, api, _, _ = _open_source(
        scope=_scope(current_message_id="om_root"),
        get_responses=[
            _Response(items=[root]),
            _Response(items=[root]),
            _Response(items=[root]),
        ],
    )

    with source:
        resolved = source.resolve_thread()
        first = source.list_thread_messages()
        second = source.list_thread_messages()

    assert resolved.feishu_thread_id == ""
    assert first == second
    assert [message.message_id for message in first.messages] == ["om_root"]
    assert first.messages[0].thread_id == ""
    assert len(api.get_requests) == 3


def test_group_history_capability_probe_uses_employee_client() -> None:
    api = _MessageAPI(
        get_responses=[],
        list_responses=[_Response(items=[])],
    )
    vault = _Vault()
    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=lambda **_kwargs: _Client(api),
    )

    assert factory.probe_group_history(_principal(), "oc_1") is True
    assert api.list_requests[0].queries == [
        ("container_id_type", "chat"),
        ("container_id", "oc_1"),
        ("sort_type", "ByCreateTimeDesc"),
        ("page_size", "1"),
        ("card_msg_content_type", "user_card_content"),
    ]
    factory.close()


def test_plain_group_root_assembles_with_recent_group_window() -> None:
    root = _message(
        "om_root",
        root_id=None,  # type: ignore[arg-type]
        thread_id=None,  # type: ignore[arg-type]
        position=0,
        message_position=10,
    )
    source, _, _, _ = _open_source(
        scope=_scope(current_message_id="om_root"),
        get_responses=[
            _Response(items=[root]),
            _Response(items=[root]),
            _Response(items=[root]),
        ],
        list_responses=[
            _Response(items=[root]),
            _Response(items=[root]),
        ],
    )

    with source:
        snapshot = EmployeeThreadContext(message_source=source).assemble()

    assert [message.message_id for message in snapshot.thread_messages] == [
        "om_root"
    ]
    assert snapshot.thread_messages[0].is_current is True
    assert snapshot.watermark is not None
    assert snapshot.watermark.feishu_thread_id == ""


@pytest.mark.parametrize(
    "items",
    [
        [],
        [_message(), _message("om_other")],
        [_message(root_id="om_wrong")],
        [_message(thread_id="")],
    ],
)
def test_get_fails_closed_unless_exactly_one_message_matches_scope(items) -> None:
    source, _, _, _ = _open_source(get_responses=[_Response(items=items)])
    with source, pytest.raises(ContextUnavailableError) as raised:
        source.resolve_thread()
    assert raised.value.reason is ContextUnavailableReason.ROOT_THREAD_BINDING


def test_thread_and_chat_list_build_exact_official_requests() -> None:
    thread_page = _Response(items=[_message()], has_more=True, page_token="next")
    group_page = _Response(
        items=[_message("om_group", root_id="", thread_id="", position=20, message_position=20)]
    )
    source, api, _, _ = _open_source(list_responses=[thread_page, group_page])
    with source:
        source.resolve_thread()
        thread = source.list_thread_messages(page_size=50)
        group = source.list_chat_messages(page_size=20)

    assert thread.page_token == "next" and thread.has_more is True
    assert group.has_more is False
    assert api.list_requests[0].queries == [
        ("container_id_type", "thread"),
        ("container_id", "omt_1"),
        ("sort_type", "ByCreateTimeAsc"),
        ("page_size", "50"),
        ("card_msg_content_type", "user_card_content"),
    ]
    assert api.list_requests[1].queries == [
        ("container_id_type", "chat"),
        ("container_id", "oc_1"),
        ("sort_type", "ByCreateTimeDesc"),
        ("page_size", "20"),
        ("card_msg_content_type", "user_card_content"),
    ]


def test_chat_list_accepts_non_thread_reply_tree() -> None:
    reply = _message(
        "om_reply",
        root_id="om_group_root",
        thread_id="",
        position=20,
        message_position=20,
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[reply])])
    with source:
        page = source.list_chat_messages()
    assert page.messages[0].root_id == "om_group_root"
    assert page.messages[0].thread_id == ""


def test_chat_list_allows_thread_local_positions_to_repeat_across_threads() -> None:
    messages = [
        _message(
            "om_topic_a",
            root_id="",
            thread_id="omt_a",
            position=0,
            message_position=20,
        ),
        _message(
            "om_topic_b",
            root_id="",
            thread_id="omt_b",
            position=0,
            message_position=19,
        ),
    ]
    source, _, _, _ = _open_source(list_responses=[_Response(items=messages)])
    with source:
        page = source.list_chat_messages()
    assert [message.message_id for message in page.messages] == [
        "om_topic_a",
        "om_topic_b",
    ]


@pytest.mark.parametrize("page_token", ["", "same"])
def test_has_more_requires_a_nonempty_advancing_page_token(page_token: str) -> None:
    source, _, _, _ = _open_source(
        list_responses=[_Response(items=[_message()], has_more=True, page_token=page_token)]
    )
    with source:
        source.resolve_thread()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages(page_token="same" if page_token == "same" else "")
    assert raised.value.reason is ContextUnavailableReason.PAGINATION


def test_pagination_rejects_a_multi_page_token_cycle() -> None:
    responses = [
        _Response(
            items=[_message("om_a", position=1, message_position=11)],
            has_more=True,
            page_token="a",
        ),
        _Response(
            items=[_message("om_b", position=2, message_position=12)],
            has_more=True,
            page_token="b",
        ),
        _Response(
            items=[_message("om_c", position=3, message_position=13)],
            has_more=True,
            page_token="a",
        ),
    ]
    source, _, _, _ = _open_source(list_responses=responses)
    with source:
        source.resolve_thread()
        source.list_thread_messages()
        source.list_thread_messages(page_token="a")
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages(page_token="b")
    assert raised.value.reason is ContextUnavailableReason.PAGINATION


def test_pagination_cannot_restart_while_continuation_is_required() -> None:
    source, _, _, _ = _open_source(
        list_responses=[
            _Response(items=[_message()], has_more=True, page_token="next")
        ]
    )
    with source:
        source.resolve_thread()
        source.list_thread_messages()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages(page_token="")
    assert raised.value.reason is ContextUnavailableReason.PAGINATION


def test_chat_traversal_can_be_explicitly_reset_after_bounded_window() -> None:
    first = _Response(
        items=[
            _message(
                "om_first",
                root_id="",
                thread_id="",
                position=0,
                message_position=20,
            )
        ],
        has_more=True,
        page_token="next",
    )
    restarted = _Response(
        items=[
            _message(
                "om_restarted",
                root_id="",
                thread_id="",
                position=0,
                message_position=20,
            )
        ]
    )
    source, api, _, _ = _open_source(list_responses=[first, restarted])

    with source:
        assert source.list_chat_messages().has_more is True
        source.reset_chat_traversal()
        page = source.list_chat_messages()

    assert page.messages[0].message_id == "om_restarted"
    assert ("page_token", "next") not in api.list_requests[1].queries


def test_normalizes_edits_and_tombstones_without_stale_content() -> None:
    edited = _message(
        "om_edited",
        position=1,
        updated=True,
        content={"text": "new text"},
        update_time="1700000001000",
    )
    deleted = _message(
        "om_deleted",
        position=2,
        message_position=12,
        deleted=True,
        content={"text": "stale secret"},
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[edited, deleted])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()

    assert page.messages[0].edited is True
    assert page.messages[0].text == "new text"
    assert page.messages[0].create_time_ms == 1_700_000_000_000
    assert page.messages[1].deleted is True
    assert page.messages[1].text == ""
    assert "stale secret" not in repr(page.messages[1])


def test_valid_media_uses_a_stable_placeholder_without_resource_key() -> None:
    image = _message(
        "om_image",
        msg_type="image",
        content={"image_key": "img_sensitive_key"},
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[image])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()
    assert page.messages[0].text == "[image]"
    assert "img_sensitive_key" not in repr(page.messages[0])


def test_system_message_is_marked_as_untrusted_system_history() -> None:
    system = _message(
        "om_system",
        msg_type="system",
        content={"template": "member joined"},
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[system])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()
    assert page.messages[0].is_system is True


def test_post_preserves_text_but_removes_nested_resource_keys() -> None:
    post = _message(
        "om_post",
        msg_type="post",
        content={
            "zh_cn": {
                "title": "Status",
                "content": [[
                    {"tag": "text", "text": "hello"},
                    {"tag": "img", "image_key": "img_sensitive_key"},
                ]],
            }
        },
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[post])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()
    assert "Status" in page.messages[0].text
    assert "hello" in page.messages[0].text
    assert "[image]" in page.messages[0].text
    assert "img_sensitive_key" not in page.messages[0].text


def test_official_query_post_shape_is_normalized_without_resource_keys() -> None:
    post = _message(
        "om_post_query",
        msg_type="post",
        content={
            "title": "Status",
            "content": [[
                {"tag": "text", "text": "hello"},
                {"tag": "media", "file_key": "file_sensitive_key"},
            ]],
        },
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[post])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()
    assert page.messages[0].text == "Status\n\nhello[media]"
    assert "file_sensitive_key" not in page.messages[0].text


def test_query_post_supports_sdk_markdown_tag() -> None:
    post = _message(
        "om_post_md",
        msg_type="post",
        content={
            "title": "Status",
            "content": [[{"tag": "md", "text": "**hello**"}]],
        },
    )
    source, _, _, _ = _open_source(list_responses=[_Response(items=[post])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()
    assert page.messages[0].text == "Status\n\n**hello**"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Card title"}
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "plain_text", "content": "Card body"},
                    }
                ],
            },
            ("Card title", "Card body"),
        ),
        (
            {
                "schema": "2.0",
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": "Card v2 body"}
                    ]
                },
            },
            ("Card v2 body",),
        ),
    ],
)
def test_raw_interactive_card_versions_preserve_text(content, expected) -> None:
    card = _message("om_card", msg_type="interactive", content=content)
    source, _, _, _ = _open_source(list_responses=[_Response(items=[card])])
    with source:
        source.resolve_thread()
        page = source.list_thread_messages()
    for text in expected:
        assert text in page.messages[0].text


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (_message(create_time="not-int"), ContextUnavailableReason.REVISION),
        (
            _message(update_time="1699999999999"),
            ContextUnavailableReason.REVISION,
        ),
        (
            _message(content="not-an-object", msg_type="text"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={"x": 1}, msg_type="new-unknown-type"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={"x": 1}, msg_type="text"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={}, msg_type="image"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={}, msg_type="video"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(
                content={
                    "zh_cn": {
                        "title": "bad",
                        "content": [[{"tag": "img"}]],
                    }
                },
                msg_type="post",
            ),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={}, msg_type="interactive"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={}, msg_type="share_chat"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={"garbage": 1}, msg_type="interactive"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={"summary": 123}, msg_type="calendar"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(content={}, msg_type="merge_forward"),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(
                content={
                    "title": "bad",
                    "content": [[
                        {"tag": "text", "text": "hello"},
                        {"tag": "audio"},
                    ]],
                },
                msg_type="post",
            ),
            ContextUnavailableReason.CONTENT,
        ),
        (
            _message(
                content={
                    "title": "bad",
                    "content": [[
                        {"tag": "text", "text": "hello"},
                        {"tag": "file"},
                    ]],
                },
                msg_type="post",
            ),
            ContextUnavailableReason.CONTENT,
        ),
    ],
)
def test_invalid_revision_or_content_fails_closed(message, reason) -> None:
    source, _, _, _ = _open_source(list_responses=[_Response(items=[message])])
    with source:
        source.resolve_thread()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages()
    assert raised.value.reason is reason


def test_thread_positions_must_be_unique_and_ascending() -> None:
    messages = [
        _message("om_first", position=2),
        _message("om_second", position=1, message_position=12),
    ]
    source, _, _, _ = _open_source(list_responses=[_Response(items=messages)])
    with source:
        source.resolve_thread()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages()
    assert raised.value.reason is ContextUnavailableReason.ORDERING


def test_present_thread_positions_must_not_repeat() -> None:
    messages = [
        _message("om_first", position=1, message_position=11),
        _message("om_second", position=1, message_position=12),
    ]
    source, _, _, _ = _open_source(list_responses=[_Response(items=messages)])
    with source:
        source.resolve_thread()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages()
    assert raised.value.reason is ContextUnavailableReason.ORDERING


def test_list_rejects_messages_from_another_chat() -> None:
    message = _message()
    message.chat_id = "oc_other"
    source, _, _, _ = _open_source(list_responses=[_Response(items=[message])])
    with source:
        source.resolve_thread()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages()
    assert raised.value.reason is ContextUnavailableReason.SCOPE


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (230027, ContextUnavailableReason.PERMISSION),
        (230050, ContextUnavailableReason.VISIBILITY),
        (230073, ContextUnavailableReason.VISIBILITY),
        (230002, ContextUnavailableReason.VISIBILITY),
        (230006, ContextUnavailableReason.PERMISSION),
        (230110, ContextUnavailableReason.CURRENT_MESSAGE),
    ],
)
def test_platform_errors_map_to_stable_reasons(code: int, reason) -> None:
    source, _, _, _ = _open_source(get_responses=[_Response(code=code)])
    with source, pytest.raises(ContextUnavailableError) as raised:
        source.resolve_thread()
    assert raised.value.reason is reason
    assert str(raised.value) == f"CONTEXT_UNAVAILABLE:{reason.value}"
    assert "unsafe upstream detail" not in repr(raised.value)


def test_transport_timeout_maps_to_deadline_without_upstream_text() -> None:
    source, api, _, _ = _open_source()

    def timeout(_request):
        raise TimeoutError("upstream timeout detail")

    api.get = timeout
    with source, pytest.raises(ContextUnavailableError) as raised:
        source.resolve_thread()
    assert raised.value.reason is ContextUnavailableReason.DEADLINE
    assert str(raised.value) == "CONTEXT_UNAVAILABLE:deadline"


def test_source_rejects_calls_after_bounded_owner_closes() -> None:
    source, _, _, _ = _open_source()
    with source:
        source.resolve_thread()
    assert source.closed is True
    with pytest.raises(ContextUnavailableError):
        source.resolve_thread()
