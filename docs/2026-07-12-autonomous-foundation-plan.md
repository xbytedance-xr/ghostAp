# Autonomous Agent Department Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the single-source-of-truth employee domain, encrypted credential storage, Journal-backed employee projections, global registry facade, and legacy writer fencing required by every later Agent Department subsystem.

**Architecture:** Frozen employee and bot-principal aggregates live in `src/autonomous/domain`. Journal replay materializes employee state and non-secret identity files; a read facade exposes that state to Slock without making files authoritative. An AES-GCM vault stores Bot secrets outside identity and Journal, while an authority epoch prevents legacy and v5 writers from mutating the same identity concurrently.

**Tech Stack:** Python 3.13, dataclasses, existing authenticated Journal/Projection APIs, `cryptography==49.0.0` AES-GCM, pydantic-settings, pytest, uv, ruff.

## Global Constraints

- Use `uv`; never use pip or conda.
- Journal is the only fact source for employee lifecycle, bot principal, aliases, membership, and writer authority.
- `identity.json` contains `app_id + credential_ref`, never plaintext `app_secret`.
- Vault directories are mode `0700`; credential files and identity projections are mode `0600`.
- New employee IDs use `agt_<random>`; imported legacy IDs become durable aliases, never canonical IDs.
- `~/.ghostap/autonomy/` is the only v5 control-state root; `~/.ghostap/slock/agents/` contains employee projections.
- Domain dataclasses remain `frozen=True`; transitions use `dataclasses.replace()`.
- Do not modify Deep, Spec, Worktree, Workflow, main Bot WebSocket, or Slock `_run_acp_session`.
- Do not add compatibility defaults that make missing tenant, owner, authority epoch, Vault key, or credential binding silently succeed.
- Every production change starts with a failing regression test, gets a task-level spec/quality review, updates `.Memory`, and is committed independently.

---

## File Structure

### Files created in this plan

- `src/autonomous/workforce/__init__.py` — stable public workforce API.
- `src/autonomous/workforce/credential_vault.py` — encrypted credential receipts, resolve, rewrap, destroy, orphan scan.
- `src/autonomous/workforce/projection.py` — employee/bot/authority reducers and file materializer.
- `src/autonomous/workforce/registry.py` — read-only global employee registry facade and legacy `AgentIdentity` view.
- `src/autonomous/workforce/authority.py` — authority epoch model and mutation guard.
- `tests/autonomous/unit/test_employee_domain.py` — canonical employee serialization and invariant tests.
- `tests/autonomous/security/test_credential_vault.py` — encryption, permissions, redaction, rotation, crash receipts.
- `tests/autonomous/unit/test_employee_projection.py` — replay, name uniqueness, materialization, rebuild.
- `tests/autonomous/workforce_helpers.py` — real Journal/Projection fixtures shared by workforce tests.
- `tests/autonomous/integration/test_projected_agent_registry.py` — registry facade and Slock view contract.
- `tests/autonomous/integration/test_employee_authority_fencing.py` — legacy/v5 cutover fencing.

### Files modified in this plan

- `pyproject.toml`, `uv.lock` — pin `cryptography==49.0.0`.
- `src/config/settings.py` — Vault/key and employee projection settings.
- `src/autonomous/domain/enums.py` — full employee lifecycle and ID origin enums.
- `src/autonomous/domain/employees.py` — canonical frozen aggregates.
- `src/autonomous/domain/__init__.py`, `src/autonomous/models.py` — clean public re-exports; remove the quoted legacy-model tombstone.
- `src/autonomous/journal/projections.py` — include workforce projection state and reducer delegation.
- `src/slock_engine/agent_registry.py` — optional mutation guard checked before every write.
- `src/autonomous/migration/slock_importer.py` — durable random ID + legacy alias mapping.
- `.Memory/2026-07-12.md`, `.Memory/Abstract.md` — implementation evidence.

---

### Task 1: Canonical Frozen Employee Domain

**Files:**
- Modify: `src/autonomous/domain/enums.py`
- Modify: `src/autonomous/domain/employees.py`
- Modify: `src/autonomous/domain/__init__.py`
- Modify: `src/autonomous/models.py`
- Create: `tests/autonomous/unit/test_employee_domain.py`
- Modify: `tests/autonomous/unit/test_domain_serialization.py`

**Interfaces:**
- Produces: `EmployeeDefinition`, `BotPrincipal`, `EmployeeState`, `EmployeeIdOrigin`.
- `EmployeeDefinition.from_dict()` accepts legacy `employee_id` input but `to_dict()` emits canonical `agent_id` only.
- `EmployeeDefinition.employee_id` remains a read-only property returning `agent_id` while callers migrate; no serializer emits both fields.
- Later tasks consume `EmployeeDefinition.agent_id`, `.credential_ref` through `BotPrincipal`, `.member_groups`, `.aggregate_version`.

