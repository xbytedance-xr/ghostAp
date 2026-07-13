# Autonomous Employee Durable Ingress and Slock Gateway Plan

> **Status:** active; Tasks 0-4 are complete. Task 5 authority-bound durable
> Router and bounded queues is next. This plan does not
> authorize raising `autonomous_visible_employee_limit` above `0`.

**Goal:** Accept employee Bot events durably before the Feishu WebSocket ACK,
deduplicate them across restart, authorize and queue them from Journal state,
anchor one immutable execution attempt, and invoke the existing Slock
`_run_acp_session` exactly once per accepted attempt in one live process.

**Non-goals:** This phase does not implement the employee Durable Outbox,
team/role mutation commands, `/stop`, `/fire`, canonical data producer cutover,
external release trust, or real-tenant release. Queue-full and terminal response
facts are recorded now; employee-owned delivery is Phase 4.

## Corrected SDK ACK boundary

The high-level `FeishuChannel` callback cannot satisfy this phase. In the
official `lark-channel-sdk==1.1.0`, and in official repository main at
`ae11cab573eec804c185f571ff5627583ea2d485`, the synchronous message dispatcher
only schedules `_handle_message_event()` and returns. The low-level WS client
then writes `Response(code=200)` without waiting for the user handler. Waiting
for a parent-process IPC ACK inside `channel.on("message")` therefore cannot
prove platform ACK-after-fsync.

The candidate production route is the pinned official low-level
`lark_channel.ws.Client` plus `EventDispatcherHandler`. Its registered event
callback runs synchronously, and the WS client writes the platform response
only after the dispatcher returns. The callback must send a bounded IPC ingress
request and return only after the parent confirms an anchored Journal commit;
timeout or failure raises so the official client returns a non-success response.
It is not an approved production route until Task 0 proves message, P2
`card.action.trigger`, response ordering, deadline, reconnect, and shutdown on
the exact wheel used at runtime.

Two gates remain separate:

- **IPC durable ACK:** parent fsync/anchor precedes the child ACK. The durable
  `IngressAcceptance` is canonical across replay; every delivery receives a new
  transport `IngressAck` bound to that request and current connection while
  referencing the original acceptance.
- **Platform ACK capability:** a pinned-SDK black-box contract proves ordering
  `Journal anchor -> callback return -> WS 200 write`. The high-level channel is
  explicitly unsupported. Message and P2 `card.action.trigger` EVENT paths must
  each pass; neither may be inferred from the other. Raw `MessageType.CARD` is a
  separate known-RED SDK path and is never treated as card-action evidence. If
  the low-level ordering contract,
  version pin, stop/reconnect lifecycle, or latency bound fails, employee
  execution readiness is false.

Official references:

- <https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN>
- <https://github.com/larksuite/channel-sdk-python>

## Frozen safety invariants

- The worker binding, not event text or callback payload, supplies employee ID,
  app ID, and channel generation.
- The parent resolves Bot principal and lifecycle from the current Journal
  projection. A raw Channel event cannot construct an authorized request.
- Journal metadata is secret-free. User content and attachment metadata that
  are not safe indexes are stored in an employee-scoped encrypted Blob; only an
  authenticated Blob reference and content hash enter the Journal.
- Dedup prefers `(tenant, employee, event_id)`. Without `event_id`, the key is
  `(tenant, employee, message_id, event_type, action_identity)`. Generation is
  authority evidence, not part of logical dedup, so reconnect replay deduplicates.
- An accepted event is never dispatched before its Inbox frame is anchored.
  Anchor failure yields no ACK and no Router/ACP call.
- Normal employee routing requires ACTIVE visible employee, exact
  employee/principal/app/generation binding, tenant, membership, requester ACL,
  live Channel identity, and a current data projection head.
- Self messages and every managed GhostAP Bot are ignored unless a separate,
  authorized inter-agent correlation contract exists.
- Per employee concurrency is one. Per-employee, per-team, and global queues are
  bounded and Journal-derived. Queue full records a durable busy disposition.
