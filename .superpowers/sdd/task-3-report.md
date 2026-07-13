# Task 3 Implementation Report

## Status

DONE_PENDING_REVIEW

## Outcome

The production employee Channel path now uses the official low-level
`lark_channel.ws.Client` and synchronous `EventDispatcherHandler` callbacks.
Message and real P2 `card.action.trigger` responses are held behind a strict
worker-to-parent ingress frame and a parent ACK that refers to one anchored
`IngressAcceptance`. The high-level `FeishuChannel` helper remains unreachable
from the production worker entry point.

The bridge is fail-closed across callback timeout, parent close, child crash,
anchor/projection failure, reconnect, oversized payload, and control-pipe
concurrency. Reconnect rotates a GhostAP connection ID, cancels old mailbox
ownership, ignores late ACKs from the old connection, and does not admit a new
callback until an observed WSS connection has emitted READY.

The final review closes four additional boundaries: child-to-parent IPC emit is
inside the same 1.5-second callback deadline; a single nonblocking emitter owns
all child event-pipe writes; ACK acceptance is bound to envelope, dedup key, and
semantic digest; and any READY/STARTING event-pipe EOF revokes readiness and
boundedly reaps the worker.

## Changed Files

- `src/autonomous/provisioning/channel_protocol.py`
- `src/autonomous/provisioning/channel_worker.py`
- `src/autonomous/supervisor/employee_channels.py`
- `src/autonomous/ingress/service.py`
- `src/autonomous/ingress/implementation_evidence.py`
- `src/autonomous/ingress/phase3_implementation_evidence_manifest.json`
- `tests/autonomous/contract/test_employee_channel_contract.py`
- `tests/autonomous/contract/test_employee_ingress_contract.py`
- `tests/autonomous/unit/test_employee_channel_ack_mailbox.py`
- `tests/autonomous/unit/test_employee_channel_emitter.py`
- `tests/autonomous/integration/test_employee_channel_process.py`
- `tests/autonomous/integration/test_employee_channel_bridge.py`

## RED Evidence

- Strict ingress protocol tests initially failed with
  `AttributeError: INGRESS` because the frame types and typed payload validators
  did not exist.
- The production supervisor ingress fixture initially failed because
  `EmployeeChannelSupervisor` had no durable ingress service/binding contract.
- The first real low-level bridge selectors failed because the production
  callback did not emit/wait for strict ingress ACK frames and readiness was not
  tied to an observed SDK connection.
- The anchor-failure matrix test exposed a correctness bug: a
  durable-but-unanchored Journal tail was replayed into the ingress projection.
  `EmployeeIngressService` now rebuilds only through the anchor high-water mark.
- Reconnect review exposed two ownership races: a connection ID survived
  reconnect, and READY could be emitted before a stale epoch was rejected.
  Connection admission is now condition-locked: stale pending waits are
  cancelled, each reconnect gets a new connection ID, and READY emission plus
  admission publication is atomic and ordered before INGRESS.
- Final review reproduced a real full-pipe callback hang: the old blocking
  `os.write` produced no wire response after two seconds. It also reproduced a
  READY supervisor retaining an alive child after event-pipe EOF. Both failures
  now have process/real-wire regression selectors.

## GREEN Evidence

Final scoped results:

```text
fix-scoped contract + unit + process + bridge: 130 passed, 1 SDK warning
ingress recovery/ACK chaos: 20 passed
full production bridge fault suite: 17 passed, 1 SDK warning
full tests/autonomous regression: 1406 passed, 2 skipped, 1 SDK warning
scoped ruff: all checks passed
src/autonomous ruff: all checks passed
docs references: 4 passed
git diff --check: passed
```

The warning is the pinned SDK's protobuf use of deprecated
`datetime.utcfromtimestamp`; it is outside GhostAP bridge code and does not
change the wire result.

## Mandatory Bridge Fault Matrix

All real-wire rows run the production `run_low_level_employee_channel` in a
fresh isolated interpreter against the pinned SDK's real HTTP discovery,
WSS/TLS, protobuf dispatch, and SDK response writer. The common bridge context
asserts READY precedes every INGRESS and a thread profiler observes no call into
`src.autonomous.provisioning.router` or `src.acp`. The contract selector
`test_parent_durable_ingress_call_graph_excludes_router_and_acp_execution`
independently locks that pre-dispatch boundary. Task 3 deliberately creates no
Router attempt or ACP call; therefore their expected count is zero here.