- [ ] **Step 1: Write failing canonical-domain tests**

```python
from dataclasses import FrozenInstanceError, replace

import pytest

from src.autonomous.domain import (
    BotPrincipal,
    EmployeeDefinition,
    EmployeeIdOrigin,
    EmployeeState,
    WorkerType,
)


def test_employee_round_trip_uses_canonical_agent_id_and_full_identity() -> None:
    employee = EmployeeDefinition(
        agent_id="agt_01",
        tenant_key="tenant_1",
        owner_principal_id="ou_admin",
        name="Atlas",
        emoji="🧭",
        tool="codex",
        model="gpt-5.6-sol",
        profile="standard",
        effort="high",
        role="coder",
        persona="Production backend engineer",
        personality_traits=("precise", "skeptical"),
        permissions=("file_read", "shell"),
        member_groups=("oc_team",),
        worker_type=WorkerType.VISIBLE,
        state=EmployeeState.READY_PENDING_VERIFICATION,
        id_origin=EmployeeIdOrigin.NATIVE,
        aggregate_version=3,
    )

    payload = employee.to_dict()

    assert payload["agent_id"] == "agt_01"
    assert "employee_id" not in payload
    assert EmployeeDefinition.from_dict(payload) == employee
    assert employee.employee_id == employee.agent_id


def test_legacy_employee_id_is_only_an_input_alias() -> None:
    restored = EmployeeDefinition.from_dict(
        {
            "employee_id": "agt_migrated",
            "tenant_key": "tenant_1",
            "owner_principal_id": "ou_admin",
            "name": "Legacy",
            "id_origin": "legacy_alias",
            "legacy_id_alias": "codex:default:Legacy",
        }
    )
    assert restored.agent_id == "agt_migrated"
    assert restored.to_dict()["legacy_id_alias"] == "codex:default:Legacy"


def test_employee_domain_is_frozen_and_transitions_use_replace() -> None:
    employee = EmployeeDefinition(agent_id="agt_1")
    with pytest.raises(FrozenInstanceError):
        employee.state = EmployeeState.ACTIVE  # type: ignore[misc]
    assert replace(employee, state=EmployeeState.ACTIVE).state is EmployeeState.ACTIVE


def test_visible_employee_requires_tenant_and_owner() -> None:
    with pytest.raises(ValueError, match="tenant_key"):
        EmployeeDefinition(name="Atlas", worker_type=WorkerType.VISIBLE)


def test_bot_principal_never_serializes_secret() -> None:
    principal = BotPrincipal(
        bot_principal_id="bot_1",
        tenant_key="tenant_1",
        agent_id="agt_1",
        app_id="cli_1",
        credential_ref="cred_1",
        desired_manifest_hash="sha256:desired",
        observed_manifest_hash="sha256:observed",
    )
    payload = principal.to_dict()
    assert payload["credential_ref"] == "cred_1"
    assert "app_secret" not in payload
    assert BotPrincipal.from_dict(payload) == principal
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run python -m pytest tests/autonomous/unit/test_employee_domain.py -q
```

Expected: collection or constructor failures because `agent_id`, lifecycle states, `EmployeeIdOrigin`, and full identity fields do not exist.

- [ ] **Step 3: Implement the canonical aggregates**

Implement these exact enum values:

```python
class EmployeeIdOrigin(str, Enum):
    NATIVE = "native"
    LEGACY_ALIAS = "legacy_alias"


class EmployeeState(str, Enum):
    DRAFT = "draft"
    PROVISIONING_APP = "provisioning_app"
    STORING_CREDENTIAL = "storing_credential"
    CONFIGURING = "configuring"
    VALIDATING = "validating"
    READY_PENDING_VERIFICATION = "ready_pending_verification"
    ACTIVE = "active"
    RETIRING = "retiring"
    ACTION_REQUIRED = "action_required"
    ARCHIVED = "archived"
```

Replace `EmployeeDefinition` with this public shape, preserving the existing freeze/thaw helpers for mapping fields:

```python
@dataclass(frozen=True)
class EmployeeDefinition:
    agent_id: str = field(default_factory=lambda: new_id("agt"))
    tenant_key: str = ""
    owner_principal_id: str = ""
    name: str = ""
    emoji: str = "🤖"
    tool: str = ""
    model: str = ""
    profile: str = "standard"
    effort: str = "default"
    role: str = ""
    persona: str = ""
    personality_traits: tuple[str, ...] = ()
    worker_type: WorkerType = WorkerType.LOGICAL
    state: EmployeeState = EmployeeState.DRAFT
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    budget_template: Any = field(default_factory=dict)
    bot_principal_id: str | None = None
    member_groups: tuple[str, ...] = ()
    id_origin: EmployeeIdOrigin = EmployeeIdOrigin.NATIVE
    legacy_id_alias: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    aggregate_version: int = 0

    @property
    def employee_id(self) -> str:
        return self.agent_id
```