- Context is assembled exactly once. The final authority check and immutable
  attempt/dispatch anchor are atomic; the external ACP call happens after the
  lock is released.
- The gateway calls `SlockEngine.run_agent_session()` once. That public wrapper
  continues to call the unchanged `_run_acp_session`; no backend branch is
  copied and TTADK remains a CLI bridge.
- A dispatch-anchored attempt without a terminal fact after restart becomes
  `action_required` and is never automatically rerun. Explicit retry creates a
  new attempt ID and discloses possible prior side effects.
- A terminal history commit must succeed before a successful employee response
  may be emitted. Late or duplicate terminals cannot overwrite the first
  terminal epoch.

## Task 0: Prove the pinned Channel capability before bridge work

**Hard gate**

- Add `tests/autonomous/contract/test_employee_channel_sdk_capability.py` using
  the installed, locked wheel's real `lark_channel.ws.Client`,
  `EventDispatcherHandler`, protobuf `Frame`, and a local HTTP + WebSocket wire
  harness. Do not replace `_handle_data_frame`, `_write_message`, dispatcher, or
  `schedule` in the positive proof.
- Force the harness and candidate adapter to use SDK strict security config,
  WSS, explicit proxy policy, bounded fragments/concurrency, and overflow drop;
  compatibility-mode or insecure transport can never produce a green artifact.
- Deliver real P2 message and P2 `card.action.trigger` EVENT payloads separately.
  Use barriers/events, never sleeps or timestamp-only ordering: before the
  parent anchor barrier opens, the wire must contain no success response; after
  it opens, parse the returned protobuf frame and require code 200. Exceptions,
  timeout, and parent close must produce a wire-observed non-success response.
- Add the high-level negative proof through the same wire harness: hold the user
  handler incomplete and observe that high-level scheduling still allows an
  early 200. This test documents why that backend is forbidden.
- Prove request reconnect while idle and while a callback is blocked. The pinned
  SDK has no public stop/close; production shutdown therefore closes admission,
  waits a bounded callback grace, then terminates the one-employee worker
  process. An exact-version reviewed private stop adapter may replace process
  termination only if this gate later proves it. No hidden indefinite drain.
- Default durable ACK timeout is 1.5 seconds, leaving at least 1.0 second of the
  documented three-second handler deadline for SDK/wire overhead. Test message
  and card deadlines independently, heartbeat/reconnect progress, blocked-stop
  freedom from deadlock, and exactly one inbound WS per app.
- Emit a capability artifact bound to GhostAP commit, exact
  `lark-channel-sdk` version, installed wheel hash, exact collected node IDs,
  and result summary. Runtime mismatch produces stable blocker
  `employee_channel_sdk_capability_mismatch`.

Task 0 ends with one explicit decision: `CAPABLE_PINNED_ADAPTER` or
`CAPABILITY_RED`. RED may not be bypassed and blocks Tasks 3 and 7 production
composition; pure contracts and durable Inbox work may continue fail-closed.

**Task 0 outcome (2026-07-13):** `CAPABLE_PINNED_ADAPTER` for the exact locked
`lark-channel-sdk==1.1.0` adapter profile. The standalone runner freezes 19
wire selectors and binds their setup/call/teardown results to the GhostAP
commit, `pyproject.toml`, `uv.lock` wheel hash, installed RECORD, runtime source
payload, Python/pytest/requests/websockets versions, and a controlled empty
bytecode cache. Identity mismatch reports stable blocker
`employee_channel_sdk_capability_mismatch`; stale artifacts are removed before
identity validation. Evidence remains `promotable=false` and explicitly records
`requires_parent_payload_gate=true`.

The gate proves low-level message and P2 `card.action.trigger` EVENT callbacks
hold wire 200 until callback return; timeout, parent close, and callback failure
produce wire 500. It also proves bounded idle/blocked reconnect, heartbeat,
termination, strict WSS/direct proxy policy, concurrency, fragment-count and
fragment-byte overflow drop with same-connection recovery, and secret-safe
ERROR logging. The high-level handler still demonstrates early 200 and remains
forbidden. Raw `MessageType.CARD` remains RED. A single oversized frame still
reaches the SDK callback; Tasks 3 and 7 must require a parent payload-gate
selector that proves wire non-200 and zero Inbox/Journal/Router/ACP side effects.