| Fault point | Executable selector(s) | Assertions |
| --- | --- | --- |
| Partial IPC frame; child crash | `test_partial_ingress_ipc_frame_crashes_without_acceptance_or_ack`, `test_child_crash_before_parent_commit_has_no_success_and_replay_converges` | No wire success; partial frame never reaches `accept`; zero-or-one anchored race is explicit; replay converges to one acceptance. |
| Blob published before anchor failure | `test_blob_publish_then_anchor_failure_is_wire_500_and_fail_closed_on_replay` | Wire 500 twice; anchor stays at genesis; projection stays empty; both published ciphertext blobs are quarantined; no Router/ACP call. |
| Anchor succeeds; projection/ACK path fails | `test_anchor_success_then_projection_apply_failure_replays_duplicate` | First wire 500 with anchor sequence 1 and empty in-memory projection; replay rebuilds the anchored record and returns duplicate 200 for the same acceptance. |
| Callback times out before late parent commit | `test_callback_timeout_precedes_late_parent_commit_and_replay_is_duplicate` | First response is 500 within the shared 1.5-second total budget; late ACK is ignored; replay is duplicate 200 with one Journal record. |
| ACK received before callback return; child crash | `test_child_crash_after_mailbox_ack_before_callback_return_replays_duplicate` | A test-only barrier wraps the real mailbox wait after ACK receipt; child is killed before callback return; no wire success; replay is duplicate with one acceptance. |
| Callback/SDK response loses its connection | `test_disconnect_before_ack_prevents_wire_success_and_replay_is_duplicate` | Old WSS closes before ACK; old connection has no success; reconnect rotates request and connection IDs; the same canonical envelope/dedup is redelivered on the new real connection and gets duplicate 200. |
| Wire 200 then worker restart | `test_wire_200_then_worker_restart_returns_duplicate_ack_once` | Both wire responses are 200; second worker returns duplicate ACK for the same acceptance; Journal contains one frame. |
| Pending callback plus STOP/reconnect/generation rotation | `test_stop_during_pending_callback_then_generation_rotation_is_fenced`, reconnect selector above, `test_stale_observer_emits_no_ready_and_current_ready_precedes_ingress` | STOP unblocks with 500; generation 4 owns its ACK; old reconnect pending is cancelled; stale observer emits zero READY; new READY precedes new INGRESS. |
| Concurrent ACK/STOP/SEND | `test_parent_control_ack_stop_send_share_one_noninterleaving_writer` | Three concurrent fragmented writes decode as three complete strict frames with unique monotonic sequence numbers and correct types; runtime remains READY until an actual STOP is sent. |
| SDK 500 then platform redelivery | `test_sdk_500_after_lost_ack_redelivers_to_one_duplicate_acceptance` | First response is 500 within total ACK budget after an anchored but lost ACK; redelivery is 200 duplicate; one projection record and one Journal frame. |
| Child event-pipe backpressure | `test_event_pipe_backpressure_fails_wire_within_total_ack_budget`, `test_partial_frame_timeout_closes_emitter_and_rejects_future_frames` | A real full pipe no longer blocks the callback: response is wire 500 within the total budget, zero durable side effects, and a partial NDJSON write closes the emitter so no later frame can interleave. |
| Parent post-anchor ACK encode/write failures | `test_post_anchor_ack_encode_failure_is_wire_500_then_duplicate_replay`, `test_post_anchor_control_pipe_write_failure_is_wire_500_then_duplicate_replay` | Each first delivery is wire 500 after exactly one anchor; replay returns duplicate 200 for the identical acceptance; READY precedes INGRESS and Router/ACP calls remain zero. |
| Callback completes; official SDK response write fails | `test_post_callback_sdk_write_failure_has_no_wire_success_then_duplicate_replay` | Fault injection is after the synchronous callback and before the SDK wire write; first delivery has no wire success but exactly one anchor, and replay returns duplicate 200 for the same acceptance. |
| READY worker loses event-pipe ownership | `test_ready_event_pipe_eof_revokes_readiness_and_reaps_live_worker` | Supervisor changes READY to FAILED with `ready_at=None`, fails pending work, closes control, and boundedly terminates/reaps the still-alive child without joining the reader thread from itself. |

The two frozen implementation-evidence gates are:

- `EI-BRIDGE-MESSAGE-01` ->
  `test_message_wire_response_waits_for_durable_parent_ack`
- `EI-BRIDGE-CARD-01` ->
  `test_card_action_wire_response_waits_for_durable_parent_ack`

Both prove no early response before the parent anchor, code 200 after the
matching ACK, one SDK connection, and exact anchor/acceptance identity.

## Security and Boundary Notes

- Production endpoint discovery requires HTTPS; the loopback HTTP exception is
  an explicit test-only switch while the returned SDK endpoint remains strict
  WSS/TLS.
- Environment proxy discovery is disabled. An explicit proxy must match the
  configured allowlist.
- SDK security is strict with insecure/local WS disabled, 8 fragments,
  256 KiB fragment bytes, one concurrent handler, and overflow drop.
- Worker hardening runs before bootstrap credential read and before SDK import.
- The controlled SDK identity gate runs before `lark_channel` import.
- Card correlation never trusts `action.value`; real P2 event identity is used,
  and missing trusted fallback correlation fails closed.
- Ordinary IPC applies the same collapsed exact secret aliases as ingress
  models, rejecting nested camel/Pascal/hyphen variants without rejecting
  `authorization_type`, token-expiry, or password-policy metadata.
- Outbound SEND is intentionally rejected by this inbound worker; outbound
  transport remains separate, with no second employee WS and no main-Bot
  fallback.
- `autonomous_visible_employee_limit` and release readiness remain unchanged;
  this task is local implementation evidence, not real-tenant release evidence.

## Remaining Boundary

This task anchors and deduplicates ingress only. Router disposition, attempt
creation, ACP execution, Response Outbox, and real-tenant release attestation
remain later Phase 3 tasks. No Task 3 test treats an ingress ACK as permission to
execute those downstream stages.