`__post_init__()` rejects a VISIBLE employee without both `tenant_key` and
`owner_principal_id`. LOGICAL legacy workers may retain empty tenancy until the
importer binds them; they never pass VISIBLE readiness attestation.

Extend `BotPrincipal` with `agent_id`, desired/observed manifest hashes, and aggregate version. Do not add an app-secret field. Make `from_dict()` require either `agent_id` or a migration-supplied legacy binding; it must not invent a tenant or owner.

Reduce `src/autonomous/models.py` to the current six-line domain re-export module and delete the triple-quoted legacy source body.

- [ ] **Step 4: Run domain and serialization tests**

Run:

```bash
uv run python -m pytest \
  tests/autonomous/unit/test_employee_domain.py \
  tests/autonomous/unit/test_domain_serialization.py -q
```

Expected: all pass.

- [ ] **Step 5: Run the autonomous domain regression set**

Run:

```bash
uv run python -m pytest \
  tests/autonomous/unit/test_state_machines.py \
  tests/autonomous/unit/test_policy_engine.py \
  tests/autonomous/unit/test_supervisor.py -q
```

Expected: all pass; update call sites to use the read-only `employee_id` alias only where the surrounding domain still names worker assignment fields `employee_id`.

- [ ] **Step 6: Commit the domain task**

```bash
git add src/autonomous/domain/enums.py src/autonomous/domain/employees.py \
  src/autonomous/domain/__init__.py src/autonomous/models.py \
  tests/autonomous/unit/test_employee_domain.py \
  tests/autonomous/unit/test_domain_serialization.py
git commit -m "refactor(autonomous): unify employee domain identity"
```

---

### Task 2: AES-GCM Credential Vault and Key Rotation

**Files:**
- Modify: `pyproject.toml`
- Modify mechanically: `uv.lock`
- Modify: `src/config/settings.py`
- Create: `src/autonomous/workforce/__init__.py`
- Create: `src/autonomous/workforce/credential_vault.py`
- Create: `tests/autonomous/security/test_credential_vault.py`
- Modify: `tests/autonomous/contract/test_config_and_gate_contract.py`

**Interfaces:**
- Produces: `CredentialKeyring.from_settings(settings)`, `CredentialVault.put()`, `.resolve()`, `.rewrap()`, `.destroy()`, `.find_orphan_receipts()`.
- `CredentialVault.put(agent_id, app_id, app_secret, hire_intent_id, attempt_id) -> CredentialReceipt`.
- `CredentialVault.resolve(ref, agent_id, app_id) -> str` validates AES-GCM associated data.
- Later Hire/Channel/Slash tasks consume only `credential_ref` and call the Vault; they never inspect envelope JSON.

- [ ] **Step 1: Pin the encryption dependency and add fail-closed settings tests**

Add to the main dependency list:

```toml
"cryptography==49.0.0",
```

Add settings:

```python
from pydantic import SecretStr

autonomous_credential_dir: str = "~/.ghostap/slock/credentials"
autonomous_credential_keys: SecretStr = SecretStr("")
autonomous_credential_active_key_id: str = ""
```

`autonomous_credential_keys` is versioned JSON inside `SecretStr`, for example
`{"version":1,"keys":{"k1":"<base64-32-bytes>"}}`. Parsing rejects unknown
top-level keys, unsupported versions, duplicate key IDs, invalid base64,
decoded keys other than exactly 32 bytes, and an active key ID absent from the
mapping.

Add this exact settings contract test (using the existing settings factory in
the file rather than constructing unrelated environment state):

```python
import base64
import json


def test_employee_credential_settings_default_fail_closed_and_redact() -> None:
    empty = Settings()
    assert empty.autonomous_credential_keys.get_secret_value() == ""
    assert empty.autonomous_credential_active_key_id == ""

    encoded = base64.urlsafe_b64encode(bytes([7]) * 32).decode()
    keyring_json = json.dumps({"version": 1, "keys": {"k1": encoded}})
    configured = Settings(
        autonomous_credential_keys=keyring_json,
        autonomous_credential_active_key_id="k1",
    )
    assert keyring_json not in repr(configured)
```

The first production composition task that enables VISIBLE employees must call
`CredentialKeyring.from_settings()`; that constructor raises
`CredentialVaultConfigurationError` when either setting is empty. Foundation
does not change the existing visible-employee limit and therefore does not add
a misleading partial readiness attestation here.

- [ ] **Step 2: Run settings tests and verify RED**

```bash
uv sync --group dev
uv run python -m pytest \
  tests/autonomous/contract/test_config_and_gate_contract.py -q
```

Expected: fail because the dependency and settings do not exist.

- [ ] **Step 3: Write failing Vault tests**