Commit: `test(autonomous): prove employee channel ack capability`

## Task 1: Contracts, configuration, and evidence schema

**Tests first**

- Add `tests/autonomous/contract/test_employee_ingress_contract.py`.
- Add strict round-trip, unknown-field, ID, size, timestamp, generation,
  secret-field, and canonical dedup tests for frozen envelope metadata,
  encrypted payload, ACK, disposition, and attempt state.
- Add configuration tests for ACK timeout, payload/attachment limits,
  per-employee/per-team/global queue limits, and boolean/NaN rejection.
- Add evidence-schema tests that reject missing/non-collected selectors, wrong
  commit or SDK artifact identity, duplicate IDs, and result summaries that do
  not bind the exact pytest node ID.

**Implementation**

- Create `src/autonomous/ingress/models.py` with exact-schema frozen models:
  `EmployeeIngressMetadata`, `EmployeeIngressPayload`, `IngressAcceptance`,
  `EmployeeIngressAck`, `IngressDisposition`, and `EmployeeAttemptState`.
- Keep trusted worker binding fields separate from untrusted normalized parts.
- Add strict settings; default ACK timeout must remain below Feishu's documented
  three-second handler deadline.
- Create a dedicated local Phase 3 implementation-evidence manifest rather than
  misusing the real-tenant Employee release manifest. Bind every result to exact
  selector, commit, artifact/wheel digest, and test summary. FI-29 remains the
  global IPC durability requirement; employee platform capability uses distinct
  local IDs and never counts as real-tenant evidence.
- Freeze these selectors in that manifest:
  - `EI-PLATFORM-MESSAGE-01` →
    `tests/autonomous/contract/test_employee_channel_sdk_capability.py::test_message_wire_response_waits_for_parent_anchor`
  - `EI-PLATFORM-CARD-01` →
    `tests/autonomous/contract/test_employee_channel_sdk_capability.py::test_card_action_wire_response_waits_for_parent_anchor`
  - `EI-IPC-01` →
    `tests/autonomous/chaos/test_employee_ingress_recovery.py::test_ipc_ack_only_after_anchored_acceptance`
  Each is local `chaos_security` evidence; the platform selectors use the local
  pinned-wheel wire harness and IPC uses the local process/fsync harness.
- Update the Agent Department design ACK section to remove the high-level SDK
  assumption.

Commit: `feat(autonomous): freeze durable employee ingress contracts`

**Task 1 outcome (2026-07-13):** complete. Six frozen exact-schema ingress
models now separate trusted worker binding from normalized untrusted content,
reject secret-bearing aliases recursively, and freeze canonical restart-safe
dedup without connection or generation. Strict settings cover the 1.5-second
ACK deadline, payload/attachment ceilings, and monotonic employee/team/global
queue limits. A dedicated development-only Phase 3 manifest binds exact
selectors, commit, artifact, locked SDK wheel/capability artifact, and pytest
phase summary; `EI-IPC-01` remains explicitly pending and cannot be satisfied by
platform SDK evidence. This outcome does not provide Inbox durability.

## Task 2: Journal-backed employee Inbox and encrypted payloads

**Tests first**

- Add `tests/autonomous/unit/test_employee_durable_inbox.py`.
- Add `tests/autonomous/chaos/test_employee_ingress_recovery.py`.
- Add true concurrent duplicate admission, restart replay, generation replay,
  fallback action identity, semantic conflict, Blob corruption, Blob failure,
  fsync failure, and anchor failure.

**Implementation**

- Create `src/autonomous/ingress/projection.py` and `service.py`; do not reuse
  the legacy generic `manager.DurableInbox`, whose key and payload schema are
  insufficient for employee authority.
- Give Ingress a dedicated encrypted BlobStore root and sole close/GC owner,
  while reusing the employee data keyring provider. Do not share the Data
  service's BlobStore: its live-set does not include Ingress records. Commit only
  safe metadata plus authenticated Blob reference/hash.
