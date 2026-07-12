# Task 2 Report: AES-GCM Credential Vault and Key Rotation

## Status

Implemented and focused/autonomous verification complete. Task 3/4 production
composition and authority work were not implemented. The existing
`autonomous_visible_employee_limit=0` gate is unchanged.

## Delivered

- Pinned `cryptography==49.0.0` in `pyproject.toml` and mechanically updated
  `uv.lock` with `uv add`.
- Added fail-closed `SecretStr` keyring settings, active key ID, and credential
  directory default.
- Added strict `CredentialKeyring.from_settings()` parsing for exactly
  `{version: 1, keys: {...}}`, including duplicate-key detection before normal
  JSON object materialization.
- Added immutable `CredentialReceipt` and AES-256-GCM `CredentialVault` APIs:
  `put`, `resolve`, `rewrap`, `destroy`, and `find_orphan_receipts`.
- Bound ciphertext to canonical non-secret employee/application identity AAD.
- Added exact-schema envelopes, deterministic non-secret refs, safe redacted
  exceptions, path-traversal rejection, ciphertext integrity checks, and
  rotation/orphan behavior.
- Enforced `0700` credential directories and same-directory `0600` atomic
  writes with file fsync, replace, directory fsync; deletion directory-fsyncs.

## TDD Evidence

### Baseline

Command:

```text
uv sync --group dev
uv run python -m pytest tests/autonomous/contract/test_config_and_gate_contract.py -q
```

Result: `10 passed in 0.79s`.

### RED 1: dependency and settings contract

Command:

```text
uv run python -m pytest tests/autonomous/contract/test_config_and_gate_contract.py -q
```

Result: `2 failed, 9 passed in 0.94s`.

- `cryptography==49.0.0` absent from main dependencies.
- `Settings.autonomous_credential_keys` absent.

### RED 2: Vault API

Command:

```text
uv run python -m pytest tests/autonomous/security/test_credential_vault.py -q
```

Result: collection error with
`ModuleNotFoundError: No module named 'src.autonomous.workforce'`.

### RED 3: credential directory contract

Command:

```text
uv run python -m pytest tests/autonomous/contract/test_config_and_gate_contract.py::test_employee_credential_settings_default_fail_closed_and_redact -q
```

Result: `1 failed in 0.79s`; the required
`autonomous_credential_dir` setting was absent.

### GREEN

Commands and results:

```text
uv run python -m pytest tests/autonomous/contract/test_config_and_gate_contract.py::test_employee_credential_settings_default_fail_closed_and_redact -q
# 1 passed in 0.72s

uv run python -m pytest tests/autonomous/security/test_credential_vault.py tests/autonomous/contract/test_config_and_gate_contract.py -q
# 26 passed in 0.91s

uv run ruff check src/autonomous/workforce/credential_vault.py src/config/settings.py tests/autonomous/security/test_credential_vault.py tests/autonomous/contract/test_config_and_gate_contract.py
# All checks passed!

uv run python -m pytest tests/autonomous/ -q
# 603 passed in 60.43s
```

`git diff --check` also produced no output.

## Self-review

- Keyring JSON rejects unknown top-level fields, unsupported or non-integer
  versions, duplicate IDs, invalid Base64, non-32-byte keys, empty key maps,
  empty settings, and absent active IDs.
- Envelope readers require exactly the 11 approved keys, schema v1, canonical
  deterministic ref consistency, string field types, and ciphertext digest.
- AES-GCM AAD includes credential ref, agent ID, app ID, hire intent ID, and
  attempt ID. Resolving under another employee/app identity fails closed.
- Public credential operations redact underlying crypto, JSON, filesystem,
  secret, and key failures to `CredentialVaultError:<credential_ref>`.
- Secret literals used by tests do not appear in production source.
- The existing VISIBLE production limit remains unchanged.

## Expanded-suite concern

An optional full repository run was stopped at 50% after
`5347 passed, 32 failed in 219.05s`. The failures are outside the Task 2 diff:

- unchanged lock-card/signing tests fail because the environment has an empty
  app signing key;
- unchanged `src/feishu/handlers/system.py` lines 637/653 violate the existing
  bare-fstring error guard.

The focused Task 2 suite and complete `tests/autonomous/` regression suite are
green. No unrelated fixes were made.

## Commit

SHA: `f12a5c2`

Subject: `feat(autonomous): add encrypted employee credential vault`

## Security Review Fix

### Findings verified

The review findings reproduced in the committed implementation:

- `self._root` pathname operations followed root and leaf symlinks and could
  be redirected after construction;