```python
import base64
import json
import os
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from src.autonomous.workforce.credential_vault import (
    CredentialKeyring,
    CredentialVault,
    CredentialVaultConfigurationError,
    CredentialVaultError,
)


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode()


def test_keyring_parses_versioned_rotation_set_and_rejects_missing_active() -> None:
    settings = SimpleNamespace(
        autonomous_credential_keys=SecretStr(
            json.dumps({"version": 1, "keys": {"old": _key(1), "new": _key(2)}})
        ),
        autonomous_credential_active_key_id="new",
    )
    assert CredentialKeyring.from_settings(settings).active_key_id == "new"
    settings.autonomous_credential_active_key_id = "absent"
    with pytest.raises(CredentialVaultConfigurationError):
        CredentialKeyring.from_settings(settings)


def test_vault_encrypts_secret_and_enforces_modes(tmp_path) -> None:
    vault = CredentialVault(
        tmp_path / "credentials",
        CredentialKeyring(keys={"k1": _key(1)}, active_key_id="k1"),
    )
    receipt = vault.put(
        agent_id="agt_1",
        app_id="cli_1",
        app_secret="super-secret",
        hire_intent_id="hire_1",
        attempt_id="attempt_1",
    )
    raw = receipt.path.read_bytes()
    assert b"super-secret" not in raw
    assert os.stat(receipt.path).st_mode & 0o777 == 0o600
    assert os.stat(receipt.path.parent).st_mode & 0o777 == 0o700
    assert vault.resolve(receipt.credential_ref, agent_id="agt_1", app_id="cli_1") == "super-secret"


def test_vault_rejects_wrong_associated_identity(tmp_path) -> None:
    vault = CredentialVault(
        tmp_path / "credentials",
        CredentialKeyring(keys={"k1": _key(1)}, active_key_id="k1"),
    )
    receipt = vault.put(
        agent_id="agt_1", app_id="cli_1", app_secret="secret",
        hire_intent_id="hire_1", attempt_id="attempt_1",
    )
    with pytest.raises(CredentialVaultError):
        vault.resolve(receipt.credential_ref, agent_id="agt_2", app_id="cli_1")


def test_vault_finds_orphan_receipt_and_rewraps_to_active_key(tmp_path) -> None:
    root = tmp_path / "credentials"
    old = CredentialVault(root, CredentialKeyring(keys={"old": _key(1)}, active_key_id="old"))
    receipt = old.put(
        agent_id="agt_1", app_id="cli_1", app_secret="secret",
        hire_intent_id="hire_1", attempt_id="attempt_1",
    )
    rotated = CredentialVault(
        root,
        CredentialKeyring(keys={"old": _key(1), "new": _key(2)}, active_key_id="new"),
    )
    assert [r.credential_ref for r in rotated.find_orphan_receipts(set())] == [receipt.credential_ref]
    rotated.rewrap(receipt.credential_ref, agent_id="agt_1", app_id="cli_1")
    envelope = json.loads(receipt.path.read_text())
    assert envelope["key_id"] == "new"
    assert rotated.resolve(receipt.credential_ref, agent_id="agt_1", app_id="cli_1") == "secret"


def test_destroy_is_idempotent_and_removes_secret(tmp_path) -> None:
    vault = CredentialVault(
        tmp_path / "credentials",
        CredentialKeyring(keys={"k1": _key(1)}, active_key_id="k1"),
    )
    receipt = vault.put(
        agent_id="agt_1", app_id="cli_1", app_secret="secret",
        hire_intent_id="hire_1", attempt_id="attempt_1",
    )
    assert vault.destroy(receipt.credential_ref) is True
    assert vault.destroy(receipt.credential_ref) is False
    assert not receipt.path.exists()
```

- [ ] **Step 4: Run Vault tests and verify RED**

```bash
uv run python -m pytest tests/autonomous/security/test_credential_vault.py -q
```

Expected: import failure because the workforce Vault does not exist.

- [ ] **Step 5: Implement Vault envelopes**

Use this immutable receipt API:

```python
@dataclass(frozen=True)
class CredentialReceipt:
    credential_ref: str
    key_id: str
    agent_id: str
    app_id: str
    hire_intent_id: str
    attempt_id: str
    ciphertext_sha256: str
    path: Path
```

Envelope keys are exactly `schema_version`, `credential_ref`, `key_id`, `agent_id`, `app_id`, `hire_intent_id`, `attempt_id`, `nonce`, `ciphertext`, `ciphertext_sha256`, `created_at`. Derive `credential_ref` as `cred_` plus SHA-256 of `hire_intent_id|attempt_id`, never from the secret. Use canonical JSON of the non-secret identity fields as AES-GCM associated data.

All writes use a same-directory temporary file opened with mode `0600`, `flush()`, `os.fsync()`, `os.replace()`, then directory fsync. `destroy()` unlinks and directory-fsyncs. Error messages contain the credential ref and error class only.

