# Built-in Visible Employees Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make administrator-only `/hire` usable after a normal GhostAP startup, with local durable security bootstrap and no external release service.

**Architecture:** A focused local bootstrap owns generation and validation of three persisted runtime keys. Employee composition consumes that material with the existing `FileAnchor`, and Channel startup retries once without `bwrap` when host namespaces are unavailable while preserving an explicit unverified attestation.

**Tech Stack:** Python 3.13, pydantic-settings, lark-oapi 1.7.1, lark-channel-sdk 1.1.0, pytest, uv, ruff.

## Global Constraints

- Do not use pip or conda; run all Python checks through `uv --cache-dir /tmp/ghostap-uv-cache`.
- Keep `/hire` separate from `AgentRegistry.legacy()` and retain administrator plus main-Bot-DM authorization.
- Never write generated secrets to `.env`, logs, cards, exceptions, or Journal metadata.
- Explicit `AUTONOMOUS_VISIBLE_EMPLOYEE_LIMIT=0` remains the disable switch; the default is `8`.
- Corrupt, linked, non-owned, or over-permissive local security state fails closed.
- `bwrap` remains preferred; fallback is recorded as unverified `process-fallback` and never presented as verified isolation.

---

### Task 1: Local employee runtime secrets

**Files:**
- Create: `src/autonomous/provisioning/local_bootstrap.py`
- Create: `tests/autonomous/security/test_local_employee_bootstrap.py`

**Interfaces:**
- Produces: `LocalEmployeeRuntimeMaterial` with `journal_hmac_key: bytes`, `credential_keyring: CredentialKeyring`, and `data_keyring: EmployeeDataKeyring`.
- Produces: `load_or_create_local_employee_material(state_dir: str | Path) -> LocalEmployeeRuntimeMaterial`.
- Produces: `resolve_employee_runtime_material(settings: Any) -> LocalEmployeeRuntimeMaterial`, which uses a complete explicit three-key configuration or local bootstrap and rejects mixed configuration.

- [x] **Step 1: Write failing creation and restart tests**

```python
def test_bootstrap_creates_private_stable_material(tmp_path: Path) -> None:
    first = load_or_create_local_employee_material(tmp_path)
    second = load_or_create_local_employee_material(tmp_path)
    assert first == second
    assert stat.S_IMODE((tmp_path / "employee-runtime-secrets.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
```

- [x] **Step 2: Run the new test and confirm import failure**

Run: `uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/security/test_local_employee_bootstrap.py -q`

Expected: FAIL because `local_bootstrap` does not exist.

- [x] **Step 3: Implement strict versioned bootstrap**

```python
@dataclass(frozen=True)
class LocalEmployeeRuntimeMaterial:
    journal_hmac_key: bytes = field(repr=False)
    credential_keyring: CredentialKeyring = field(repr=False)
    data_keyring: EmployeeDataKeyring = field(repr=False)

def load_or_create_local_employee_material(
    state_dir: str | Path,
) -> LocalEmployeeRuntimeMaterial:
    """Load or atomically create mode-restricted local employee keys."""
```

Use an `O_NOFOLLOW` lock, exact owner/type/mode checks, duplicate-key rejecting JSON, three independent `secrets.token_bytes(32)` values, atomic replace, file and directory `fsync`, and secret-free exceptions.

- [x] **Step 4: Add corruption and concurrency tests**

Cover symlink, wrong file mode, malformed envelope, duplicate JSON keys, truncated file, and concurrent callers converging on one material set.
Also cover a complete explicit settings set taking precedence and any partial
explicit settings set raising a secret-free configuration error.

- [x] **Step 5: Run bootstrap security tests**

Run: `uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/security/test_local_employee_bootstrap.py -q`

Expected: PASS.

### Task 2: Default local runtime composition

**Files:**
- Modify: `src/config/settings.py`
- Modify: `.env.example`
- Modify: `src/autonomous/provisioning/composition.py`
- Modify: `src/autonomous/provisioning/hire_service.py`
- Modify: `src/feishu/ws_client.py`
- Modify: `tests/autonomous/integration/test_employee_hire_composition.py`
- Modify: `tests/autonomous/unit/test_employee_hire_service.py`

**Interfaces:**
- Consumes: `load_or_create_local_employee_material()` from Task 1.
- Produces: a recovered `EmployeeDepartmentRuntime` with non-null `hire_service` for default settings and no release provider.

- [x] **Step 1: Write failing default-settings and composition tests**

```python
def test_visible_employee_capacity_is_enabled_by_default() -> None:
    assert Settings().autonomous_visible_employee_limit == 8

def test_local_composition_does_not_require_release_provider(tmp_path: Path) -> None:
    runtime = EmployeeDepartmentRuntime.from_settings(_local_settings(tmp_path))
    try:
        assert runtime.hire_service is not None
        assert runtime.hire_readiness().ready is True
    finally:
        runtime.close()
```