- Under one writer transaction guard: synchronize projection, detect dedup,
  write and anchor `employee.ingress.accepted`, apply the frame, then return a
  stable acceptance. Check dedup before publishing a new randomized-nonce Blob;
  compare incoming key/digest/provenance and verify the original acceptance's
  authenticated Blob ref. Return duplicate only when key, semantic digest,
  sender/chat/action provenance, and authenticated Blob ref/hash all match.
  Same key with different digest/provenance is a durable security conflict: no
  success ACK and no Router entry. Fallback `action_identity` must be a trusted,
  server-generated correlation; missing correlation is rejected rather than
  derived from user-controlled action JSON.
- Blob AES-GCM AAD binds schema, tenant, employee, canonical envelope/dedup
  identity, and semantic digest.
- Recovery verifies Blob references before reopening admission. Missing or
  corrupt payload for nonterminal records closes employee ingress. Terminal
  disposition makes payload eligible for durable tombstone/GC; historical
  acceptance metadata remains. Blob publish followed by failed Journal anchor
  is quarantined as an orphan and never routed. Exactly one Ingress service owns
  and closes this BlobStore.
- Land `EI-IPC-01` with this task, including exact selector collectability and
  bound result evidence. Task 7 may aggregate it but may not invent it later.

Commit: `feat(autonomous): persist employee ingress before ack`

**Task 2 outcome (2026-07-13):** complete. Employee ingress now owns a
dedicated AES-GCM BlobStore and a Journal-backed projection. Admission validates
trusted correlation and payload bounds, checks durable dedup before randomized
Blob publication, verifies authenticated Blob labels/content, anchors
`employee.ingress.accepted`, applies the frame, and only then returns the ACK.
Canonical acceptance survives reconnect/generation replay; semantic or
provenance conflicts fail closed. Startup replay closes admission for missing or
corrupt nonterminal payloads, quarantines pre-commit orphan Blobs, and retries
physical cleanup after an anchored tombstone. Dispositions are strictly
validated before Journal commit so malformed lifecycle input cannot poison
replay.

The exact `EI-IPC-01` selector is now collectable and uses a real spawned child,
`multiprocessing.Pipe`, `FileAnchor`, Journal file and fsync boundary. Its
observed ACK latency was `0.014952s` against the `1.5s` limit. This remains local
Phase 3 evidence and is not FI-29 or production readiness. Independent final
review approved both specification compliance and code quality; Task 3-7 and
`autonomous_visible_employee_limit=0` remain unchanged.

## Task 3: Official low-level Channel ACK bridge

**Tests first**

- Extend Channel protocol tests with strict `INGRESS` and `INGRESS_ACK` frames,
  canonical `IngressAcceptance` reference, correlation, sequence, generation,
  connection, timeout, stale ACK, and secret rejection.
- Add independent production-bridge wire selectors, still without replacing SDK
  dispatch/write internals. If message or real P2 `card.action.trigger` cannot
  be held until durable ACK, keep capability RED:
  - `EI-BRIDGE-MESSAGE-01` →
    `tests/autonomous/integration/test_employee_channel_bridge.py::test_message_wire_response_waits_for_durable_parent_ack`
  - `EI-BRIDGE-CARD-01` →
    `tests/autonomous/integration/test_employee_channel_bridge.py::test_card_action_wire_response_waits_for_durable_parent_ack`
  Both are local `integration` evidence in `local_channel_wire_harness`; Task 0
  selectors retain their fixed SDK-only semantics.
- Add chaos tests for crash before commit, commit then lost ACK, parent close,
  stale worker generation, and bounded timeout/non-success response.

**Implementation**

- Replace production message/card ingress with the official low-level
  `lark_channel.ws.Client + EventDispatcherHandler` synchronous callback.
- Force SDK strict security mode and WSS; reject insecure/local-insecure URLs,
  disable environment proxy discovery unless an explicit proxy allowlist is
  configured, cap fragments/concurrent handlers, and drop resource overflow.
  Raw SDK `MessageType.CARD` is known RED in 1.1.0 because Client returns without
  dispatch. The only card-action candidate is a real P2
  `card.action.trigger` carried as `MessageType.EVENT`, independently wire-tested.