- [ ] **Step 6: Run Vault, config, and secret scans**

```bash
uv run python -m pytest \
  tests/autonomous/security/test_credential_vault.py \
  tests/autonomous/contract/test_config_and_gate_contract.py -q
uv run ruff check src/autonomous/workforce/credential_vault.py src/config/settings.py \
  tests/autonomous/security/test_credential_vault.py
```

Expected: all pass and ruff reports no errors.

- [ ] **Step 7: Commit the Vault task**

```bash
git add pyproject.toml uv.lock src/config/settings.py \
  src/autonomous/workforce/__init__.py \
  src/autonomous/workforce/credential_vault.py \
  tests/autonomous/security/test_credential_vault.py \
  tests/autonomous/contract/test_config_and_gate_contract.py
git commit -m "feat(autonomous): add encrypted employee credential vault"
```

---

### Task 3: Journal Employee Projection and Identity Materializer

**Files:**
- Modify: `src/autonomous/journal/projections.py`
- Create: `src/autonomous/workforce/projection.py`
- Create: `tests/autonomous/workforce_helpers.py`
- Create: `tests/autonomous/unit/test_employee_projection.py`

**Interfaces:**
- Produces: `WorkforceProjectionState`, `apply_workforce_event()`,
  `validate_workforce_events()`, `commit_workforce_events()`,
  `EmployeeIdentityMaterializer.materialize_all()`.
- Adds `ProjectionState.employees`, `.bot_principals`, `.employee_name_keys`,
  `.legacy_agent_aliases`, `.legacy_source_hashes`, `.authority_epoch`.
- Materializer consumes ProjectionState and creates non-secret `identity.json`; no caller writes identity directly.

- [ ] **Step 1: Write failing replay tests**

First create `tests/autonomous/workforce_helpers.py` with real Journal helpers:

```python
from src.autonomous.journal import JournalWriter, MemoryAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.projections import ProjectionRepository, ProjectionState

HMAC_KEY = b"test-workforce-key-at-least-32-bytes!"


def make_writer(tmp_path):
    return JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )


def commit_events(writer, state: ProjectionState, *events: JournalEvent):
    from src.autonomous.workforce.projection import commit_workforce_events

    return commit_workforce_events(writer, state, events)


def employee_created(agent_id: str = "agt_1", name: str = "Atlas") -> JournalEvent:
    return JournalEvent(
        event_type="employee.created",
        aggregate_id=agent_id,
        payload={
            "agent_id": agent_id,
            "tenant_key": "tenant_1",
            "owner_principal_id": "ou_admin",
            "name": name,
            "tool": "codex",
            "model": "gpt-5.6-sol",
            "worker_type": "visible",
            "state": "draft",
            "member_groups": ["oc_team"],
        },
    )


def seed_workforce_state(tmp_path) -> tuple[JournalWriter, ProjectionState]:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.bot_principal_bound",
            aggregate_id="agt_1",
            payload={"agent_id": "agt_1", "bot_principal_id": "bot_1"},
        ),
        JournalEvent(
            event_type="bot_principal.bound",
            aggregate_id="bot_1",
            payload={
                "bot_principal_id": "bot_1",
                "tenant_key": "tenant_1",
                "agent_id": "agt_1",
                "app_id": "cli_1",
                "credential_ref": "cred_1",
                "scopes": [],
            },
        ),
    )
    return writer, state


def replay_state(writer) -> ProjectionState:
    return ProjectionRepository().rebuild(writer.replay())
```

Then use those helpers in `test_employee_projection.py`:

```python
import json
import os

import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.projections import ProjectionError, ProjectionState, apply_event
from src.autonomous.workforce.projection import EmployeeIdentityMaterializer
from tests.autonomous.workforce_helpers import (
    commit_events,
    employee_created,
    make_writer,
    seed_workforce_state,
)


def test_employee_events_replay_into_projection(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(
        writer,
        state,
        employee_created(),
        JournalEvent(
            event_type="employee.bot_principal_bound",
            aggregate_id="agt_1",
            payload={"agent_id": "agt_1", "bot_principal_id": "bot_1"},
        ),
        JournalEvent(
            event_type="bot_principal.bound",
            aggregate_id="bot_1",
            payload={
                "bot_principal_id": "bot_1", "tenant_key": "tenant_1",
                "agent_id": "agt_1", "app_id": "cli_1",
                "credential_ref": "cred_1", "scopes": [],
            },
        ),
    )
    assert state.employees["agt_1"].bot_principal_id == "bot_1"
    assert state.bot_principals["bot_1"].credential_ref == "cred_1"


def test_duplicate_active_name_is_rejected_casefolded() -> None:
    state = ProjectionState()
    apply_event(state, employee_created("agt_1", "Atlas"))
    with pytest.raises(ProjectionError, match="duplicate active employee name"):
        apply_event(state, employee_created("agt_2", "ATLAS"))


def test_rejected_employee_command_does_not_advance_journal(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created("agt_1", "Atlas"))
    sequence = writer.get_last_frame().sequence
    with pytest.raises(ProjectionError, match="duplicate active employee name"):
        commit_events(writer, state, employee_created("agt_2", "ATLAS"))
    assert writer.get_last_frame().sequence == sequence


def test_materializer_writes_non_secret_identity_with_mode_0600(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    materializer = EmployeeIdentityMaterializer(tmp_path / "agents")
    path = materializer.materialize(state, "agt_1")
    payload = json.loads(path.read_text())
    assert payload["app_id"] == "cli_1"
    assert payload["credential_ref"] == "cred_1"
    assert "app_secret" not in payload
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_missing_projection_file_is_rebuilt_from_state(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    materializer = EmployeeIdentityMaterializer(tmp_path / "agents")
    first = materializer.materialize(state, "agt_1")
    first.unlink()
    rebuilt = materializer.materialize(state, "agt_1")
    assert rebuilt.exists()
```