- already-absent `destroy()` returned before directory fsync;
- the dataclass repr exposed decoded AES key bytes;
- orphan receipt parsing accepted bool/float schema versions and malformed
  nonce, ciphertext, digest, timestamp, and empty identity/key fields.

### RED evidence

After adding the security regressions and before changing production code:

```text
uv run python -m pytest tests/autonomous/security/test_credential_vault.py -q
# 19 failed, 17 passed in 1.13s
```

The failures covered decoded-key repr disclosure, missing held root FD and
cleanup API, absent-delete fsync omission, root/leaf symlink following,
root-swap redirection, weak schema typing, and malformed envelope fields.
The already-enforced deterministic-ref checks continued to reject empty
hire/attempt identities.

The first implementation run exposed an import error because
`TracebackType` was imported from `typing`; it was corrected to the standard
`types.TracebackType` before GREEN.

### Fix

- Root paths are now walked/created component-by-component with `dir_fd`,
  `O_DIRECTORY`, `O_NOFOLLOW` where available, and descriptor type checks.
  Only the verified final directory receives `fchmod(0700)`.
- The held final directory FD is the capability used by every envelope
  list/open/create/replace/unlink/fsync operation. Leaf reads use
  `O_NOFOLLOW`, require a regular file, and require exact mode `0600`.
- Root replacement after construction cannot redirect Vault operations.
- `close()` and context-manager cleanup explicitly release the held FD;
  an idempotent finalizer covers forgotten cleanup without retaining keys or
  secrets in its callback.
- Both successful and already-absent destruction paths directory-fsync.
- `CredentialKeyring.keys` is excluded from dataclass repr.
- Envelope parsing validates strict integer schema v1, nonempty identity/key
  fields, 12-byte Base64 nonce, nonempty valid Base64 ciphertext with matching
  lowercase SHA-256, and a parseable UTC creation timestamp before producing
  orphan receipts.

### GREEN evidence

```text
uv run python -m pytest tests/autonomous/security/test_credential_vault.py -q
# 36 passed in 0.99s

uv run python -m pytest tests/autonomous/security/test_credential_vault.py tests/autonomous/contract/test_config_and_gate_contract.py -q
# 47 passed in 1.11s

uv run python -m pytest tests/autonomous/ -q
# 624 passed in 60.39s

uv run ruff check src/autonomous/workforce/credential_vault.py tests/autonomous/security/test_credential_vault.py
# All checks passed!
```

`git diff --check` produced no output. A source scan confirms no envelope
operation re-resolves `self._root`; the retained `CredentialReceipt.path` is
informational and is never trusted by Vault I/O.

### Security-fix commit

SHA: `43a3827`

Subject: `fix(autonomous): secure credential vault I/O`

## Producer/Parser Consistency Review Fix

### Finding reproduced

`CredentialVault.put()` accepted empty and non-string employee, application,
Hire, Attempt, and secret values even though `_read_envelope()` rejects the
resulting envelopes. A producer could therefore report a successful durable
write that neither `resolve()` nor orphan recovery could consume.

### RED evidence

After adding parameterized producer validation and immediate round-trip tests,
before changing production code:

```text
uv run python -m pytest tests/autonomous/security/test_credential_vault.py -q
# 15 failed, 37 passed in 1.25s
```

The 15 failures covered empty, `None`, and integer values for `agent_id`,
`app_id`, `hire_intent_id`, `attempt_id`, and `app_secret`. Empty/string-like
identity values were persisted, while non-string secrets failed only after a
credential ref had already been derived.

### Fix

- `put()` now validates every persisted identity field and the plaintext
  secret as a nonempty string before deriving a credential ref, encrypting, or
  creating a JSON file.
- Invalid input raises the fixed redacted error
  `CredentialVaultError:invalid-input`; invalid Hire/Attempt values never enter
  the error or a derived reference.
- A successful `put()` is covered by a producer/parser consistency regression
  that immediately resolves the secret and parses the same envelope through
  `find_orphan_receipts()`.
- `CredentialReceipt.path` is documented as informational after a root rename;
  all trusted I/O continues to use the held root directory descriptor.

### GREEN evidence

```text
uv run python -m pytest tests/autonomous/security/test_credential_vault.py -q
# 52 passed in 1.08s

uv run python -m pytest tests/autonomous/security/test_credential_vault.py tests/autonomous/contract/test_config_and_gate_contract.py -q
# 63 passed in 1.21s

uv run python -m pytest tests/autonomous/ -q
# 640 passed in 60.69s (fresh completion verification)

uv run ruff check src/autonomous/workforce/credential_vault.py tests/autonomous/security/test_credential_vault.py src/config/settings.py tests/autonomous/contract/test_config_and_gate_contract.py
# All checks passed!

git diff --check
# no output
```