- The worker sends a strict ingress frame and waits at most the configured ACK
  timeout for the matching parent ACK. A dedicated OS reader thread must receive
  ACKs because the synchronous SDK callback blocks its event loop. It never
  treats a write to the pipe as durability.
- The parent validates the runtime-bound agent/app/generation, calls the durable
  Inbox, and sends ACK only for an anchored first or duplicate record.
- Durable acceptance binds canonical envelope/dedup/digest/Journal identity.
  Each transport ACK newly binds current request, employee, app, generation,
  connection, and that acceptance. Cross-employee, stale-generation,
  mismatched, and late ACKs are ignored. All parent control-pipe writes share one
  sequence/write lock so ACK, STOP, and SEND frames cannot interleave.
- Keep reconnect/error notifications non-blocking. Connection readiness must be
  based on an observed official connection capability, not merely process
  liveness or endpoint discovery.
- Keep employee outbound transport separate; no second WS connection for the
  same app and no main-Bot fallback.

Commit: `feat(autonomous): ack employee events after journal anchor`

**Task 3 outcome (2026-07-13):** complete. The production worker now uses only
the pinned official low-level `WSClient + EventDispatcherHandler`; its
synchronous message and P2 card callbacks share a strict 1.5-second deadline
covering connection admission, child-to-parent IPC, Journal anchor and the
matching transport ACK. The parent validates the current tenant, employee, Bot,
app, generation and observed connection before calling the durable Inbox. ACK,
STOP and SEND frames share one sequence/write lock; reconnect rotates the
connection epoch, cancels old pending ownership and publishes READY before any
new INGRESS.

The exact `EI-BRIDGE-MESSAGE-01` and `EI-BRIDGE-CARD-01` integration selectors
pass against the real HTTP discovery, WSS/TLS, protobuf dispatcher and SDK
response writer. The mandatory fault matrix covers IPC backpressure/partial
writes, parent close, anchor/projection/ACK encode/control write failures, late
or lost ACK, child crash, post-callback SDK write failure, reconnect/STOP/
generation fencing and replay convergence. Oversized single frames produce
wire non-success with zero Inbox/Journal/Router/ACP side effects. Independent
final review approved specification compliance and code quality. This remains
local Phase 3 evidence; Task 5-7, Task 7 production aggregation, FI-29 and real
tenant release remain pending, so `autonomous_visible_employee_limit=0` is
unchanged.

**Mandatory bridge fault matrix**

| Fault point | Wire result | Durable state / replay invariant |
| --- | --- | --- |
| partial IPC frame; complete INGRESS then child crash | no success | zero acceptance or one anchored acceptance; redelivery converges to one |
| Blob published before Journal anchor failure | non-success | orphan quarantined; zero Router/ACP |
| anchor succeeds; projection apply, ACK encode, or pipe write fails | non-success | one acceptance; redelivery gets new transport ACK for same acceptance |
| child timeout/500 before parent late commit | first non-success | late ACK ignored; platform replay dedups to one acceptance/attempt |
| ACK received before callback return; child crash | no observed success | replay dedups; at most one dispatch |
| callback returns before SDK write; disconnect/write failure | no observed success | replay dedups; at most one dispatch |
| wire 200 then worker or parent restart | success | replay, if any, returns duplicate ACK; one attempt/ACP call |
| pending callback plus STOP/reconnect/generation rotation | bounded non-success or matched success | no deadlock, no cross-generation ACK ownership |
| concurrent ACK/STOP/SEND | protocol-valid frames | single writer ordering and correct demultiplex |
| SDK 500 then harness redelivery | first 500, later 200 | one Inbox, one attempt, one ACP call |

Every row asserts the wire response, Journal/projection state, replay result,
Router/ACP counts, and readiness; logs or exception text alone are not evidence.

## Task 4: Employee-scoped attachment staging

**Tests first**

