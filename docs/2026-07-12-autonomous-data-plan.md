# Autonomous Employee Data Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `subagent-driven-development` task-by-task, with TDD and an independent
> spec/quality review after every task.

**Goal:** Make employee execution history, L1 memory, skill profile, and
reasoning artifacts durable Journal facts backed by encrypted blobs, rebuildable
file projections, strict tenant/requester ACLs, and an idempotent one-time Slock
data migration.

**Architecture:** Sensitive values are published to the existing encrypted
`BlobStore` before an authenticated Journal transaction is committed. Journal
events contain only strict metadata and one top-level `blob_ref`; replay builds
immutable metadata indexes. Daily JSONL, `memory/MEMORY.md`, skill, and reasoning
files are disposable projections. Public reads go through an ACL-aware query
service and validate blob labels again before decryption. Physical blob deletion
is deferred because the current Journal integrity check requires historical
references to remain readable.

**Tech Stack:** Python 3.12+, frozen dataclasses, existing AES-GCM BlobStore,
JournalWriter/ProjectionRepository, zoneinfo, pydantic-settings, pytest, uv,
ruff.

## Binding Decisions

- Journal is the only fact source. Projection files and daily JSONL are caches.
- A history record belongs to one tenant and one employee; row visibility is
  additionally scoped by chat, requester, and task.
- Full L1 is readable only by a configured department admin through the main
  Bot DM. Group callers receive a current-chat safe summary only.
- Admin status never bypasses tenant isolation. Cross-tenant break-glass is not
  implemented in this slice.
- Data encryption uses a distinct versioned keyring setting, never the Bot
  credential Vault keyring:
  `{"version":1,"keys":{"key-id":"base64-32-byte-key"}}`.
- `key_ref` is the active data key ID. Old keys remain readable for rotation.
- A key cannot be removed while any historical Journal event references it.
- Retention is logical tombstoning only. No referenced blob is physically
  deleted until Journal live-reference validation supports retention.
- Full Journal replay is authoritative. `SnapshotStore` remains an unused cache
  and is not expanded in this plan.
- Existing Slock virtual agents retain legacy storage until imported. New
  canonical employee execution must use `EmployeeDataService`; no long-term
  dual write is allowed.
- Missing tenant, owner, requester, canonical employee ID, Blob key, or ACL is
  fail-closed.
- Missing or authentication-invalid live blobs make Autonomous startup fail
  globally. Tenant-scoped degraded recovery is deferred until Journal replay can
  separate cryptographic replay from typed live-resource health reporting.
- Public query callers never supply admin, DM, owner, or membership booleans.
  A trusted context factory derives them from authenticated transport identity,
  configured admins, Bot identity, and Journal membership projections.
- Canonical producer cutover is part of this plan. Isolated storage components
  are not considered delivered until real terminal/L1/skill/reasoning paths use
  them and legacy writers are epoch-fenced.
- Deep, Spec, Worktree, Workflow, main Bot WebSocket, and `_run_acp_session`
  remain unchanged.

## Canonical Schemas

### `ExecutionHistoryRecordV1`

Strict frozen fields (unknown/missing fields rejected):

```text
schema_version = 1
record_id, occurrence_key, terminal_epoch
tenant_key, agent_id, owner_principal_id, requester_principal_id
task_id, run_id, attempt_id, message_id, thread_root_id, chat_id
started_at, ended_at, duration_ms, shard_day, shard_timezone
tool, model, effort
status = completed|failed|canceled|timeout|action_required
safe_summary
prompt_tokens, completion_tokens, total_tokens
tool_usage: tuple[{name,count,duration_ms,status}]
predecessor_sequence, predecessor_hash
```

`occurrence_key` is the canonical SHA-256 of
`tenant|agent|run|attempt|terminal_epoch`; `record_id` is deterministically
`hist_<occurrence-key>`. Retrying an anchored terminal write therefore returns
the existing record, while a different payload for the same occurrence is a
conflict. Times are timezone-aware UTC RFC3339. The
`shard_day` is computed from `ended_at` in the configured IANA timezone. Raw
prompt/result/error/tool arguments never enter the event or JSONL projection;
they remain only inside the encrypted blob.

### `ExecutionHistoryPayloadV1`

The encrypted Blob payload has exact fields:

```text
schema_version = 1
record_id, occurrence_key
request_text, result_text, error_detail
attachments: tuple[{resource_type,resource_id,name,mime_type,size,sha256}]
tool_calls: tuple[{name,status,duration_ms,input_summary,output_summary}]
```

Unknown keys, control characters, invalid attachment hashes, and unbounded
strings are rejected. Only department-admin/main-Bot-DM queries may decrypt this
payload. `safe_summary` is not caller text: `SafeExecutionSummary` generates a
fixed localized sentence from terminal status, allowlisted error category, and
numeric counts. Metadata fields have explicit length/control-character limits.

### `EmployeeDataDocumentV1`

```text
schema_version = 1
document_id, tenant_key, agent_id, owner_principal_id
kind = l1_memory|memory_summary|skill_profile|reasoning
version >= 1
source_id                     # task ID for reasoning, otherwise canonical kind
created_at, predecessor_sequence, predecessor_hash
content_type = text/markdown|application/json
content_hash
previous_document_id | ""
legacy_source_hash | ""
```

`document_id` is `data_<random>`. `(tenant, agent, kind, source_id)` is one
version chain. Reasoning source IDs are preserved in metadata and never
sanitized into authoritative identity.

For `memory_summary`, `source_id` is the SHA-256 of canonical JSON
`{"chat_id":"...","thread_root_id":"..."}`. The unhashed pair remains strict
event metadata. Empty chat IDs are rejected; an empty thread root denotes a
chat-level summary.

### Required Blob labels

History labels are exactly:

```json
{"tenant_key":"...","owner_principal_id":"...","classification":"restricted","purpose":"execution_history","resource_id":"<record_id>","schema_version":"1"}
```

Document labels replace purpose with `l1_memory`, `memory_summary`,
`skill_profile`, or `reasoning`, and resource ID with `document_id`. Query
services compare every label with event metadata before returning plaintext.

### Journal events

- `employee.history.recorded`, aggregate ID `record_id`.
- `employee.history.tombstoned`, aggregate ID `record_id`.
- `employee.data.published`, aggregate ID
  `data-chain:<tenant-hash>:<agent-id>:<kind>:<source-id-hash>`.
- `employee.data.tombstoned`, same data-chain aggregate.
- `employee.data.projection_failed` / `employee.data.projection_recovered`,
  aggregate ID `data-health:<tenant-hash>:<agent-id>`; payload contains only
  error category, affected projection kind, attempt count, and timestamps.
- `employee.execution_attempt.started`, aggregate ID `attempt_id`, anchored
  before ACP dispatch. It binds tenant, employee, owner, requester, task, run,
  attempt, chat/thread/message, tool/model/effort, start time, and
  `terminal_epoch=1`. Resume reuses it; explicit retry creates a new attempt.
- `employee.data.authority_cutover`, singleton aggregate
  `employee-data-authority`, with an epoch/mode/cutover sequence independent
  from workforce identity authority.
- `employee.data.read_audited`, aggregate ID
  `data-audit:<sha256(request-id|operation|resource-id)>`, containing only
  authenticated transport/resource metadata, outcome/reason category, and time.
- `employee.legacy_data_imported`, aggregate ID
  `legacy-data:<tenant-hash>:<agent-id>:<source-locator-hash>`.

Every publish payload contains exact non-secret schema metadata plus one
top-level `blob_ref`. Reducers verify aggregate identity, employee tenant,
labels, content hash, monotonic version/predecessor, and predecessor Journal
head. `publish_sequence/publish_frame_hash` are stamped into Projection records
from the committed frame; they never appear in a pre-commit Blob or event and
therefore create no hash cycle.

## File Structure

Create:

- `src/autonomous/data/__init__.py`
- `src/autonomous/data/models.py`
- `src/autonomous/data/keyring.py`
- `src/autonomous/data/service.py`
- `src/autonomous/data/projection.py`
- `src/autonomous/data/materializer.py`
- `src/autonomous/data/query.py`
- `src/autonomous/data/policy.py`
- `src/autonomous/data/composition.py`
- `src/autonomous/data/ports.py`
- `src/autonomous/migration/slock_data_importer.py`
- `tests/autonomous/data_helpers.py`
- `tests/autonomous/unit/test_employee_data_models.py`
- `tests/autonomous/unit/test_employee_data_projection.py`
- `tests/autonomous/security/test_employee_data_blob_policy.py`
- `tests/autonomous/security/test_employee_data_acl.py`
- `tests/autonomous/integration/test_employee_history_rebuild.py`
- `tests/autonomous/integration/test_employee_memory_projection.py`
- `tests/autonomous/integration/test_slock_data_migration.py`
- `tests/autonomous/chaos/test_employee_data_commit_boundaries.py`
- `tests/autonomous/integration/test_employee_data_composition.py`
- `tests/test_slock_execution_history.py`
- `tests/test_slock_memory_command.py`

