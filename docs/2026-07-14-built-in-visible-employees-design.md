# Built-in Visible Employees Runtime Design

## Outcome

Starting GhostAP with a configured main Bot makes administrator-only `/hire`
available without a release broker, signed evidence bundle, deployment
attestation, or manually supplied autonomous encryption keys. Feishu's
one-click application registration remains an interactive user flow: the user
opens the registration link and follows Feishu's official guide to create the
new Bot application. It does not require a tenant administrator approval.

An explicit `AUTONOMOUS_VISIBLE_EMPLOYEE_LIMIT=0` remains the operator's kill
switch. The default is `8`, which enables a bounded number of visible
employees on a fresh installation.

## Local runtime bootstrap

Add a focused local bootstrap component under
`src/autonomous/provisioning/`. Before employee composition, it ensures the
autonomous state directory is a real directory owned by the current user with
mode `0700`. It then loads or creates one versioned
`employee-runtime-secrets.json` file with mode `0600`.

The file contains three independently generated 256-bit keys:

- Journal frame HMAC key.
- Employee application credential Vault key.
- Employee data, ingress, and outbox encryption key.

Creation is serialized with a no-follow lock file and uses an atomically
renamed temporary file followed by directory `fsync`. Existing state is loaded
only when the envelope, key sizes, owner, type, and exact permissions are
valid. A symlink, non-regular file, wrong owner, overly broad permission, or
malformed payload fails closed and is never overwritten.

The bootstrap returns typed in-memory material. It does not write secrets into
`.env`, logs, Journal metadata, cards, or exceptions. Existing explicit key
settings remain supported and take precedence only when the complete explicit
set is valid; mixed explicit/generated key state is rejected.

## Runtime composition

`EmployeeDepartmentRuntime.from_settings()` no longer derives admission from
release trust. For a positive visible employee limit it:

1. obtains local runtime key material;
2. opens the existing local `FileAnchor` and Journal;
3. opens the local main-Bot send audit as diagnostic evidence;
4. composes Vault, data, ingress, outbox, context, membership, dispatch, fire,
   and provisioning services;
5. recovers durable state before opening `/hire` admission.

The release broker implementation may remain available as unused compatibility
code, but `FeishuWSClient`, hire readiness, recovery, and execution readiness
do not require it. Local audit failure is logged and marks the audit
incomplete; it does not prevent a new employee application from being
created. Main-Bot delivery remains separate from employee-Bot delivery.

## Employee Channel compatibility

Employee Channel startup prefers the existing `bwrap` launch contract. If the
production worker cannot be launched or attested because the host lacks
working user namespaces, the supervisor retries exactly once with the same
isolated Python worker and inherited pipe contract but without the `bwrap`
prefix.

The fallback process:

- still receives credentials only through the one-shot bootstrap pipe;
- still uses `python -I`, a minimal environment, closed file descriptors, and
  the existing parent/child control protocol;
- is recorded as `SandboxAttestation(verified=False,
  mechanism="process-fallback")`;
- emits a warning naming the degraded mechanism without exposing credentials;
- remains subject to generation fencing, durable ingress ACK, shutdown, and
  recovery rules.

No configuration flag or false `verified=True` attestation is introduced.
Hosts with working `bwrap` continue to use the stronger verified isolation.

## User flow

1. GhostAP starts and completes local employee runtime recovery.
2. A configured administrator sends `/hire <name>` in the main Bot DM.
3. The existing tool/model/profile/effort card flow produces a typed hire
   request.
4. `lark_oapi.aregister_app()` sends a registration link through the main Bot.
5. The user opens the link and follows Feishu's official guide to create the
   application; no tenant administrator approval is required.
6. The registration request enables Feishu's official agent preset and freezes
   the message, card, Slash Command, Bot-to-Bot mention, and document-comment
   scopes/events described by the official one-click agent guide.
7. GhostAP stores returned credentials in the local encrypted Vault,
   reconciles Slash Commands, starts the employee Channel, verifies its
   identity and reply, and marks the employee active.

The production low-level employee process owns a `lark-oapi` client built from
that employee's one-shot credential. It supports text, interactive-card, rich
post, card patch, and document-comment reply transports and returns a receipt
bound to the current employee app/generation/connection. This closes the
activation `/status` reply path without falling back to the main Bot.

Transport and platform configuration are not the same as autonomous workflow
policy. Automatic Agent-to-Agent handoff still needs a membership-bound Bot
sender authorization and loop budget; document-comment execution still needs
comment-event normalization, per-document authorization, comment fetch, and a
dedicated durable routing contract. Until those contracts are implemented,
their permissions and transport primitives must not be reported as an
end-to-end workflow.

Authorization cancellation, expiry, invalid SDK responses, corrupted local
security state, or failed employee identity verification remain explicit
failures. The system never falls back to a Slock virtual role.

## Testing and acceptance

- Fresh settings enable a bounded visible employee capacity.
- First startup creates valid secret material; restart returns identical keys.
- Concurrent bootstrap converges on one envelope.
- Symlink, ownership, permission, duplicate-key, malformed JSON, and truncated
  files fail closed.
- Runtime composition succeeds without a release provider or evidence files.
- Explicit limit zero keeps the employee department dormant.
- Verified `bwrap` remains the preferred Channel path.
- A simulated namespace/attestation failure performs one process fallback and
  reaches READY with a visibly unverified attestation.
- Credential resolution still happens only after the child process exists and
  before the bootstrap pipe is closed.
- Existing provisioning, recovery, DM authorization, transport, fire, data,
  and employee-response suites remain green.

The implementation is complete only after targeted tests, full
`tests/autonomous/`, Ruff, configuration validation, documentation reference
tests, and `git diff --check` pass.