- Add `tests/autonomous/security/test_employee_ingress_attachments.py`.
- Cover count, per-file/total size, timeout, MIME plus magic mismatch,
  executable rejection, traversal, leaf/parent symlink, hash mismatch, employee
  credential selection, URL/path descriptor rejection, crash cleanup, and
  restart recovery.

**Implementation**

- Create `src/autonomous/ingress/attachments.py`. The three-second ACK path only
  encrypts typed resource descriptors; it never downloads attachments. After
  Router authorization, asynchronously enter durable `staging_started`, use the
  target employee credential, and never fall back to Manager Bot credentials.
- A descriptor accepts only official SDK resource IDs and typed message
  coordinates. URLs, absolute/local paths, and payload-supplied attempt or
  envelope IDs are forbidden; the server allocates storage identity.
- Use random storage names below a fixed tenant/employee/envelope root, modes
  `0700/0600`, root-relative dir-fd traversal, no-follow checks, and content hash
  verification. User filenames are metadata only and never form a path.
- Store beneath an envelope/task ID root because dispatch attempt is not yet
  committed. Persist `staging_started/completed/failed` plus cleanup state so
  restart knows every temporary path's owner. Only completed staging may reach
  Context/dispatch; failure is terminal for that ingress record and causes zero
  ACP calls. ACP receives only Gateway-produced trusted paths. Cleanup is a
  durable disposition with recovery for interrupted deletion.

Commit: `feat(autonomous): stage employee attachments safely`

**Task 4 outcome (2026-07-13):** complete. The ACK path stores only encrypted,
typed official resource descriptors and performs no download. After trusted
authorization, staging uses only the target employee credential and the
official `lark-oapi` message-resource API, with no Manager Bot fallback. The
filesystem protocol binds parent and leaf identities durably, uses fixed
tenant/employee/envelope roots with `0700/0600`, dir-fd/no-follow traversal,
server-random names, and validates count, size, timeout, MIME/magic, executable
content, hashes, hardlinks, generations and exact path ownership before any
trusted path can be exported.

Cleanup erases attachment bytes through the Journal-bound exact inode fd with
`ftruncate(0)` and `fsync`, retains exact zero-byte tombstones through aggregate
completion, and fresh-reopens every target before committing the aggregate
disposition. Post-completion recovery is observation-only: it deliberately does
not perform pathname unlink/rename/replace because POSIX provides no atomic
unlink conditioned on the recorded device/inode, so it cannot delete or move a
replacement. Thus `cleanup_completed` guarantees durable sensitive-byte
erasure, not directory-entry deletion.

The final focused suite passed 70 tests; the expanded ingress suite passed 332
with 1 skip; the full Autonomous suite passed 1479 with 2 skips. Ruff, document
references, configuration validation and diff checks passed. Independent final
review approved both specification compliance and code quality with no Critical
or Important findings. This remains local Phase 3 evidence; Tasks 5-7, Task 7
production aggregation, FI-29 and real-tenant release remain pending, so
`autonomous_visible_employee_limit=0` is unchanged.

## Task 5: Authority-bound durable Router and bounded queues

**Tests first**

- Add `tests/autonomous/security/test_employee_ingress_authority.py`.
- Add `tests/autonomous/integration/test_employee_router_queues.py`.
- Cover ACTIVE/lifecycle, principal/app/generation, tenant, membership,
  requester ACL, bot-loop suppression, stale projection, queue full, durable
  FIFO restart, card-correlation replay/expiry/cross-binding, and two-employee
  parallel barriers.
- Land `EI-QUEUE-01` at
  `tests/autonomous/integration/test_employee_router_queues.py::test_two_employees_are_isolated_under_team_and_global_queue_limits`
  as local `integration` evidence in `local_process_harness`.

**Implementation**

- Replace the in-memory `EmployeeMessageRouter` production path with an ingress
  Router that consumes only projected Inbox records and decrypted payloads.
- Resolve authority from `ProjectedAgentRegistry` and live Channel status; raw
  payload fields can only reduce authority, never grant it.
- Atomically persist dispositions and queue positions. Per employee concurrency
  is one; enforce configured team/global limits without allowing employee A to
  block employee B. "Team" means the current activated Slock group/chat ID,
  persisted in the authority snapshot.