Modify:

- `src/config/settings.py`
- `src/autonomous/bootstrap.py`
- `src/autonomous/supervisor/supervisor.py`
- `src/autonomous/journal/projections.py`
- `src/autonomous/journal/writer.py`
- `src/autonomous/workforce/registry.py`
- `src/slock_engine/memory_manager.py`
- `src/slock_engine/engine.py`
- `src/slock_engine/task_router.py`
- `src/slock_engine/discussion_manager.py`
- `src/feishu/handlers/slock.py`
- `src/feishu/handlers/slock_tasks.py`
- relevant existing Autonomous/Slock tests
- `.Memory/2026-07-12.md`, `.Memory/Abstract.md`

## Task 1: Strict Data Domain, Keyring, and Blob Policy

**Produces:** strict models and `EmployeeDataKeyring.from_settings()`.

- [ ] Add settings:

```python
autonomous_data_keys: SecretStr = SecretStr("")
autonomous_data_active_key_id: str = ""
autonomous_data_blob_dir: str = "~/.ghostap/autonomy/data-blobs"
autonomous_history_timezone: str = "UTC"
autonomous_history_max_range_days: int = Field(default=31, ge=1, le=366)
autonomous_history_page_size: int = Field(default=50, ge=1, le=200)
```

- [ ] RED tests cover strict keyring JSON, redacted repr/errors, exact 32-byte
  keys, active-key presence, valid IANA timezone, strict model keys/types,
  canonical IDs, UTC timestamps, status, token arithmetic, non-negative usage,
  timezone day boundaries, and immutable nested values.
- [ ] Implement `ExecutionHistoryRecordV1`, `ExecutionHistoryPayloadV1`,
  `SafeExecutionSummary`, `ExecutionAttemptContext`,
  `EmployeeDataDocumentV1`, `ToolUsageV1`, `DataKind` (including
  `memory_summary`), and strict canonical `to_dict/from_dict`.
- [ ] Implement exact label builders/validators. Reject extra/missing labels,
  cross-tenant/resource mismatches, plaintext secret-like label keys, and
  malformed `BlobRef` values.
- [ ] Harden `BlobStore` to descriptor-relative/no-follow root and leaf I/O,
  strict schema-version typing, regular `0600` leaf verification, explicit
  close/context cleanup, and safe `iter_blob_ids()`/`quarantine_blob()` APIs.
  Orphan enumeration compares filename hashes with projected live Blob IDs;
  it never attempts to reconstruct missing plaintext labels from envelopes.
- [ ] Add a data composition factory that resolves the active/old keys,
  constructs `AesGcmEncryptionProvider` and `BlobStore`, and fails closed when
  keys/root/permissions are unavailable. Keyring and BlobStore repr/errors must
  not disclose key material or plaintext.
- [ ] Verify:

```bash
uv run python -m pytest \
  tests/autonomous/unit/test_employee_data_models.py \
  tests/autonomous/security/test_employee_data_blob_policy.py \
  tests/autonomous/contract/test_config_and_gate_contract.py -q
uv run ruff check src/autonomous/data src/config/settings.py \
  tests/autonomous/unit/test_employee_data_models.py \
  tests/autonomous/security/test_employee_data_blob_policy.py
```

- [ ] Commit: `feat(autonomous): define employee data contracts`

## Task 2: Journal Publish Service and Replay Projection

**Produces:** `EmployeeDataService`, metadata records/indexes, verified replay,
transaction-atomic projection apply, and a public atomic Journal head API.

- [ ] Add frozen `JournalHead(sequence, logical_hash)`. Genesis is exactly
  `(0, "")`, matching current conditional-commit semantics. `get_head()` and
  aggregate-version reads are atomic under the writer mutex. Publishing uses
  `expected_head_sequence/hash`; reducers require the event predecessor to equal
  the projection cursor immediately before its publishing frame.
