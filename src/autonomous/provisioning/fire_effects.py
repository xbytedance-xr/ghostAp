"""Production cleanup effect adapters for employee retirement."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..supervisor.channel_models import ChannelProcessState
from .fire_state import DurableFireState


class ExecutionQuiesceEffect:
    def __init__(self, coordinator: object | None, *, grace_seconds: float = 5.0) -> None:
        self._coordinator = coordinator
        self._grace = max(0.0, float(grace_seconds))

    def execute(self, state: DurableFireState) -> None:
        if self._coordinator is None:
            raise RuntimeError("employee dispatch coordinator unavailable")
        if not state.drain:
            attempts = tuple(self._coordinator.state.attempts.values())
            for attempt in attempts:
                if attempt.binding.agent_id != state.agent_id or attempt.terminal_status:
                    continue
                self._coordinator.request_cancel(
                    agent_id=state.agent_id,
                    chat_id=attempt.binding.chat_id,
                    requester_principal_id=state.requester_principal_id,
                    command_acceptance_id=state.message_id,
                )
        deadline = time.monotonic() + self._grace
        while time.monotonic() < deadline and self.observe(state) is not True:
            time.sleep(0.05)

    def observe(self, state: DurableFireState) -> bool | None:
        if self._coordinator is None:
            return None
        return not any(
            attempt.binding.agent_id == state.agent_id and not attempt.terminal_status
            for attempt in self._coordinator.state.attempts.values()
        )


class SlashCleanupEffect:
    def __init__(
        self,
        *,
        reconciler_factory: Callable[[str, str], Any],
        credential_resolver: Callable[[str, str, str], str],
        async_runner: Callable[[Any], Any],
    ) -> None:
        self._factory = reconciler_factory
        self._resolve = credential_resolver
        self._run = async_runner

    def _reconciler(self, state: DurableFireState):
        secret = self._resolve(state.credential_ref, state.agent_id, state.app_id)
        try:
            return self._factory(state.app_id, secret)
        finally:
            del secret

    def execute(self, state: DurableFireState) -> None:
        self._run(self._reconciler(state).cleanup())

    def observe(self, state: DurableFireState) -> bool | None:
        return self._run(self._reconciler(state).observe_empty())


class ChannelStopEffect:
    def __init__(self, channels: object | None) -> None:
        self._channels = channels

    def execute(self, state: DurableFireState) -> None:
        if self._channels is None:
            raise RuntimeError("employee Channel supervisor unavailable")
        self._channels.stop(state.agent_id)

    def observe(self, state: DurableFireState) -> bool | None:
        if self._channels is None:
            return None
        status = self._channels.status(state.agent_id)
        return status is None or status.state in {
            ChannelProcessState.STOPPED,
            ChannelProcessState.FAILED,
            ChannelProcessState.CRASHED,
        }


class MembershipCleanupEffect:
    def __init__(self, membership: object | None, hire_service: object) -> None:
        self._membership = membership
        self._hire = hire_service

    def execute(self, state: DurableFireState) -> None:
        if self._membership is None:
            raise RuntimeError("employee membership service unavailable")
        outcomes = self._membership.retire_all(
            tenant_key=state.tenant_key,
            agent_id=state.agent_id,
            requester_principal_id=state.requester_principal_id,
        )
        if any(outcome.confirmed is not True for outcome in outcomes):
            raise RuntimeError("employee membership cleanup unconfirmed")

    def observe(self, state: DurableFireState) -> bool | None:
        employee = self._hire.synchronize_projection().employees.get(state.agent_id)
        if employee is None:
            return None
        return employee.member_groups == ()


class CredentialDestroyEffect:
    def __init__(self, vault: object | None) -> None:
        self._vault = vault

    def execute(self, state: DurableFireState) -> None:
        if self._vault is None:
            raise RuntimeError("employee credential Vault unavailable")
        self._vault.destroy(state.credential_ref)

    def observe(self, state: DurableFireState) -> bool | None:
        if self._vault is None:
            return None
        return self._vault.exists(state.credential_ref) is False


class AtomicEmployeeArchive:
    """Write a durable manifest then atomically move one employee directory."""

    def __init__(self, agents_root: str | Path) -> None:
        self._root = Path(agents_root).expanduser().absolute()
        self._archive_root = self._root / ".archive"

    def execute(self, state: DurableFireState) -> None:
        source = self._safe_employee_path(state.agent_id)
        destination = self._archive_root / state.agent_id
        if destination.is_dir():
            return
        if not source.is_dir() or source.is_symlink():
            raise RuntimeError("employee archive source unavailable")
        self._archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest = self._manifest(state, source)
        target = source / "archive_manifest.json"
        temporary = source / ".archive_manifest.tmp"
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(fd, encoded[offset:])
                if written <= 0:
                    raise OSError("short archive manifest write")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        self._fsync(source)
        os.replace(source, destination)
        self._fsync(self._root)
        self._fsync(self._archive_root)

    def observe(self, state: DurableFireState) -> bool | None:
        destination = self._archive_root / state.agent_id
        source = self._root / state.agent_id
        manifest_path = destination / "archive_manifest.json"
        if source.exists() and destination.exists():
            return None
        if not destination.is_dir() or source.exists() or manifest_path.is_symlink():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return (
            manifest.get("agent_id") == state.agent_id
            and manifest.get("external_app_disposition") == "manual_deletion_required"
            and manifest.get("app_id_sha256")
            == hashlib.sha256(state.app_id.encode()).hexdigest()
            and manifest.get("credential_destroyed") is True
            and self._files_match_manifest(destination, manifest.get("files"))
        )

    @staticmethod
    def _files_match_manifest(destination: Path, value: object) -> bool:
        if not isinstance(value, dict):
            return False
        for relative, expected_hash in value.items():
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
            ):
                return False
            candidate = destination / relative
            try:
                candidate.relative_to(destination)
            except ValueError:
                return False
            if candidate.is_symlink() or not candidate.is_file():
                return False
            try:
                observed_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                return False
            if observed_hash != expected_hash:
                return False
        observed_files = {
            str(path.relative_to(destination))
            for path in destination.rglob("*")
            if path.is_file() and path.name != "archive_manifest.json"
        }
        return observed_files == set(value)

    def _safe_employee_path(self, agent_id: str) -> Path:
        if not agent_id.startswith("agt_") or "/" in agent_id or ".." in agent_id:
            raise RuntimeError("invalid employee archive identity")
        return self._root / agent_id

    @staticmethod
    def _manifest(state: DurableFireState, source: Path) -> dict[str, Any]:
        files: dict[str, str] = {}
        dates: set[str] = set()
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("employee archive contains a symlink")
            if path.is_file() and path.name not in {"archive_manifest.json", ".archive_manifest.tmp"}:
                relative = str(path.relative_to(source))
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                dates.update(re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", relative))
        return {
            "schema_version": 1,
            "agent_id": state.agent_id,
            "fire_intent_id": state.intent_id,
            "app_id_sha256": hashlib.sha256(state.app_id.encode()).hexdigest(),
            "credential_ref_sha256": hashlib.sha256(
                state.credential_ref.encode()
            ).hexdigest(),
            "files": files,
            "history_date_range": {
                "start": min(dates) if dates else None,
                "end": max(dates) if dates else None,
            },
            "cleanup_disposition": {
                "execution_quiesce": "committed",
                "slash_cleanup": "committed",
                "channel_stop": "committed",
                "membership_cleanup": "committed",
                "credential_destroy": "committed",
                "archive_move": "executing",
            },
            "credential_destroyed": True,
            "archived_at": datetime.now(UTC).isoformat(),
            "external_app_disposition": "manual_deletion_required",
            "external_disposed_at": None,
            "external_disposed_by": None,
        }

    @staticmethod
    def _fsync(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = [
    "AtomicEmployeeArchive",
    "ChannelStopEffect",
    "CredentialDestroyEffect",
    "ExecutionQuiesceEffect",
    "MembershipCleanupEffect",
    "SlashCleanupEffect",
]