- Persist the state machine `accepted -> authorized -> staging -> queued ->
  dispatching -> terminal`; Journal sequence is the FIFO tie-breaker. Dequeue
  revalidates sender open-id type/tenant/sender type, non-DEGRADED membership,
  requester ACL, lifecycle, and Channel authority.
- Until Phase 4 creates a trusted employee card issuance chain, card actions are
  durably `unsupported` and never reach Router/ACP. Future legal actions require
  a server-generated one-time expiring correlation bound to tenant, employee,
  app, chat/message, operator, action, and generation; consume correlation and
  disposition atomically. User-provided action JSON never grants authority.
- Construct `AuthorizedContextRequest` only from the frozen authority snapshot,
  current message coordinates, and trusted constraints digest/reserve.

Commit: `feat(autonomous): route durable employee inbox records`

## Task 6: Attempt anchor, Context gate, and real Slock gateway

**Tests first**

- Add `tests/autonomous/integration/test_employee_slock_gateway.py`.
- Add `tests/autonomous/integration/test_employee_terminal_pipeline.py`.
- Add `tests/autonomous/chaos/test_employee_attempt_recovery.py`.
- Spy on the real engine `_run_acp_session`, not a fake execution port, and
  prove one call for concurrent/replayed delivery of one accepted attempt.
- Parameterize completed, failed, canceled, timeout, and action_required;
  verify terminal monotonicity and that history failure blocks false success.
- Prove ingress cannot select identity/tool/model/effort/permissions, a worker
  without shell or write capabilities cannot use shell/lark-cli or equivalent
  writes, and ACP process/env cannot read Vault master, Manager Bot, or another
  employee's credentials. Scan Journal/log/error output for secret and plaintext
  payload leakage. Prove each `DispatchPermit` is consumed once.
- Land these frozen gates with the tests, not later in Task 7:
  - `EI-ACP-ONCE-01` →
    `tests/autonomous/integration/test_employee_slock_gateway.py::test_replay_dispatches_one_real_slock_session`
    (`integration`, `local_slock_harness`)
  - `EI-TERMINAL-01` →
    `tests/autonomous/integration/test_employee_terminal_pipeline.py::test_every_started_attempt_has_one_terminal_or_action_required`
    (`integration`, `local_process_harness`)
  - `EI-RECOVERY-01` →
    `tests/autonomous/chaos/test_employee_attempt_recovery.py::test_unknown_dispatch_recovers_action_required_without_rerun`
    (`chaos_security`, `local_process_harness`)

**Implementation**

- Add `EmployeeDispatchCoordinator` as the only dispatch commit path. Its exact
  lock order is workforce/hire service mutex -> Ingress service mutex -> Data
  service mutex -> Channel authority guard -> `writer.transaction_guard`; all
  participating mutations use this compatible prefix order and never acquire in
  reverse.
  In one short critical section it synchronizes workforce/hire, ingress, and
  data projections to the same Journal head, revalidates live Channel authority,
  and commits immutable attempt binding plus `attempt.dispatch_committed` in one
  frame. Channel generation/connection changes use the same authority guard to
  eliminate TOCTOU.
- Return a frozen, atomically one-shot `DispatchPermit` and release every lock
  before external ACP. It binds Inbox/envelope/dedup identity,
  employee/Bot/app/generation, historical `ingress_connection_id`, current
  `authority_connection_id`, requester identity, activated Slock
  engine/chat/root identity, effective tool/model/effort/permissions, and
  Context snapshot/watermark digest. Reconnect does not itself invalidate an
  accepted event; current authority must still pass at dispatch.
- Render the already-budgeted Context snapshot directly in strict layer order.
  Do not call Slock's legacy prompt builder because it rereads legacy replay,
  group, and global memory and would violate the Context contract.
- Resolve the projected Slock identity and activated chat engine, then call
  `run_agent_session()` exactly once with the employee model selection and
  timeout. No activated engine produces a durable rejection; never create a
  legacy engine or use cwd fallback. Do not modify `_run_acp_session`.