- [ ] `EmployeeDataService.record_history(record, sensitive_payload)` and
  `.publish_document(document, content)` execute:

```text
validate caller/employee/proposed predecessor/occurrence key
capture Journal head
canonicalize payload and publish encrypted Blob with exact labels
fully BlobStore.read() and strict-parse the result
conditionally commit one event referencing the Blob
require CommitState.ANCHORED
apply the committed frame to live ProjectionState
```

- [ ] A blob published before a rejected/failed frame is an orphan and is never
  exposed. Durable-not-anchored disables writes and is not projected.
- [ ] Add `HistoryMetadataRecord` and `EmployeeDataMetadataRecord` to
  `ProjectionState`, with indexes:

```text
history_records[record_id]
history_by_employee_day[(tenant, agent, shard_day)] -> ordered record IDs
history_by_task[(tenant, task_id)] -> ordered record IDs
history_by_occurrence[(tenant, agent, occurrence_key)] -> record ID
execution_attempts[attempt_id] -> immutable trusted attempt binding
employee_documents[document_id]
latest_employee_document[(tenant, agent, kind, source_id)] -> document ID
legacy_data_sources[(tenant, agent, source_locator_hash)] -> import manifest
data_authority -> independent epoch/mode/cutover sequence
data_read_audits[audit_id] -> anchored non-sensitive audit metadata
```

- [ ] Replay validates exact blob labels, employee tenant, predecessor/version,
  UTC day, terminal status, occurrence idempotency, and deterministic ordering
  by `(shard_day, publish_sequence, record_id)`.
- [ ] `record_history()` returns the existing record when the same occurrence
  and payload hash are retried after a lost response; a different payload is a
  typed conflict. Head races use bounded revalidation/retry and never mint a new
  ID.
- [ ] Define history/data tombstone payloads, authorization, monotonic version,
  predecessor-chain behavior, latest-index removal, query hiding, projection
  deletion, and admin audit visibility. Blobs remain physically present.
- [ ] Pure reducers validate metadata only. Add `VerifiedProjectionRepository`
  with an injected `BlobReferenceVerifier`; before applying a data frame it
  fully reads/authenticates each Blob, validates labels/payload schema, applies
  to an isolated clone, and atomically swaps state. A failure exposes no partial
  indexes. Production recovery must use this repository.
- [ ] Wire the production JournalWriter with a mandatory data-event Blob
  validator in `data/composition.py`/`bootstrap.py`. A startup integration test
  proves missing/tampered Blob, wrong key, or missing validator fails before
  Supervisor reports ready.
- [ ] Unknown data event versions fail closed. Existing unrelated unknown
  events retain the current forward-compatible behavior.
- [ ] `start_attempt()` commits the immutable binding before ACP dispatch.
  `record_history()` accepts only attempt ID, terminal outcome, and sensitive
  payload, loads identity/routing/model fields from ProjectionState, and rejects
  caller overrides. Resume/retry and terminal-epoch transitions are covered by
  state-machine tests.
- [ ] RED/chaos tests cover blob publish failure, validation failure, head race,
  frame fsync failure, anchor failure, duplicate record, stale document version,
  missing/tampered blob, cross-tenant labels, and fresh full replay.
- [ ] Verify:

```bash
uv run python -m pytest \
  tests/autonomous/unit/test_employee_data_projection.py \
  tests/autonomous/chaos/test_employee_data_commit_boundaries.py \
  tests/autonomous/unit/test_journal_writer.py \
  tests/autonomous/chaos/test_journal_blob_crash_boundaries.py -q
```

- [ ] Commit: `feat(autonomous): journal employee data records`

## Task 3: Daily History Projection and ACL-Gated Range Query

**Produces:** disposable daily JSONL shards, deterministic rebuild, and
row-filtered queries.

- [ ] `DailyHistoryMaterializer` writes
  `agents/<agent_id>/history/YYYY-MM-DD.jsonl` using a same-directory `0600`
  temporary file, file fsync, replace, and directory fsync. It uses the same
  descriptor/no-follow containment pattern as identity/Vault materializers.