- [x] **Step 2: Run focused composition tests and confirm release blockers**

Run: `uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/integration/test_employee_hire_composition.py tests/autonomous/unit/test_employee_hire_service.py -q`

Expected: FAIL with default limit/release evidence assertions.

- [x] **Step 3: Compose from local material and FileAnchor**

Replace release-derived admission with local material and set the service readiness inputs as follows:

```python
material = resolve_employee_runtime_material(settings)
writer = JournalWriter.open(
    Path(settings.autonomous_journal_dir).expanduser(),
    anchor=FileAnchor(settings.autonomous_anchor_path),
    hmac_key=material.journal_hmac_key,
    writer_epoch=time.time_ns(),
)
```

Pass the material keyrings directly into Vault/data/ingress/outbox composition. Open local main-Bot audit when possible, but do not add it to hire readiness. Remove `_external_resume_allowed` and release-session renewal from normal startup/recovery.

- [x] **Step 4: Remove FeishuWSClient broker construction**

Call `EmployeeDepartmentRuntime.from_settings()` without a release provider while preserving the registration-link callback and handler-service injection.

- [x] **Step 5: Run composition, handler, and transport regressions**

Run: `uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/integration/test_employee_hire_composition.py tests/autonomous/unit/test_employee_hire_service.py tests/test_im_client_sanitize.py tests/test_feishu_card_api_client.py -q`

Expected: PASS.

### Task 3: bwrap-first Channel fallback and release documentation

**Files:**
- Modify: `src/autonomous/supervisor/employee_channels.py`
- Modify: `tests/autonomous/security/test_employee_channel_isolation.py`
- Modify: `tests/autonomous/contract/test_employee_channel_contract.py`
- Modify: `docs/goals.md`
- Modify: `.Memory/2026-07-14.md`
- Modify: `.Memory/Abstract.md`

**Interfaces:**
- Produces: one automatic direct-process retry after bwrap launch/attestation failure.
- Preserves: `ChannelProcessStatus.sandbox` with `verified=False` and `mechanism="process-fallback"`.

- [x] **Step 1: Write failing fallback tests**

```python
def test_production_channel_falls_back_when_bwrap_attestation_fails(...) -> None:
    status = supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda event: None)
    assert status.state is ChannelProcessState.READY
    assert status.sandbox == SandboxAttestation(
        pid=status.pid,
        verified=False,
        mechanism="process-fallback",
    )
    assert launcher.call_count == 2
```

- [x] **Step 2: Run isolation tests and confirm current fail-closed behavior**

Run: `uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/security/test_employee_channel_isolation.py tests/autonomous/contract/test_employee_channel_contract.py -q`

Expected: FAIL because unverified workers are currently reaped without retry.

- [x] **Step 3: Add one-shot fallback without false attestation**

Refactor child launch into a helper that can build either the bwrap or direct
`python -I` contract. Reap the failed bwrap child and recreate all three pipes
before the fallback. Resolve credentials only after the accepted fallback
child exists. Never reuse descriptors or generation state from the failed
attempt.

- [x] **Step 4: Update goals and project memory**

State that visible employees are built in, external release infrastructure is
not required, the user completes Feishu's official one-click application
creation flow without tenant administrator approval, explicit limit zero
disables the feature, and process fallback is a logged security degradation.

- [x] **Step 5: Run full verification**

Run:

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/ -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check src/autonomous/
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/test_docs_references.py -q
uv --cache-dir /tmp/ghostap-uv-cache run python -m src.main --validate
git diff --check
```

Expected: all commands exit zero.

### Task 4: Official agent preset and employee-owned outbound

**Files:**
- Modify: `src/autonomous/provisioning/lark_app.py`
- Create: `src/autonomous/provisioning/lark_outbound.py`
- Modify: `src/autonomous/provisioning/channel_worker.py`
- Create: `tests/autonomous/unit/test_employee_lark_outbound.py`
- Modify: `tests/autonomous/unit/test_lark_app_registrar.py`
- Modify: `tests/autonomous/contract/test_employee_channel_contract.py`

- [x] **Step 1: Freeze the official one-click intelligent-agent preset**

Include the official Bot-to-Bot mention and document-comment permissions and
events, with `preset=True` and user `offline_access`.

- [x] **Step 2: Add employee-owned lark-oapi outbound transport**

Support exact text/card/post sends, replies, stable UUIDs, card patches, and
document-comment replies. Reject ambiguous payloads and expose only secret-free
failure types.

- [x] **Step 3: Wire production low-level worker sends**

Replace `outbound-transport-separate` with the current employee app's official
client and bind every success receipt to app/generation/connection authority.

- [x] **Step 4: Keep workflow admission explicit**

Do not treat enabled scopes as proof that automatic Bot-to-Bot handoff or
document-comment execution is complete. Those paths retain separate
membership, loop-control, document-authorization, ingress, and durable routing
requirements.