- [ ] **Step 2: Run projection tests and verify RED**

```bash
uv run python -m pytest tests/autonomous/unit/test_employee_projection.py -q
```

Expected: missing projection fields and workforce reducer/materializer imports.

- [ ] **Step 3: Implement pure workforce reducers**

Handle these event types with frozen replacements and aggregate-version increments:

```text
employee.created
employee.state_changed
employee.profile_changed
employee.membership_changed
employee.legacy_alias_bound
bot_principal.bound
employee.bot_principal_bound
bot_principal.manifest_observed
credential.destroyed
authority.cutover
```

`employee.created` reserves `(tenant_key, name.casefold())`. ARCHIVED employees keep a tombstone name record until an explicit later retention policy event; no reducer silently frees it. `bot_principal.bound` verifies the employee exists and that app ID is not bound to another non-archived employee.

`validate_workforce_events(state, events)` applies the proposed events to a
deep-copied projection and returns normally only when the whole transaction is
valid. `commit_workforce_events(writer, state, events)` performs that validation
and the authenticated commit while holding the workforce single-writer lock,
then applies the anchored frame to the live state. Every workforce command path
introduced in this and later plans uses this function rather than calling
`JournalWriter.commit()` directly. Reducer rejection is retained as a replay
integrity guard, but no production command appends an event and only then
discovers its semantic invalidity.

Binding a Bot is always one transaction containing
`employee.bot_principal_bound` on the employee aggregate and
`bot_principal.bound` on the principal aggregate. Expected-version fencing must
therefore cover both aggregate IDs; the principal event alone never mutates the
employee projection.

- [ ] **Step 4: Implement atomic identity materialization**

`EmployeeIdentityMaterializer.materialize(state, agent_id)` merges the employee and bound principal into one non-secret JSON projection, writes mode `0600`, fsyncs the file, atomically renames, and fsyncs the parent directory. `materialize_all()` sorts agent IDs for deterministic rebuilds.

- [ ] **Step 5: Run projection and Journal regressions**

```bash
uv run python -m pytest \
  tests/autonomous/unit/test_employee_projection.py \
  tests/autonomous/unit/test_projections.py \
  tests/autonomous/unit/test_journal_writer.py \
  tests/autonomous/chaos/test_journal_blob_crash_boundaries.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit the projection task**

```bash
git add src/autonomous/journal/projections.py \
  src/autonomous/workforce/projection.py \
  tests/autonomous/workforce_helpers.py \
  tests/autonomous/unit/test_employee_projection.py
git commit -m "feat(autonomous): project durable employee identities"
```

---

### Task 4: Global Projected Registry and Writer Authority Fencing

**Files:**
- Create: `src/autonomous/workforce/registry.py`
- Create: `src/autonomous/workforce/authority.py`
- Modify: `src/autonomous/workforce/__init__.py`
- Modify: `src/slock_engine/agent_registry.py`
- Modify: `src/slock_engine/engine.py`
- Modify: `src/feishu/handlers/slock.py`
- Modify: `src/autonomous/migration/slock_importer.py`
- Create: `tests/autonomous/integration/test_projected_agent_registry.py`
- Create: `tests/autonomous/integration/test_employee_authority_fencing.py`
- Modify: `tests/autonomous/integration/test_slock_importer.py`

**Interfaces:**
- Produces: `ProjectedAgentRegistry.get/find_by_name/list_agents/as_slock_identity`.
- Produces: `AuthoritySnapshot(epoch: int, mode: AuthorityMode, cutover_sequence: int)` and `LegacyMutationGuard.assert_writable(operation)`.
- Existing `AgentRegistry` requires an explicit `mutation_guard`;
  `AgentRegistry.legacy()` is the explicit compatibility constructor while
  production v5 composition injects its Journal-backed guard.
- Later production composition injects `ProjectedAgentRegistry`; no later component parses identity files.

- [ ] **Step 1: Write failing registry facade tests**

```python
from src.autonomous.workforce.registry import ProjectedAgentRegistry
from tests.autonomous.workforce_helpers import seed_workforce_state