- [ ] Each JSONL row contains only safe metadata, `record_id`, Blob payload
  hash, and Journal publish sequence; never raw prompt/result/error/tool args.
- [ ] `materialize_day()` and `materialize_all()` are deterministic and repair
  missing, truncated, reordered, duplicated, extra, or hash-mismatched shards.
- [ ] `AuthenticatedDataRequest` contains only transport facts:

```text
principal_id, tenant_key, receiving_bot_app_id
chat_id, chat_type, thread_root_id, requested_agent_id
```

  `EmployeeDataRequestContextFactory` verifies the ingress binding, resolves
  configured admins, main/employee Bot identity, and employee tenant from trusted
  configuration/projection. Callback payload fields cannot grant authority. A
  group inbound event proves that sender was present in that chat at request
  time; employee membership projection proves the employee belongs to that chat.
  No unimplemented requester-membership/team-owner projection is assumed. Policy
  denies by default; tenant check precedes every admin grant. Full cross-chat
  history is admin+main-Bot-DM only.
- [ ] `HistoryRangeQuery.query()` accepts inclusive ISO dates, validates range
  length, reads only the requested authoritative ProjectionState day indexes,
  and paginates with cursor
  `(shard_day, publish_sequence, record_id)`, applies row ACL before any Blob
  read, then validates labels and strict payload again. Authoritative queries use
  ProjectionState indexes, not JSONL. A shard may accelerate a query only when
  its strict manifest `(tenant,agent,day,source_sequence,source_hash,
  content_hash,row_count)` matches the projected range; otherwise it is rebuilt
  or ignored.
- [ ] Unauthorized queries perform zero Blob reads. Query access and denials
  emit non-sensitive durable audit events through an injected audit port.
  Before returning, it commits anchored `employee.data.read_audited` with a
  stable request/operation/resource occurrence ID. Lost-response retry is
  idempotent; a conflicting outcome is rejected. Audit failure is fail-closed
  for every data query.
- [ ] Success, failure, cancellation, timeout, and action-required records all
  rebuild/query identically.
- [ ] Non-admin/requester/team queries return safe metadata only and perform zero
  Blob reads. Only same-tenant department admin + main-Bot DM may request the
  encrypted detailed payload.
- [ ] Verify:

```bash
uv run python -m pytest \
  tests/autonomous/integration/test_employee_history_rebuild.py \
  tests/autonomous/security/test_employee_data_acl.py -q
```

- [ ] Commit: `feat(autonomous): rebuild daily employee history`

## Task 4: L1, Skill, and Reasoning Materialization

**Produces:** canonical employee document paths and Journal-first read/write
facades.

- [ ] Canonical projection layout:

```text
agents/<agent_id>/memory/MEMORY.md
agents/<agent_id>/skill_profile.json
agents/<agent_id>/reasoning/<sha256(source_id)>.json
```

- [ ] Update `ProjectedAgentRegistry` to expose the new L1 path.
- [ ] Materializers use allowlisted schemas, `0600`, descriptor-relative
  no-follow writes, fsync/replace/dir-fsync, and deterministic rebuild.
- [ ] `EmployeeMemoryQuery` returns full L1 only to same-tenant department
  admin in main Bot DM. Other authorized team members receive an allowlisted
  safe summary for the current chat; no raw active context, reasoning, tool
  arguments, or cross-chat knowledge.
- [ ] Add `memory_summary` as a separate Journal-backed document kind keyed by
  `(tenant, agent, chat_id, thread_root_id)`. Its strict payload is an
  allowlisted fact summary with provenance; group `/memory` reads only this
  artifact. It never derives a group-safe answer by exposing the merged global
  L1 Blob.
- [ ] `MemoryManager` becomes a compatibility facade:
  - canonical read first;
  - legacy root `MEMORY.md` read only when canonical is absent;
  - both present with different hashes raises a typed migration conflict;
  - canonical employees write only through `EmployeeDataService`;
  - legacy virtual agents keep existing OCC until imported.
- [ ] Skill/reasoning authoritative writes use version/predecessor checks;
  sanitized filenames are never used as identity.
- [ ] Verify:

```bash
uv run python -m pytest \
  tests/autonomous/integration/test_employee_memory_projection.py \
  tests/autonomous/security/test_employee_data_acl.py \
  tests/test_slock_memory.py tests/test_slock_occ_dual_version.py \
  tests/test_slock_memory_cross_group.py -q
```