- Add atomic `finalize_attempt()`: stage the history Blob, then commit
  `employee.execution_attempt.terminal` and history metadata/reference in one
  Journal frame. Reducer is first-terminal-wins with monotonic terminal epoch;
  identical replay is idempotent, conflicting terminal fails closed. Blob or
  history failure produces neither success terminal nor success response.
  Recovery maps `dispatch_committed && !terminal` to `action_required` without
  rerun.

Commit: `feat(autonomous): dispatch anchored employee attempts to slock`

## Task 7: Production composition, recovery, and Phase 3 handoff

**Tests first**

- Extend `test_employee_hire_composition.py` for Inbox/Router/Gateway ownership,
  recovery, reverse shutdown, and stale generation invalidation.
- Extend release gates with real selectors for IPC ACK, platform ACK capability,
  queue isolation, one ACP call, terminal completeness, and unknown recovery.
- Replace the stale FI-29 selector with tests that distinguish IPC durability
  from actual platform response order.
- Collect every frozen selector by exact node ID and verify evidence binding;
  Task 0/2 artifacts are development evidence only and may not be relabeled as
  final. On the final Phase 3 candidate commit, CI/build reruns all nine exact
  selectors: two `EI-PLATFORM-*`, `EI-IPC-01`, two `EI-BRIDGE-*`,
  `EI-QUEUE-01`, `EI-ACP-ONCE-01`, `EI-TERMINAL-01`, and `EI-RECOVERY-01`, and
  emits a trusted build attestation outside Git, so the commit does not
  self-reference. Runtime accepts only that final-build attestation.
- Harden the global manifest evaluator to validate selector, commit, and bound
  result before FI-29 can bridge specifically to `EI-IPC-01`. Until that bridge
  validates, FI-29 remains pending; arbitrary `passed=true` evidence cannot
  satisfy it. Platform SDK and production bridge gates stay distinct and never
  count as real-tenant evidence.

**Implementation**

- Inject `SlockEngineManager` into `EmployeeDepartmentRuntime` from the existing
  `FeishuWSClient` composition without changing main-Bot routing.
- Runtime shutdown order is ingress admission, ACK waiters, Router/queue workers,
  Context work, Slock attempts, Context sources, employee Channels, data, hire
  service/writer, then Vault.
- Shutdown has a bounded grace: stop admission/new scheduling, settle in-flight
  ACK callbacks, then terminate employee Channel workers. ACP calls beyond grace
  are not silently rerun; restart maps their dispatch-anchored nonterminal state
  to `action_required`.
- Execution readiness requires the durable Inbox, platform ACK capability,
  Router, Context, data/attempt anchoring, and Slock gateway. Any missing
  dependency is a stable blocker.
- Runtime platform capability must match trusted build attestation for GhostAP
  commit, exact SDK version, installed wheel/artifact digest, and all four wire
  selector results: SDK-only message/card plus production-bridge message/card.
  The bridge artifact also asserts exactly one inbound WS connection for the
  same app. A missing, failed, mismatched, or extra-connection result produces a
  stable readiness blocker. `pyproject.toml` version text alone is never
  sufficient.
- Keep `autonomous_visible_employee_limit=0` until external release trust and
  real tenant gates in later phases are complete.

**Verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/ -q
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/test_slock_role_creation.py \
  tests/test_ws_client_routing.py \
  tests/test_contextvar_propagation.py \
  tests/test_project.py \
  tests/test_docs_references.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check src/autonomous/
uv --cache-dir /tmp/ghostap-uv-cache run python -m src.main --validate
git diff --check
```

Commit: `test(autonomous): close durable employee ingress phase`

## Phase completion boundary

Phase 3 is complete only when Tasks 0-7 are pushed to `dev`, all three reviews
approve, the official SDK black-box platform ACK ordering test passes, and every
accepted attempt has durable, restart-safe disposition. It still does not prove
employee-owned response cards, team/stop/fire behavior, canonical producer
cutover, external release trust, or real-tenant acceptance. Those remain active
in `docs/goals.md` Phases 4-9.
