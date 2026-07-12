"""Journal-backed workforce projections and secret-free identity materialization."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from ..domain import (
    BotPrincipal,
    EmployeeDefinition,
    EmployeeIdOrigin,
    EmployeeState,
)
from ..journal.frame import JournalEvent
from ..journal.writer import AnchorMismatchError, CommitState
from .authority import AuthorityMode, AuthoritySnapshot

if TYPE_CHECKING:
    from ..journal.projections import ProjectionState
    from ..journal.writer import CommitResult, JournalWriter


@dataclass
class WorkforceProjectionState:
    """Workforce portion embedded in the canonical ProjectionState."""

    employees: dict[str, EmployeeDefinition] = field(default_factory=dict)
    bot_principals: dict[str, BotPrincipal] = field(default_factory=dict)
    employee_name_keys: dict[tuple[str, str], str] = field(default_factory=dict)
    legacy_agent_aliases: dict[str, str] = field(default_factory=dict)
    legacy_source_hashes: dict[str, str] = field(default_factory=dict)
    authority_epoch: int = 0
    authority_mode: AuthorityMode = AuthorityMode.LEGACY_WRITE
    authority_cutover_sequence: int = 0

    def authority_snapshot(self) -> AuthoritySnapshot:
        """Return the replayable writer-authority view for mutation guards."""

        return AuthoritySnapshot(
            epoch=self.authority_epoch,
            mode=self.authority_mode,
            cutover_sequence=self.authority_cutover_sequence,
        )


_WORKFORCE_EVENTS = frozenset(
    {
        "employee.created",
        "employee.state_changed",
        "employee.profile_changed",
        "employee.membership_changed",
        "employee.legacy_alias_bound",
        "employee.bot_principal_bound",
        "bot_principal.bound",
        "bot_principal.manifest_observed",
        "credential.destroyed",
        "authority.cutover",
    }
)
_PROFILE_FIELDS = frozenset(
    {
        "name",
        "emoji",
        "tool",
        "model",
        "profile",
        "effort",
        "role",
        "persona",
        "personality_traits",
        "capabilities",
        "permissions",
        "budget_template",
    }
)
_WORKFORCE_COMMIT_LOCK = threading.RLock()
_AGENT_ID_PATTERN = re.compile(r"agt_[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_BOT_PRINCIPAL_ID_PATTERN = re.compile(r"bot_[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _projection_error(message: str) -> Exception:
    from ..journal.projections import ProjectionError

    return ProjectionError(message)


def _employee(state: WorkforceProjectionState, agent_id: str) -> EmployeeDefinition:
    employee = state.employees.get(agent_id)
    if employee is None:
        raise _projection_error(f"unknown employee {agent_id}")
    return employee


def _principal(state: WorkforceProjectionState, principal_id: str) -> BotPrincipal:
    principal = state.bot_principals.get(principal_id)
    if principal is None:
        raise _projection_error(f"unknown bot principal {principal_id}")
    return principal


def _require_mutable_employee(employee: EmployeeDefinition) -> None:
    if employee.state is EmployeeState.ARCHIVED:
        raise _projection_error("archived employee is terminal")


def _name_key(tenant_key: str, name: str) -> tuple[str, str]:
    return (tenant_key, name.casefold())


def _reserve_name(
    state: WorkforceProjectionState,
    employee: EmployeeDefinition,
    *,
    previous_name: str = "",
) -> None:
    key = _name_key(employee.tenant_key, employee.name)
    owner = state.employee_name_keys.get(key)
    if owner is not None and owner != employee.agent_id:
        raise _projection_error(
            f"duplicate active employee name: {employee.name.casefold()}"
        )
    if previous_name and previous_name.casefold() != employee.name.casefold():
        previous_key = _name_key(employee.tenant_key, previous_name)
        if state.employee_name_keys.get(previous_key) == employee.agent_id:
            del state.employee_name_keys[previous_key]
    state.employee_name_keys[key] = employee.agent_id


def _create_employee(state: WorkforceProjectionState, event: JournalEvent) -> None:
    agent_id = str(event.payload.get("agent_id") or event.aggregate_id)
    if agent_id != event.aggregate_id:
        raise _projection_error("employee aggregate_id does not match agent_id")
    if _AGENT_ID_PATTERN.fullmatch(agent_id) is None:
        raise _projection_error("employee requires canonical agent_id")
    if agent_id in state.employees:
        raise _projection_error(f"employee already exists: {agent_id}")
    payload = dict(event.payload)
    payload["agent_id"] = agent_id
    payload.setdefault("created_at", event.timestamp)
    payload.setdefault("updated_at", event.timestamp)
    payload["aggregate_version"] = 1
    try:
        employee = EmployeeDefinition.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise _projection_error(f"invalid employee.created: {exc}") from exc
    _reserve_name(state, employee)
    state.employees[agent_id] = employee


def _change_employee_state(
    state: WorkforceProjectionState, event: JournalEvent
) -> None:
    employee = _employee(state, event.aggregate_id)
    _require_mutable_employee(employee)
    try:
        next_state = EmployeeState(event.payload["state"])
    except (KeyError, ValueError) as exc:
        raise _projection_error("invalid employee state") from exc
    state.employees[employee.agent_id] = replace(
        employee,
        state=next_state,
        updated_at=event.timestamp,
        aggregate_version=employee.aggregate_version + 1,
    )


def _change_employee_profile(
    state: WorkforceProjectionState, event: JournalEvent
) -> None:
    employee = _employee(state, event.aggregate_id)
    _require_mutable_employee(employee)
    unknown = set(event.payload) - _PROFILE_FIELDS
    if unknown:
        raise _projection_error(
            f"unsupported employee profile fields: {sorted(unknown)}"
        )
    values = {key: event.payload[key] for key in event.payload}
    try:
        updated = replace(
            employee,
            **values,
            updated_at=event.timestamp,
            aggregate_version=employee.aggregate_version + 1,
        )
    except (TypeError, ValueError) as exc:
        raise _projection_error(f"invalid employee profile: {exc}") from exc
    _reserve_name(state, updated, previous_name=employee.name)
    state.employees[employee.agent_id] = updated


def _change_membership(state: WorkforceProjectionState, event: JournalEvent) -> None:
    employee = _employee(state, event.aggregate_id)
    _require_mutable_employee(employee)
    groups = event.payload.get("member_groups")
    if not isinstance(groups, (list, tuple)) or not all(
        isinstance(group, str) and group for group in groups
    ):
        raise _projection_error("member_groups must be non-empty strings")
    state.employees[employee.agent_id] = replace(
        employee,
        member_groups=tuple(dict.fromkeys(groups)),
        updated_at=event.timestamp,
        aggregate_version=employee.aggregate_version + 1,
    )


def _bind_legacy_alias(state: WorkforceProjectionState, event: JournalEvent) -> None:
    employee = _employee(state, event.aggregate_id)
    _require_mutable_employee(employee)
    alias = event.payload.get("legacy_id_alias")
    source_hash = event.payload.get("source_hash")
    if not isinstance(alias, str) or not alias:
        raise _projection_error("legacy_id_alias is required")
    if not isinstance(source_hash, str) or not source_hash:
        raise _projection_error("source_hash is required")
    alias_owner = state.legacy_agent_aliases.get(alias)
    hash_owner = state.legacy_source_hashes.get(source_hash)
    if alias_owner not in (None, employee.agent_id):
        raise _projection_error("legacy alias already bound")
    if hash_owner not in (None, employee.agent_id):
        raise _projection_error("legacy source hash already bound")
    state.legacy_agent_aliases[alias] = employee.agent_id
    state.legacy_source_hashes[source_hash] = employee.agent_id
    state.employees[employee.agent_id] = replace(
        employee,
        id_origin=EmployeeIdOrigin.LEGACY_ALIAS,
        legacy_id_alias=alias,
        updated_at=event.timestamp,
        aggregate_version=employee.aggregate_version + 1,
    )


def _bind_employee_principal(
    state: WorkforceProjectionState, event: JournalEvent
) -> None:
    employee = _employee(state, event.aggregate_id)
    _require_mutable_employee(employee)
    agent_id = event.payload.get("agent_id")
    principal_id = event.payload.get("bot_principal_id")
    if agent_id != employee.agent_id or not isinstance(principal_id, str) or not principal_id:
        raise _projection_error("invalid employee bot principal binding")
    if _BOT_PRINCIPAL_ID_PATTERN.fullmatch(principal_id) is None:
        raise _projection_error("binding requires canonical bot_principal_id")
    if employee.bot_principal_id not in (None, principal_id):
        raise _projection_error("employee already bound to another bot principal")
    state.employees[employee.agent_id] = replace(
        employee,
        bot_principal_id=principal_id,
        updated_at=event.timestamp,
        aggregate_version=employee.aggregate_version + 1,
    )


def _bind_bot_principal(state: WorkforceProjectionState, event: JournalEvent) -> None:
    payload = dict(event.payload)
    principal_id = str(payload.get("bot_principal_id") or event.aggregate_id)
    if principal_id != event.aggregate_id:
        raise _projection_error("bot principal aggregate_id mismatch")
    if _BOT_PRINCIPAL_ID_PATTERN.fullmatch(principal_id) is None:
        raise _projection_error("binding requires canonical bot_principal_id")
    if principal_id in state.bot_principals:
        raise _projection_error("bot principal already exists")
    agent_id = payload.get("agent_id")
    employee = _employee(state, str(agent_id or ""))
    if employee.bot_principal_id != principal_id:
        raise _projection_error("bot binding events do not match")
    app_id = payload.get("app_id")
    credential_ref = payload.get("credential_ref")
    if payload.get("tenant_key") != employee.tenant_key:
        raise _projection_error("bot principal tenant does not match employee tenant")
    if not isinstance(app_id, str) or not app_id:
        raise _projection_error("bot principal app_id is required")
    if not isinstance(credential_ref, str) or not credential_ref:
        raise _projection_error("bot principal credential_ref is required")
    for existing in state.bot_principals.values():
        existing_employee = state.employees.get(existing.agent_id)
        if (
            app_id
            and existing.app_id == app_id
            and existing_employee is not None
            and existing_employee.state is not EmployeeState.ARCHIVED
        ):
            raise _projection_error(f"app_id already bound: {app_id}")
    payload["bot_principal_id"] = principal_id
    payload["aggregate_version"] = 1
    try:
        state.bot_principals[principal_id] = BotPrincipal.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise _projection_error(f"invalid bot principal: {exc}") from exc


def _observe_manifest(state: WorkforceProjectionState, event: JournalEvent) -> None:
    principal = _principal(state, event.aggregate_id)
    observed = event.payload.get("observed_manifest_hash")
    if not isinstance(observed, str) or not observed:
        raise _projection_error("observed_manifest_hash is required")
    state.bot_principals[principal.bot_principal_id] = replace(
        principal,
        observed_manifest_hash=observed,
        aggregate_version=principal.aggregate_version + 1,
    )


def _destroy_credential(state: WorkforceProjectionState, event: JournalEvent) -> None:
    principal = _principal(state, event.aggregate_id)
    credential_ref = event.payload.get("credential_ref")
    if credential_ref != principal.credential_ref:
        raise _projection_error("credential ref does not match bot principal")
    state.bot_principals[principal.bot_principal_id] = replace(
        principal,
        credential_ref="",
        aggregate_version=principal.aggregate_version + 1,
    )


def _cutover_authority(state: WorkforceProjectionState, event: JournalEvent) -> None:
    if event.aggregate_id != "workforce_authority":
        raise _projection_error("authority cutover aggregate is invalid")
    required = {"authority_epoch", "authority_mode", "cutover_sequence"}
    if set(event.payload) != required:
        raise _projection_error("authority cutover payload fields are invalid")
    epoch = event.payload.get("authority_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= state.authority_epoch:
        raise _projection_error("authority_epoch must increase")
    try:
        mode = AuthorityMode(event.payload["authority_mode"])
    except (KeyError, ValueError, TypeError) as exc:
        raise _projection_error("authority_mode is invalid") from exc
    cutover_sequence = event.payload.get("cutover_sequence")
    if (
        isinstance(cutover_sequence, bool)
        or not isinstance(cutover_sequence, int)
        or cutover_sequence < state.authority_cutover_sequence
    ):
        raise _projection_error("cutover_sequence must not decrease")
    state.authority_epoch = epoch
    state.authority_mode = mode
    state.authority_cutover_sequence = cutover_sequence


_APPLIERS = {
    "employee.created": _create_employee,
    "employee.state_changed": _change_employee_state,
    "employee.profile_changed": _change_employee_profile,
    "employee.membership_changed": _change_membership,
    "employee.legacy_alias_bound": _bind_legacy_alias,
    "employee.bot_principal_bound": _bind_employee_principal,
    "bot_principal.bound": _bind_bot_principal,
    "bot_principal.manifest_observed": _observe_manifest,
    "credential.destroyed": _destroy_credential,
    "authority.cutover": _cutover_authority,
}


def apply_workforce_event(state: WorkforceProjectionState, event: JournalEvent) -> bool:
    """Apply a workforce event and report whether the event was recognized."""

    applier = _APPLIERS.get(event.event_type)
    if applier is None:
        return False
    applier(state, event)
    return True


def _validate_bot_binding_pairs(events: tuple[JournalEvent, ...]) -> None:
    employee_events = tuple(
        event
        for event in events
        if event.event_type == "employee.bot_principal_bound"
    )
    principal_events = tuple(
        event for event in events if event.event_type == "bot_principal.bound"
    )
    if not employee_events and not principal_events:
        return
    if not employee_events or not principal_events:
        raise _projection_error("bot binding events must be in same transaction")
    if len(employee_events) != 1 or len(principal_events) != 1:
        raise _projection_error(
            "bot binding transaction requires exactly one event per aggregate"
        )
    employee_bindings = {
        (event.payload.get("agent_id"), event.payload.get("bot_principal_id"))
        for event in employee_events
    }
    principal_bindings = {
        (event.payload.get("agent_id"), event.payload.get("bot_principal_id") or event.aggregate_id)
        for event in principal_events
    }
    if employee_bindings != principal_bindings:
        raise _projection_error("bot binding events do not match")


def validate_workforce_frame_events(events: Iterable[JournalEvent]) -> None:
    """Validate transaction-level workforce invariants during replay."""

    workforce_events = tuple(
        event for event in events if event.event_type in _WORKFORCE_EVENTS
    )
    event_keys = [
        (event.aggregate_id, event.event_type) for event in workforce_events
    ]
    if len(event_keys) != len(set(event_keys)):
        raise _projection_error(
            "workforce transaction rejects duplicate event type per aggregate"
        )
    _validate_bot_binding_pairs(workforce_events)


def normalize_workforce_aggregate_versions(
    state: WorkforceProjectionState,
    aggregate_versions: Mapping[str, int],
    events: Iterable[JournalEvent],
) -> None:
    """Align workforce aggregate versions with the committed Journal frame."""

    workforce_ids = {
        event.aggregate_id
        for event in events
        if event.event_type in _WORKFORCE_EVENTS
    }
    for aggregate_id in workforce_ids:
        version = aggregate_versions[aggregate_id]
        employee = state.employees.get(aggregate_id)
        if employee is not None:
            state.employees[aggregate_id] = replace(
                employee,
                aggregate_version=version,
            )
        principal = state.bot_principals.get(aggregate_id)
        if principal is not None:
            state.bot_principals[aggregate_id] = replace(
                principal,
                aggregate_version=version,
            )


def _clone_projection_state(
    state: WorkforceProjectionState,
) -> WorkforceProjectionState:
    """Copy projection containers while sharing immutable domain values."""

    isolated = copy.copy(state)
    for attribute in (
        "employees",
        "bot_principals",
        "employee_name_keys",
        "legacy_agent_aliases",
        "legacy_source_hashes",
        "goals",
        "runs",
        "plans",
        "steps",
        "effects",
    ):
        values = getattr(state, attribute, None)
        if values is not None:
            setattr(isolated, attribute, dict(values))
    inbox = getattr(state, "inbox", None)
    if inbox is not None:
        isolated.inbox = {
            event_id: copy.copy(record) for event_id, record in inbox.items()
        }
    for attribute in ("dedup_keys", "occurrence_keys"):
        values = getattr(state, attribute, None)
        if values is not None:
            setattr(isolated, attribute, set(values))
    return isolated


def validate_workforce_events(
    state: WorkforceProjectionState,
    events: Iterable[JournalEvent],
) -> WorkforceProjectionState:
    """Validate a workforce transaction against an isolated projection copy."""

    event_values = tuple(events)
    if not event_values:
        raise ValueError("workforce transaction cannot be empty")
    if any(event.event_type not in _WORKFORCE_EVENTS for event in event_values):
        raise ValueError("workforce transaction contains a non-workforce event")
    validate_workforce_frame_events(event_values)
    isolated = _clone_projection_state(state)
    for event in event_values:
        apply_workforce_event(isolated, event)
    return isolated


def commit_workforce_events(
    writer: JournalWriter,
    state: ProjectionState,
    events: Iterable[JournalEvent],
) -> CommitResult:
    """Validate, durably commit, and apply one serialized workforce transaction."""

    from ..journal.projections import apply_frame

    event_values = tuple(events)
    with _WORKFORCE_COMMIT_LOCK:
        last_frame = writer.get_last_frame()
        writer_sequence = 0 if last_frame is None else last_frame.sequence
        writer_hash = "" if last_frame is None else last_frame.frame_hash
        if (
            state.cursor_sequence != writer_sequence
            or state.cursor_hash != writer_hash
        ):
            raise _projection_error("workforce projection is stale")
        validate_workforce_events(state, event_values)
        aggregate_ids = {event.aggregate_id for event in event_values}
        expected_versions = writer.get_aggregate_versions(aggregate_ids)
        result = writer.commit(
            event_values,
            expected_versions,
            expected_head_sequence=state.cursor_sequence,
            expected_head_hash=state.cursor_hash,
        )
        if result.state is not CommitState.ANCHORED:
            raise AnchorMismatchError("workforce commit was not anchored")
        apply_frame(state, result.frame)
        return result


class EmployeeIdentityMaterializer:
    """Materialize rebuildable, secret-free employee identity projections."""

    def __init__(self, agents_root: str | Path) -> None:
        self._root = Path(agents_root)

    def materialize(
        self,
        state: WorkforceProjectionState,
        agent_id: str,
    ) -> Path:
        employee = state.employees.get(agent_id)
        if employee is None:
            raise KeyError(agent_id)
        payload = {
            "agent_id": employee.agent_id,
            "tenant_key": employee.tenant_key,
            "owner_principal_id": employee.owner_principal_id,
            "name": employee.name,
            "emoji": employee.emoji,
            "tool": employee.tool,
            "model": employee.model,
            "profile": employee.profile,
            "effort": employee.effort,
            "role": employee.role,
            "persona": employee.persona,
            "personality_traits": list(employee.personality_traits),
            "worker_type": employee.worker_type.value,
            "state": employee.state.value,
            "capabilities": list(employee.capabilities),
            "permissions": list(employee.permissions),
            "bot_principal_id": employee.bot_principal_id,
            "member_groups": list(employee.member_groups),
            "id_origin": employee.id_origin.value,
            "legacy_id_alias": employee.legacy_id_alias,
            "created_at": employee.created_at,
            "updated_at": employee.updated_at,
            "aggregate_version": employee.aggregate_version,
        }
        principal = (
            state.bot_principals.get(employee.bot_principal_id)
            if employee.bot_principal_id
            else None
        )
        if employee.bot_principal_id and principal is None:
            raise _projection_error("employee references missing bot principal")
        if principal is not None:
            payload.update(
                {
                    "app_id": principal.app_id,
                    "credential_ref": principal.credential_ref,
                    "scopes": list(principal.scopes),
                    "desired_manifest_hash": principal.desired_manifest_hash,
                    "observed_manifest_hash": principal.observed_manifest_hash,
                }
            )
        else:
            payload.update({"app_id": "", "credential_ref": "", "scopes": []})
        return self._atomic_write(agent_id, payload)

    def materialize_all(self, state: WorkforceProjectionState) -> list[Path]:
        return [self.materialize(state, agent_id) for agent_id in sorted(state.employees)]

    def _atomic_write(self, agent_id: str, payload: dict) -> Path:
        if _AGENT_ID_PATTERN.fullmatch(agent_id) is None:
            raise _projection_error("agent_id must be a canonical safe path component")
        root_fd = self._open_root(self._root)
        try:
            os.fchmod(root_fd, 0o700)
            try:
                os.mkdir(agent_id, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(
                agent_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                os.fchmod(directory_fd, 0o700)
                self._write_identity_file(directory_fd, payload)
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)
        return self._root / agent_id / "identity.json"

    @staticmethod
    def _open_root(root: Path) -> int:
        parts = root.parts[1:] if root.is_absolute() else root.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise OSError("agents root must contain safe path components")
        flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
        )
        descriptor = os.open("/" if root.is_absolute() else ".", flags)
        try:
            for part in parts:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    os.close(child)
                    raise OSError("agents root component is not a directory")
                os.close(descriptor)
                descriptor = child
            os.fchmod(descriptor, 0o700)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _write_identity_file(directory_fd: int, payload: dict) -> None:
        temp_name = f".identity-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temp_name,
                "identity.json",
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