- [ ] Commit: `feat(autonomous): project employee memory documents`

## Task 5: Idempotent Legacy Data Migration and Recovery

**Produces:** one-time import, quarantine, tombstones, and projection recovery.

- [ ] `SlockDataImporter` scans only a projected canonical employee's legacy
  directory after resolving its durable alias, tenant, and owner.
- [ ] Import sources:
  `execution_history.jsonl`, root `MEMORY.md` plus `.version`,
  `skill_profile.json`, and `reasoning/*.json`.
- [ ] Separate stable `source_locator_hash` (canonical relative path + kind)
  from `content_hash`. A file-level manifest stores locator, whole-file hash,
  import version, state, and imported object IDs. Repeated locator+hash creates
  zero events; same locator with changed hash is a conflict.
- [ ] Each legacy JSONL row uses deterministic identity derived from
  `(source_locator_hash, whole_file_hash, canonical_line_ordinal)` and records
  its row hash. Bounded batch progress events make partial-import crashes
  resumable without duplicate records.
- [ ] History maps only `success=true` to completed and `false` to failed;
  canceled/timeout/action-required are never invented. Missing tenant/chat/
  requester data is resolved only from durable evidence. Actual legacy rows are
  converted with deterministic synthetic run/attempt IDs,
  `ended_at=ts`, `started_at=ts-duration`, explicit unknown thread/effort, and
  `legacy_attribution=unknown`; the migration operator is never fabricated as
  historical requester. Unattributed rows are admin-only or quarantined.
- [ ] If canonical and legacy L1 both exist with different content, quarantine
  and fail the employee migration. Never choose by mtime.
- [ ] On successful migration, fsync a manifest and atomically rename legacy
  sources under `legacy-imported/`; create no long-term dual writes.
- [ ] Recovery enumerates Blob files not referenced by ProjectionState. It may
  remove stale unpublished temp staging. Published encrypted orphan blobs are
  retained or moved through `quarantine_blob()` with a durable safe metadata
  manifest; quarantine never stores plaintext legacy rows. Missing/auth-invalid
  live blobs fail Autonomous startup globally; no fallback to legacy files after
  migration.
- [ ] Verify:

```bash
uv run python -m pytest \
  tests/autonomous/integration/test_slock_data_migration.py \
  tests/autonomous/chaos/test_employee_data_commit_boundaries.py -q
uv run python -m pytest tests/autonomous/ -q
uv run ruff check src/autonomous/data src/autonomous/migration/slock_data_importer.py \
  src/autonomous/journal src/autonomous/workforce/registry.py
uv run python -m src.main --validate
git diff --check
```

- [ ] Update Memory and commit:
  `feat(autonomous): migrate durable employee data`

## Task 6: Production Composition and Canonical Producer Cutover

**Produces:** real terminal/document producers, authenticated read ports, and a
restart-safe one-way cutover from legacy direct files.

- [ ] Define narrow injected ports in `data/ports.py`:

```text
EmployeeDataSink.record_terminal(AuthenticatedExecutionTerminal)
EmployeeDataSink.publish_document(PublishEmployeeDocumentCommand)
EmployeeHistoryReadPort.query(AuthenticatedDataRequest, HistoryQuerySpec)
EmployeeMemoryReadPort.query(AuthenticatedDataRequest, MemoryQuerySpec)
```

  Producer envelopes are constructed by trusted orchestration from bound
  employee/tenant/requester/run/attempt state; ACP output cannot provide identity
  or authorization fields.
- [ ] Production bootstrap constructs one data keyring/provider/BlobStore,
  JournalWriter with mandatory Blob validation, verified repository, service,
  materializers, request-context factory, query ports, and recovery verifier.
  Supervisor reports ready only after Journal/anchor/live Blob verification and
  projection rebuild.
- [ ] Wrap (do not rewrite) the existing `_run_acp_session` call in Slock
  employee orchestration so every unique terminal transition records exactly one
  history occurrence:

  Before dispatch, orchestration calls `start_attempt()` and waits for its
  anchored frame. ACP receives only the resulting immutable attempt ID. Resume
  reuses it; an explicit retry first creates and anchors a new attempt binding.