def test_projected_registry_is_global_and_returns_slock_view(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)
    employee = registry.get("tenant_1", "agt_1")
    slock_view = registry.as_slock_identity("tenant_1", "agt_1")
    assert employee.agent_id == "agt_1"
    assert slock_view.agent_id == "agt_1"
    assert slock_view.name == employee.name
    assert slock_view.agent_type == employee.tool
    assert slock_view.model_name == employee.model


def test_projected_registry_filters_membership_without_changing_global_identity(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)
    assert [e.agent_id for e in registry.list_agents("tenant_1", "oc_team")] == ["agt_1"]
    assert registry.get("tenant_1", "agt_1") is not None


def test_projected_registry_has_no_remove_or_register_mutation(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)
    assert not hasattr(registry, "register")
    assert not hasattr(registry, "remove")
```

- [ ] **Step 2: Write failing authority and importer tests**

```python
import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.migration.slock_importer import LegacyEntity, SlockImporter
from src.autonomous.workforce.authority import (
    AuthorityMode,
    AuthoritySnapshot,
    LegacyMutationGuard,
    StaleAuthorityEpoch,
)
from src.slock_engine.agent_registry import AgentRegistry
from src.slock_engine.models import AgentIdentity
from tests.autonomous.workforce_helpers import make_writer, replay_state


