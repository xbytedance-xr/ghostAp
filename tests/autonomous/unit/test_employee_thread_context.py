"""Deterministic employee Thread snapshot and budget tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.autonomous.context import (
    ContextLayer,
    ContextMessage,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeMessageScope,
    EmployeeThreadContext,
    MessagePage,
    ResolvedThread,
    ThreadContextConfig,
)


def _scope() -> EmployeeMessageScope:
    return EmployeeMessageScope(
        tenant_key="tenant_1",
        agent_id="agt_1",
        bot_principal_id="bot_1",
        app_id="cli_1",
        chat_id="oc_1",
        thread_root_message_id="om_root",
        current_message_id="om_current",
    )


def _msg(
    message_id: str,
    text: str | None = None,
    *,
    create: int,
    update: int | None = None,
    position: int,
    message_position: int | None = None,
    deleted: bool = False,
    edited: bool = False,
    system: bool = False,
) -> ContextMessage:
    return ContextMessage(
        message_id=message_id,
        sender_id="ou_1",
        sender_type="user",
        text=message_id if text is None else text,
        timestamp=create / 1000,
        is_system=system,
        edited=edited,
        deleted=deleted,
        chat_id="oc_1",
        thread_id="omt_1",
        root_id="" if message_id == "om_root" else "om_root",
        parent_id="" if message_id == "om_root" else "om_root",
        sender_id_type="open_id",
        sender_tenant_key="tenant_1",
        msg_type="system" if system else "text",
        create_time_ms=create,
        update_time_ms=create if update is None else update,
        message_position=position if message_position is None else message_position,
        thread_message_position=position,
    )


def _stable_thread(*extra: ContextMessage) -> list[ContextMessage]:
    return [
        _msg("om_root", "root", create=1_000, position=0),
        *extra,
        _msg("om_current", "current", create=3_000, position=2),
    ]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeSource:
    def __init__(
        self,
        *,
        traversals: list[list[MessagePage]],
        group_pages: list[MessagePage] | None = None,
        clock: _Clock | None = None,
        advance_seconds: float = 0.0,
        group_error: Exception | None = None,
    ) -> None:
        self.scope = _scope()
        self._traversals = traversals
        self._group_pages = group_pages or [MessagePage((), False)]
        self._thread_traversal = -1
        self._thread_page = 0
        self._group_page = 0
        self._clock = clock
        self._advance_seconds = advance_seconds
        self._group_error = group_error
        self.resolve_calls = 0
        self.thread_calls = 0
        self.group_calls = 0
        self.reset_calls = 0

    def _advance(self) -> None:
        if self._clock is not None:
            self._clock.now += self._advance_seconds

    def resolve_thread(self) -> ResolvedThread:
        self.resolve_calls += 1
        self._advance()
        return ResolvedThread("om_root", "omt_1", "om_current")

    def list_thread_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        del page_size
        self.thread_calls += 1
        self._advance()
        if not page_token:
            self._thread_traversal += 1
            self._thread_page = 0
        pages = self._traversals[self._thread_traversal]
        page = pages[self._thread_page]
        self._thread_page += 1
        return page

    def list_chat_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 20,
    ) -> MessagePage:
        del page_size
        self.group_calls += 1
        self._advance()
        if self._group_error is not None:
            raise self._group_error
        if not page_token and self._group_page >= len(self._group_pages):
            self._group_page = 0
        page = self._group_pages[self._group_page]
        self._group_page += 1
        return page

    def reset_chat_traversal(self) -> None:
        self.reset_calls += 1
        self._group_page = 0

    def close(self) -> None:
        pass


def _pages(messages: list[ContextMessage]) -> list[MessagePage]:
    return [MessagePage(tuple(messages), False)]


def _assembler(
    source: _FakeSource,
    *,
    config: ThreadContextConfig | None = None,
    clock: _Clock | None = None,
) -> EmployeeThreadContext:
    return EmployeeThreadContext(
        message_source=source,
        config=config,
        monotonic=clock or _Clock(),
    )


def test_requires_two_equal_traversals_and_excludes_messages_after_current() -> None:
    before = _msg("om_before", "before", create=2_000, position=1)
    future = _msg("om_future", "future", create=4_000, position=3)
    thread = _stable_thread(before) + [future]
    source = _FakeSource(traversals=[_pages(thread), _pages(thread)])

    snapshot = _assembler(source).assemble()

    assert source.resolve_calls == 1
    assert source.thread_calls == 2
    assert [message.message_id for message in snapshot.thread_messages] == [
        "om_root",
        "om_before",
        "om_current",
    ]
    assert snapshot.thread_messages[-1].is_current is True
    assert snapshot.watermark is not None
    assert snapshot.watermark.message_count == 3
    assert snapshot.watermark.last_message_id == "om_current"
    assert len(snapshot.snapshot_hash) == 64


def test_unstable_pair_retries_once_then_accepts_stable_pair() -> None:
    first = _stable_thread(_msg("om_before", "v1", create=2_000, position=1))
    second = _stable_thread(
        _msg("om_before", "v2", create=2_000, update=2_500, position=1, edited=True)
    )
    source = _FakeSource(
        traversals=[_pages(first), _pages(second), _pages(second), _pages(second)]
    )

    snapshot = _assembler(source).assemble()

    assert source.thread_calls == 4
    assert snapshot.thread_messages[1].text == "v2"


def test_page_partition_and_messages_after_current_do_not_change_snapshot() -> None:
    root = _msg("om_root", "root", create=1_000, position=0)
    current = _msg("om_current", "current", create=3_000, position=2)
    future_a = _msg("om_future_a", "future a", create=4_000, position=3)
    future_b = _msg("om_future_b", "future b", create=5_000, position=4)
    source = _FakeSource(
        traversals=[
            [
                MessagePage((root,), True, "next"),
                MessagePage((current, future_a), False),
            ],
            [MessagePage((root, current, future_b), False)],
        ]
    )

    snapshot = _assembler(source).assemble()

    assert source.thread_calls == 3
    assert [message.message_id for message in snapshot.thread_messages] == [
        "om_root",
        "om_current",
    ]


def test_instability_after_bounded_retry_fails_closed() -> None:
    versions = [
        _stable_thread(
            _msg(
                "om_before",
                f"v{version}",
                create=2_000,
                update=2_000 + version,
                position=1,
                edited=True,
            )
        )
        for version in range(4)
    ]
    source = _FakeSource(traversals=[_pages(version) for version in versions])

    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()
    assert raised.value.reason is ContextUnavailableReason.REVISION
    assert source.thread_calls == 4


def test_current_message_propagation_retries_then_requires_exact_live_message() -> None:
    missing = [
        _msg("om_root", "root", create=1_000, position=0),
        _msg("om_before", "before", create=2_000, position=1),
    ]
    present = _stable_thread(_msg("om_before", "before", create=2_000, position=1))
    source = _FakeSource(
        traversals=[_pages(missing), _pages(missing), _pages(present), _pages(present)]
    )
    assert _assembler(source).assemble().thread_messages[-1].is_current

    always_missing = _FakeSource(traversals=[_pages(missing) for _ in range(4)])
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(always_missing).assemble()
    assert raised.value.reason is ContextUnavailableReason.CURRENT_MESSAGE


@pytest.mark.parametrize(
    "current",
    [
        _msg("om_current", "stale", create=3_000, position=2, deleted=True),
        _msg("om_current", "system", create=3_000, position=2, system=True),
    ],
)
def test_deleted_or_system_current_message_is_never_executable(current) -> None:
    thread = [
        _msg("om_root", "root", create=1_000, position=0),
        current,
    ]
    source = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()
    assert raised.value.reason is ContextUnavailableReason.CURRENT_MESSAGE


def test_dedup_uses_newer_revision_and_rejects_equal_revision_conflict() -> None:
    old = _msg("om_before", "old", create=2_000, update=2_000, position=1)
    new = replace(old, text="new", update_time_ms=2_500, edited=True)
    current = _msg("om_current", "current", create=3_000, position=2)
    pages = _pages([_msg("om_root", create=1_000, position=0), old, new, current])
    source = _FakeSource(traversals=[pages, pages])
    assert _assembler(source).assemble().thread_messages[1].text == "new"

    conflict = replace(old, text="conflict")
    bad_pages = _pages(
        [_msg("om_root", create=1_000, position=0), old, conflict, current]
    )
    bad_source = _FakeSource(traversals=[bad_pages, bad_pages])
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(bad_source).assemble()
    assert raised.value.reason is ContextUnavailableReason.REVISION


def test_group_layer_is_bounded_sorted_deduped_and_untrusted_system_is_removed() -> None:
    thread_old = _msg("om_root", "thread old", create=1_000, update=1_000, position=0)
    thread = [thread_old, _msg("om_current", create=3_000, position=2)]
    group_newer = replace(
        thread_old,
        text="group newer",
        update_time_ms=2_000,
        edited=True,
    )
    group_messages = [
        _msg("om_future_group", create=4_000, position=4),
        _msg("om_group_new", create=2_500, position=3),
        _msg("om_system", create=2_000, position=2, system=True),
        group_newer,
    ]
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[MessagePage(tuple(group_messages), False)],
    )

    snapshot = _assembler(source).assemble()

    assert snapshot.thread_messages[0].text == "group newer"
    assert snapshot.watermark is not None
    assert snapshot.watermark.revision == 3_000
    assert snapshot.watermark.revision_digest == EmployeeThreadContext._revision_digest(
        list(snapshot.thread_messages)
    )
    assert [message.message_id for message in snapshot.group_messages] == [
        "om_group_new"
    ]
    assert all(not message.is_system for message in snapshot.thread_messages)
    assert all(not message.is_system for message in snapshot.group_messages)


def test_group_revision_cannot_rebind_stable_thread_identity() -> None:
    root = _msg("om_root", "thread old", create=1_000, position=0)
    thread = [root, _msg("om_current", create=3_000, position=2)]
    foreign = replace(
        root,
        text="foreign",
        update_time_ms=2_000,
        edited=True,
        thread_id="omt_foreign",
        root_id="om_foreign",
    )
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[MessagePage((foreign,), False)],
    )

    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()

    assert raised.value.reason is ContextUnavailableReason.REVISION


def test_group_failure_is_not_silently_reported_as_empty() -> None:
    thread = _stable_thread()
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_error=RuntimeError("unsafe upstream detail"),
    )
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()
    assert raised.value.reason is ContextUnavailableReason.SOURCE
    assert "unsafe upstream detail" not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None

    typed_source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_error=ContextUnavailableError(ContextUnavailableReason.PERMISSION),
    )
    with pytest.raises(ContextUnavailableError) as typed:
        _assembler(typed_source).assemble()
    assert typed.value.reason is ContextUnavailableReason.PERMISSION
    assert typed.value.__cause__ is None
    assert typed.value.__context__ is None


def test_public_scope_failure_is_typed_and_exception_graph_is_detached() -> None:
    class _BrokenScopeSource:
        @property
        def scope(self):
            raise RuntimeError("app_secret=unsafe")

    with pytest.raises(ContextUnavailableError) as raised:
        EmployeeThreadContext(
            message_source=_BrokenScopeSource(),  # type: ignore[arg-type]
            monotonic=_Clock(),
        ).assemble()

    assert raised.value.reason is ContextUnavailableReason.SOURCE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_group_fetch_continues_after_ineligible_page() -> None:
    thread = _stable_thread()
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[
            MessagePage(
                (
                    _msg("om_future", create=4_000, position=4),
                    _msg("om_system", create=2_500, position=3, system=True),
                ),
                True,
                "next",
            ),
            MessagePage(
                (_msg("om_group", create=2_000, position=1),),
                False,
            ),
        ],
    )

    snapshot = _assembler(source).assemble()

    assert source.group_calls == 4
    assert [message.message_id for message in snapshot.group_messages] == [
        "om_group"
    ]


def test_group_fetch_stops_after_collecting_configured_recent_messages() -> None:
    thread = _stable_thread()
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[
            MessagePage(
                (_msg("om_recent", create=2_900, position=9),),
                True,
                "next",
            ),
            MessagePage(
                (_msg("om_old", create=1_000, position=1),),
                True,
                "still-more",
            ),
        ],
    )
    config = ThreadContextConfig(max_group_messages=1, max_pages=2)

    snapshot = _assembler(source, config=config).assemble()

    assert source.group_calls == 4
    assert source.reset_calls == 2
    assert [message.message_id for message in snapshot.group_messages] == [
        "om_recent"
    ]


def test_group_recent_order_never_uses_thread_local_position() -> None:
    thread = _stable_thread()
    older = replace(
        _msg("om_older", create=1_000, position=1),
        message_position=None,
        thread_message_position=100,
    )
    newer = replace(
        _msg("om_newer", create=2_000, position=2),
        message_position=None,
        thread_message_position=0,
    )
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[MessagePage((newer, older), False)],
    )
    config = ThreadContextConfig(max_group_messages=1)

    snapshot = _assembler(source, config=config).assemble()

    assert [message.message_id for message in snapshot.group_messages] == [
        "om_newer"
    ]


def test_group_recent_drains_cutoff_timestamp_cohort_before_reset() -> None:
    thread = _stable_thread()
    first = replace(
        _msg("om_a", create=2_000, position=1),
        message_position=None,
        thread_message_position=None,
    )
    second = replace(
        _msg("om_z", create=2_000, position=2),
        message_position=None,
        thread_message_position=None,
    )
    older = _msg("om_old", create=1_000, position=0)
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[
            MessagePage((first,), True, "same-ms"),
            MessagePage((second,), True, "older"),
            MessagePage((older,), True, "still-more"),
        ],
    )
    config = ThreadContextConfig(max_group_messages=1, max_pages=3)

    snapshot = _assembler(source, config=config).assemble()

    assert [message.message_id for message in snapshot.group_messages] == [
        "om_z"
    ]
    assert source.group_calls == 6
    assert source.reset_calls == 2


def test_group_window_revision_change_fails_closed() -> None:
    first = _msg("om_group", "v1", create=2_000, position=1)
    second = _msg(
        "om_group",
        "v2",
        create=2_000,
        update=2_500,
        position=1,
        edited=True,
    )

    class _ChangingGroupSource(_FakeSource):
        def list_chat_messages(
            self,
            *,
            page_token: str = "",
            page_size: int = 20,
        ) -> MessagePage:
            del page_token, page_size
            self.group_calls += 1
            message = first if self.reset_calls == 0 else second
            return MessagePage((message,), False)

    thread = _stable_thread()
    source = _ChangingGroupSource(
        traversals=[_pages(thread), _pages(thread)]
    )

    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()

    assert raised.value.reason is ContextUnavailableReason.REVISION
    assert source.group_calls == 2
    assert source.reset_calls == 2


def test_group_continuation_failure_resets_cursor_for_empty_token_restart() -> None:
    first_page = MessagePage(
        (_msg("om_group", create=2_000, position=1),),
        True,
        "next",
    )

    class _FailingContinuationSource(_FakeSource):
        def list_chat_messages(
            self,
            *,
            page_token: str = "",
            page_size: int = 20,
        ) -> MessagePage:
            if page_token:
                self.group_calls += 1
                raise RuntimeError("unsafe continuation detail")
            return super().list_chat_messages(
                page_token=page_token,
                page_size=page_size,
            )

    thread = _stable_thread()
    source = _FailingContinuationSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[first_page],
    )

    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()

    assert raised.value.reason is ContextUnavailableReason.SOURCE
    assert raised.value.__context__ is None
    assert source.reset_calls == 1
    assert source.list_chat_messages(page_token="") == first_page


def test_group_boundary_rejects_equal_chat_position_for_different_message() -> None:
    current = _msg("om_current", create=3_000, position=2)
    thread = [_msg("om_root", create=1_000, position=0), current]
    collision = _msg("om_other", create=3_000, position=2)
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[MessagePage((collision,), False)],
    )

    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source).assemble()

    assert raised.value.reason is ContextUnavailableReason.ORDERING


def test_page_and_message_caps_fail_instead_of_labeling_partial_thread_full() -> None:
    root = _msg("om_root", create=1_000, position=0)
    page = MessagePage((root,), True, "next")
    source = _FakeSource(traversals=[[page], [page]])
    config = ThreadContextConfig(max_pages=1)
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source, config=config).assemble()
    assert raised.value.reason is ContextUnavailableReason.PAGINATION

    thread = _stable_thread(_msg("om_before", create=2_000, position=1))
    too_many = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    config = ThreadContextConfig(max_thread_messages=2)
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(too_many, config=config).assemble()
    assert raised.value.reason is ContextUnavailableReason.BUDGET


def test_deadline_covers_resolve_and_all_page_fetches() -> None:
    clock = _Clock()
    thread = _stable_thread()
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        clock=clock,
        advance_seconds=0.6,
    )
    config = ThreadContextConfig(fetch_timeout_seconds=1.0)
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source, config=config, clock=clock).assemble()
    assert raised.value.reason is ContextUnavailableReason.DEADLINE


def test_budget_trims_whole_units_in_exact_priority_and_recomputes_metrics() -> None:
    thread = _stable_thread(
        _msg("om_old", "tttt", create=2_000, position=1)
    )
    groups = [
        _msg("om_g_new", "gggg", create=2_900, position=4),
        _msg("om_g_old", "gggg", create=2_800, position=3),
    ]
    source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)],
        group_pages=[MessagePage(tuple(groups), False)],
    )
    config = ThreadContextConfig(
        max_context_chars=19,
        max_context_tokens=100,
        tokens_per_char=1,
    )

    snapshot = _assembler(source, config=config).assemble(
        l1_summary="1111",
        l2_summary="2222",
    )

    assert snapshot.l2_summary == ""
    assert snapshot.l1_summary == ""
    assert [message.message_id for message in snapshot.group_messages] == [
        "om_g_new"
    ]
    assert [record.layer for record in snapshot.trimming_trace] == [
        ContextLayer.L2_GROUP,
        ContextLayer.L1_MEMORY,
        ContextLayer.GROUP_RECENT,
    ]
    assert snapshot.truncated is True
    assert snapshot.total_chars <= 19
    assert snapshot.watermark is not None
    assert snapshot.watermark.message_count == 3
    metrics = {metric.layer: metric for metric in snapshot.layer_metrics}
    assert metrics[ContextLayer.GROUP_RECENT].source_messages == 2
    assert metrics[ContextLayer.GROUP_RECENT].retained_messages == 1


@pytest.mark.parametrize(
    ("max_chars", "max_tokens", "reserve"),
    [
        (3, 100, 0),
        (100, 4, 0),
        (100, 10, 7),
    ],
)
def test_protected_current_and_system_reserve_never_silently_truncate(
    max_chars: int,
    max_tokens: int,
    reserve: int,
) -> None:
    current = _msg("om_current", "12345", create=3_000, position=1)
    thread = [_msg("om_root", "", create=1_000, position=0), current]
    source = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    config = ThreadContextConfig(
        max_context_chars=max_chars,
        max_context_tokens=max_tokens,
        tokens_per_char=1,
    )
    with pytest.raises(ContextUnavailableError) as raised:
        _assembler(source, config=config).assemble(
            system_prompt_token_reserve=reserve,
            constraints_digest="a" * 64 if reserve else "",
        )
    assert raised.value.reason is ContextUnavailableReason.BUDGET


def test_snapshot_hash_is_deterministic_and_plaintext_free_diagnostics_remain() -> None:
    thread = _stable_thread(_msg("om_before", "secret body", create=2_000, position=1))
    source_a = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    source_b = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    first = _assembler(source_a).assemble(l1_summary="private memory")
    second = _assembler(source_b).assemble(l1_summary="private memory")

    assert first.snapshot_hash == second.snapshot_hash
    assert first.total_chars > 0
    diagnostics = repr(first.diagnostics())
    assert "secret body" not in diagnostics
    assert "private memory" not in diagnostics


def test_snapshot_hash_binds_the_renderer_token_conversion_contract() -> None:
    """A replay may not change prompt budgeting without changing snapshot identity."""

    thread = _stable_thread()
    source_a = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    source_b = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    first = _assembler(
        source_a,
        config=ThreadContextConfig(tokens_per_char=0.3),
    ).assemble()
    second = _assembler(
        source_b,
        config=ThreadContextConfig(tokens_per_char=0.4),
    ).assemble()

    assert first.tokens_per_char == 0.3
    assert second.tokens_per_char == 0.4
    assert first.snapshot_hash != second.snapshot_hash


def test_trusted_reservation_is_validated_and_changes_snapshot_identity() -> None:
    thread = _stable_thread()
    invalid_source = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    with pytest.raises(ContextUnavailableError) as invalid:
        _assembler(invalid_source).assemble(
            system_prompt_token_reserve=1,
            constraints_digest="not-a-digest",
        )
    assert invalid.value.reason is ContextUnavailableReason.SCOPE
    assert invalid_source.resolve_calls == 0

    missing_digest_source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)]
    )
    with pytest.raises(ContextUnavailableError) as missing_digest:
        _assembler(missing_digest_source).assemble(
            system_prompt_token_reserve=1,
        )
    assert missing_digest.value.reason is ContextUnavailableReason.SCOPE
    assert missing_digest_source.resolve_calls == 0

    invalid_type_source = _FakeSource(
        traversals=[_pages(thread), _pages(thread)]
    )
    with pytest.raises(ContextUnavailableError) as invalid_type:
        _assembler(invalid_type_source).assemble(
            constraints_digest=object(),  # type: ignore[arg-type]
        )
    assert invalid_type.value.reason is ContextUnavailableReason.SCOPE
    assert invalid_type_source.resolve_calls == 0

    source_a = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    source_b = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    baseline = _assembler(source_a).assemble()
    reserved = _assembler(source_b).assemble(
        system_prompt_token_reserve=1,
        constraints_digest="a" * 64,
    )
    assert reserved.total_tokens_estimate == baseline.total_tokens_estimate + 1
    assert reserved.snapshot_hash != baseline.snapshot_hash


def test_snapshot_hash_binds_employee_bot_and_app_authority() -> None:
    thread = _stable_thread()
    source_a = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    source_b = _FakeSource(traversals=[_pages(thread), _pages(thread)])
    source_b.scope = replace(
        source_b.scope,
        bot_principal_id="bot_2",
        app_id="cli_2",
    )

    first = _assembler(source_a).assemble()
    second = _assembler(source_b).assemble()

    assert first.snapshot_hash != second.snapshot_hash