```text
SUCCEEDED -> completed
FAILED -> failed
CANCELED -> canceled
EXPIRED/deadline -> timeout
effect/result unknown -> action_required
```

  A terminal data commit failure prevents a false successful employee status.
  Blob-validation or materializer failures may commit retryable health events
  while Journal remains writable. Journal append/fsync/anchor mismatch or
  write-disable cannot truthfully write another Journal health event: it disables
  data-service readiness, blocks terminal success, emits an external emergency
  signal through an injected non-Journal health port, and requires operator
  recovery. Materializer failure does not change an anchored terminal; successful
  repair commits the paired recovered event.
- [ ] Route canonical employee L1/skill/reasoning/memory-summary writes from
  `engine.py`, `task_router.py`, and `discussion_manager.py` through the sink.
  Existing virtual legacy agents continue using `MemoryManager` until imported.
  Missing trusted context for a canonical employee is rejected, never silently
  sent to the legacy writer.
- [ ] Implement one-way cutover with the existing authority serialization
  pattern using independent `EmployeeDataAuthorityGuard` state projected from
  `employee.data.authority_cutover` (never reuse workforce identity authority):

```text
close canonical employee ingress
wait/drain legacy history and daemon memory writers
flush and fsync legacy files
run/verify idempotent import
commit data-writer authority epoch/cutover sequence
switch resolver to canonical sink and reopen ingress
```

  Every legacy mutation carries the stamped epoch and rechecks immediately
  before disk I/O. A delayed pre-cutover writer is rejected after advancement.
  Failure before durable authority publication leaves legacy authority and
  retryable queues intact.
- [ ] Add `/history` and ACL-aware `/memory` typed read facades for the future
  employee Slash router, plus main-Bot admin-DM read handlers. Do not register
  employee Slash commands yet. Existing discussion-history commands remain
  distinct and cannot masquerade as execution history.
- [ ] End-to-end tests start from authenticated ingress/terminal state, not
  handcrafted policy booleans, and prove:
  - each real terminal path writes one Journal occurrence;
  - lost response/retry produces no duplicate;
  - failure is not swallowed;
  - handler payload cannot forge admin/tenant/chat/membership;
  - `/history` inclusive range and `/memory` full/summary behavior;
  - canonical employees never write `execution_history.jsonl` or root
    `MEMORY.md` after cutover;
  - legacy virtual agents still work;
  - restart reconstructs services/indexes and rejects missing/tampered blobs;
  - Deep/Spec/Worktree/Workflow routing and main Bot WS are unchanged.
- [ ] Verify:

```bash
uv run python -m pytest tests/autonomous/ -q
uv run python -m pytest \
  tests/test_slock_execution_history.py \
  tests/test_slock_memory.py tests/test_slock_memory_command.py \
  tests/test_slock_discussion_persist.py -q
uv run ruff check src/autonomous/ src/slock_engine/memory_manager.py \
  src/slock_engine/engine.py src/slock_engine/task_router.py \
  src/slock_engine/discussion_manager.py
uv run python -m src.main --validate
git diff --check
```

- [ ] Update Memory and commit:
  `feat(autonomous): cut over employee data producers`

## Data Plane Completion Gate

Fresh evidence required before writing the Thread Context plan:

```bash
uv run python -m pytest tests/autonomous/ -q
uv run python -m pytest \
  tests/test_slock_memory.py tests/test_slock_occ_dual_version.py \
  tests/test_slock_memory_cross_group.py -q
uv run ruff check src/autonomous/ src/slock_engine/memory_manager.py
uv run python -m src.main --validate
git diff --check
git status --short
```

Manual completion audit:

- deleting every generated history/memory/skill/reasoning projection and
  replaying Journal recreates byte-identical files;
- no event, shard, projection, error, or audit entry contains plaintext
  credential, raw prompt/result/error, or reasoning payload;
- unauthorized queries cause zero Blob reads;
- old `execution_history.jsonl` and root `MEMORY.md` stop receiving writes
  after successful import;
- current Slock virtual agents and all independent engines remain unchanged;
- `autonomous_visible_employee_limit` remains `0`.

After this gate, create and execute
`docs/2026-07-12-autonomous-thread-context-plan.md` for employee-scoped Feishu
message pagination, watermark/revision verification, deterministic layering,
budget trimming, and `CONTEXT_UNAVAILABLE` behavior.