def test_v5_cutover_rejects_legacy_registry_mutation(tmp_path) -> None:
    snapshot = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE, cutover_sequence=91)
    guard = LegacyMutationGuard(lambda: snapshot, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    with pytest.raises(StaleAuthorityEpoch):
        registry.register(AgentIdentity(agent_id="legacy_1", name="Legacy"))


def test_queued_legacy_write_is_rechecked_after_cutover(tmp_path) -> None:
    current = AuthoritySnapshot(
        epoch=1, mode=AuthorityMode.LEGACY_WRITE, cutover_sequence=0
    )
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    request = registry._make_persist_request(
        AgentIdentity(agent_id="legacy_1", name="Legacy")
    )
    current = AuthoritySnapshot(
        epoch=2, mode=AuthorityMode.V5_WRITE, cutover_sequence=91
    )
    with pytest.raises(StaleAuthorityEpoch):
        registry._persist_request(request)
    assert not (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_importer_generates_random_id_and_durable_alias(tmp_path) -> None:
    writer = make_writer(tmp_path)
    legacy = LegacyEntity(
        entity_type="agent",
        legacy_id="legacy_1",
        data={
            "name": "Legacy", "agent_type": "codex", "model_name": "gpt-5.6-sol",
            "owner_group": "oc_team", "tenant_key": "tenant_1",
            "owner_principal_id": "ou_admin",
        },
    )
    importer = SlockImporter(writer=writer, state=replay_state(writer))
    first = importer.import_agent(legacy)
    replayed = replay_state(writer)
    second = SlockImporter(writer=writer, state=replayed).import_agent(legacy)
    assert first.agent_id.startswith("agt_")
    assert first.agent_id != legacy.legacy_id
    assert replayed.legacy_agent_aliases[legacy.legacy_id] == first.agent_id
    assert second.agent_id == first.agent_id
```

- [ ] **Step 3: Run registry/fencing tests and verify RED**

```bash
uv run python -m pytest \
  tests/autonomous/integration/test_projected_agent_registry.py \
  tests/autonomous/integration/test_employee_authority_fencing.py \
  tests/autonomous/integration/test_slock_importer.py -q
```

Expected: missing workforce registry/authority APIs and old importer ID behavior.

- [ ] **Step 4: Implement the registry facade**

The facade reads only `ProjectionState`. `as_slock_identity()` creates a fresh legacy `AgentIdentity` value with `agent_id`, name, emoji, tool, model, persona/system prompt, role, permissions, member groups, and projected memory/workspace paths. It must not expose app ID or credential ref to Slock/ACP objects.

Lookups are tenant-aware. `find_by_name()` requires tenant and optional chat; ambiguous cross-tenant names raise a typed error instead of returning first match.

- [ ] **Step 5: Implement authority fencing**

```python
class AuthorityMode(str, Enum):
    LEGACY_WRITE = "legacy_write"
    SHADOW_READ = "shadow_read"
    V5_WRITE = "v5_write"
    V5_ONLY = "v5_only"


@dataclass(frozen=True)
class AuthoritySnapshot:
    epoch: int
    mode: AuthorityMode
    cutover_sequence: int = 0
```

`LegacyMutationGuard.assert_writable()` accepts only matching epoch in
LEGACY_WRITE or SHADOW_READ and returns the validated epoch. Inject it into
every legacy mutation entry: `register`, `update`, `remove`, and `move_agent`,
immediately before memory mutation. Queue entries are `(operation, agent,
validated_epoch)`; the persistence worker calls `assert_writable()` again with
that stamped epoch immediately before every filesystem write. A cutover waits
for the legacy registry lock, advances authority, and causes all old queued
entries to be discarded with a typed stale-epoch error before disk mutation.

The constructor requires a guard. `AgentRegistry.legacy(base_path=...)`
explicitly constructs a local `LEGACY_WRITE/epoch=0` authority for deployments
where v5 composition is disabled. Update the two production legacy call sites
and existing registry tests to use this named constructor; `_make_persist_request`
and `_persist_request` are small separately testable internal boundaries used by
the queue worker. There is no optional writable default hidden in `__init__`.

- [ ] **Step 6: Make importer mapping durable**

Replace the in-memory employee import path with an explicit synchronous API
over the authenticated Journal writer, while retaining a separately named
legacy event sink for old non-employee migration tests:

```python
class SlockImporter:
    def __init__(
        self,
        journal: LegacyEventJournal | None = None,
        *,
        writer: JournalWriter | None = None,
        state: ProjectionState | None = None,
    ) -> None: ...
    def import_agent(self, entity: LegacyEntity) -> EmployeeDefinition: ...
```

Construction validates exactly one mode: either `journal` for the old async
generic migration surface, or both `writer` and `state` for durable employee
imports. Supplying neither, both modes, or only one member of the authenticated
pair raises `ValueError`; there is no implicit in-memory employee authority.

`scan()` normalizes legacy agent records to `entity_type == "agent"` (the old
`"worker"` spelling is accepted as an input alias and canonicalized before
dispatch). `import_agent()` accepts only the canonical type, computes a stable source
hash, checks `ProjectionState.legacy_agent_aliases` and
`ProjectionState.legacy_source_hashes`, calls
`validate_workforce_events()`, and commits
`employee.created + employee.legacy_alias_bound` in one Journal frame. It then
applies that frame to its local state and returns the projected employee.
Replaying into a new importer returns the same random canonical ID and never
relies on an in-memory mapping dict. Keep the existing async bulk scan/plan/apply
surface as a compatibility adapter: non-agent entities retain their current
migration event path, while agent entities delegate to `import_agent()`.

- [ ] **Step 7: Run focused and Slock registry regressions**

```bash
uv run python -m pytest \
  tests/autonomous/integration/test_projected_agent_registry.py \
  tests/autonomous/integration/test_employee_authority_fencing.py \
  tests/autonomous/integration/test_slock_importer.py \
  tests/test_slock_agent_registry.py \
  tests/test_slock_role_creation.py -q
uv run ruff check src/autonomous/workforce src/autonomous/migration/slock_importer.py \
  src/slock_engine/agent_registry.py tests/autonomous/integration
git diff --check
```

Expected: all pass, ruff clean, no whitespace errors.

- [ ] **Step 8: Update project memory and commit**

Add a detailed Foundation entry to `.Memory/2026-07-12.md` and one summary line to `.Memory/Abstract.md`, including RED evidence, test counts, migration risk, and the fact that VISIBLE employee remains disabled.

```bash
git add src/autonomous/workforce src/autonomous/journal/projections.py \
  src/autonomous/migration/slock_importer.py src/slock_engine/agent_registry.py \
  src/slock_engine/engine.py src/feishu/handlers/slock.py \
  tests/autonomous/integration/test_projected_agent_registry.py \
  tests/autonomous/integration/test_employee_authority_fencing.py \
  tests/autonomous/integration/test_slock_importer.py \
  .Memory/2026-07-12.md .Memory/Abstract.md
git commit -m "feat(autonomous): fence employee identity authority"
```

---

## Foundation Completion Gate

Run fresh verification before starting the data/context plan:

```bash
uv run python -m pytest tests/autonomous/ -q
uv run python -m pytest tests/test_slock_agent_registry.py tests/test_slock_role_creation.py -q
uv run ruff check src/autonomous/ src/slock_engine/agent_registry.py tests/autonomous/
uv run python -m src.main --validate
git diff --check
git status --short
```

Required evidence:

- all commands exit 0;
- fresh Journal replay rebuilds employees, Bot principals, aliases, authority, and identity files;
- no serialized file, test output, or exception contains an app secret;
- stale legacy epoch writes fail before memory or disk mutation;
- existing Slock registry and `/hire` selection regressions remain green;
- `autonomous_visible_employee_limit` remains 0 because Provisioning and Channel plans are not yet implemented.

After this gate, write and execute `docs/2026-07-12-autonomous-data-context-plan.md`; do not start Provisioning or Channel work directly from the high-level design.
