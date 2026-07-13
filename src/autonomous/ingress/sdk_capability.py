"""Strict development evidence for the pinned employee Channel SDK."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

LOCKED_LARK_CHANNEL_VERSION = "1.1.0"
LOCKED_LARK_CHANNEL_WHEEL_SHA256 = "d5f094d697578c315d5ec02c1fe7cc6f779314f265e3335c6e0acd8ff4afceba"
LOCKED_LARK_CHANNEL_INSTALLED_RECORD_SHA256 = "832add283a7ba9800978e6e94b37bab223aa266ddc0d6163ff93d564fc06ee27"
LOCKED_LARK_CHANNEL_RUNTIME_PAYLOAD_SHA256 = "845e6c04019aefd54ec56c37a435d45a1f5e1cff8a81b7eb5382049ad3e05c88"
CAPABILITY_PROFILE_ID = "employee-channel-sdk-v1"
CAPABILITY_NODEIDS = (
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_message_wire_response_waits_for_parent_anchor",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_card_action_wire_response_waits_for_parent_anchor",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_high_level_message_handler_can_finish_after_wire_success",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_message_callback_timeout_at_ack_deadline_is_wire_500",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_card_callback_timeout_at_ack_deadline_is_wire_500",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_parent_close_is_wire_500",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_callback_exception_is_wire_500",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_raw_card_wire_type_is_not_card_action_capability",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_reconnect_requested_during_callback_is_bounded",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_idle_reconnect_is_bounded",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_blocked_callback_worker_termination_is_bounded",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_control_ping_resumes_after_callback",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_strict_local_ws_endpoint_is_rejected",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_environment_proxy_is_ignored_for_direct_wss",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_fragment_overflow_is_dropped_before_callback",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_fragment_byte_overflow_is_dropped_before_callback",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_concurrency_cap_holds_second_callback",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_single_frame_payload_limit_requires_parent_gate",
    "tests/autonomous/contract/test_employee_channel_sdk_capability.py::"
    "test_sensitive_sentinels_are_absent_from_worker_logs",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_OUTCOMES = {"passed", "failed", "skipped"}
_MAX_EVIDENCE_BYTES = 256 * 1024
_REPOSITORY = Path(__file__).resolve().parents[3]


class CapabilityDecision(str, Enum):
    CAPABLE_PINNED_ADAPTER = "CAPABLE_PINNED_ADAPTER"
    CAPABILITY_RED = "CAPABILITY_RED"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"invalid {name} fields")


def _validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")


@dataclass(frozen=True, slots=True)
class SDKDistributionIdentity:
    distribution_name: str
    version: str
    lock_wheel_sha256: str
    observed_wheel_archive_sha256: str | None
    installed_record_sha256: str
    runtime_payload_sha256: str
    record_verified: bool
    project_lock_sha256: str
    installed_identity_algorithm: str = "record-sha256-triples-v1"
    runtime_identity_algorithm: str = "package-sha256-triples-v1"
    path_basis: str = "site-packages-relative-posix"

    def __post_init__(self) -> None:
        if self.distribution_name != "lark-channel-sdk":
            raise ValueError("unexpected Channel SDK distribution")
        if self.version != LOCKED_LARK_CHANNEL_VERSION:
            raise ValueError("unexpected Channel SDK version")
        _validate_sha256(self.lock_wheel_sha256, "lock wheel hash")
        if self.observed_wheel_archive_sha256 is not None:
            _validate_sha256(
                self.observed_wheel_archive_sha256,
                "observed wheel archive hash",
            )
        _validate_sha256(self.installed_record_sha256, "installed RECORD hash")
        _validate_sha256(self.runtime_payload_sha256, "runtime payload hash")
        _validate_sha256(self.project_lock_sha256, "project lock hash")
        if self.record_verified is not True:
            raise ValueError("SDK RECORD must be verified")
        if self.lock_wheel_sha256 != LOCKED_LARK_CHANNEL_WHEEL_SHA256:
            raise ValueError("Channel SDK wheel hash is not trusted")
        if self.installed_record_sha256 != LOCKED_LARK_CHANNEL_INSTALLED_RECORD_SHA256:
            raise ValueError("Channel SDK installed RECORD identity is not trusted")
        if self.runtime_payload_sha256 != LOCKED_LARK_CHANNEL_RUNTIME_PAYLOAD_SHA256:
            raise ValueError("Channel SDK runtime payload identity is not trusted")
        if self.installed_identity_algorithm != "record-sha256-triples-v1":
            raise ValueError("unsupported installed identity algorithm")
        if self.runtime_identity_algorithm != "package-sha256-triples-v1":
            raise ValueError("unsupported runtime identity algorithm")
        if self.path_basis != "site-packages-relative-posix":
            raise ValueError("unsupported SDK identity path basis")

    def to_dict(self) -> dict[str, Any]:
        return {
            "distribution_name": self.distribution_name,
            "installed_identity_algorithm": self.installed_identity_algorithm,
            "installed_record_sha256": self.installed_record_sha256,
            "lock_wheel_sha256": self.lock_wheel_sha256,
            "observed_wheel_archive_sha256": self.observed_wheel_archive_sha256,
            "path_basis": self.path_basis,
            "project_lock_sha256": self.project_lock_sha256,
            "record_verified": self.record_verified,
            "runtime_identity_algorithm": self.runtime_identity_algorithm,
            "runtime_payload_sha256": self.runtime_payload_sha256,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SDKDistributionIdentity:
        expected = {
            "distribution_name",
            "version",
            "lock_wheel_sha256",
            "observed_wheel_archive_sha256",
            "installed_record_sha256",
            "runtime_payload_sha256",
            "record_verified",
            "installed_identity_algorithm",
            "runtime_identity_algorithm",
            "path_basis",
            "project_lock_sha256",
        }
        _exact_fields(value, expected, "SDK identity")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CapabilityTestOutcome:
    nodeid: str
    setup: str
    call: str
    teardown: str

    def __post_init__(self) -> None:
        if self.nodeid not in CAPABILITY_NODEIDS:
            raise ValueError("unknown capability test nodeid")
        for value in (self.setup, self.call, self.teardown):
            if value not in _OUTCOMES:
                raise ValueError("invalid capability test outcome")

    @property
    def passed(self) -> bool:
        return self.setup == "passed" and self.call == "passed" and self.teardown == "passed"

    def to_dict(self) -> dict[str, str]:
        return {
            "call": self.call,
            "nodeid": self.nodeid,
            "setup": self.setup,
            "teardown": self.teardown,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityTestOutcome:
        _exact_fields(value, {"nodeid", "setup", "call", "teardown"}, "outcome")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeIdentity:
    python_implementation: str
    python_version: str
    pytest_version: str
    requests_version: str
    websockets_version: str

    def __post_init__(self) -> None:
        for value in (
            self.python_implementation,
            self.python_version,
            self.pytest_version,
            self.requests_version,
            self.websockets_version,
        ):
            if not isinstance(value, str) or not value:
                raise ValueError("runtime identity fields must be non-empty strings")

    def to_dict(self) -> dict[str, str]:
        return {
            "pytest_version": self.pytest_version,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "requests_version": self.requests_version,
            "websockets_version": self.websockets_version,
        }

    @classmethod
    def current(cls) -> CapabilityRuntimeIdentity:
        return cls(
            python_implementation=platform.python_implementation(),
            python_version=platform.python_version(),
            pytest_version=importlib.metadata.version("pytest"),
            requests_version=importlib.metadata.version("requests"),
            websockets_version=importlib.metadata.version("websockets"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityRuntimeIdentity:
        _exact_fields(
            value,
            {
                "python_implementation",
                "python_version",
                "pytest_version",
                "requests_version",
                "websockets_version",
            },
            "runtime identity",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CapabilityRunEvidence:
    schema_version: int
    artifact_kind: str
    profile_id: str
    decision: CapabilityDecision
    promotable: bool
    requires_parent_payload_gate: bool
    commit_sha: str
    worktree_clean: bool
    sdk: SDKDistributionIdentity
    runtime: CapabilityRuntimeIdentity
    requested_nodeids: tuple[str, ...]
    collected_nodeids: tuple[str, ...]
    outcomes: tuple[CapabilityTestOutcome, ...]
    pytest_exit_code: int
    result_summary_sha256: str
    created_at: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported capability evidence schema")
        if self.artifact_kind != "capability_run_evidence":
            raise ValueError("unsupported capability evidence schema")
        if self.profile_id != CAPABILITY_PROFILE_ID:
            raise ValueError("unexpected capability profile")
        if self.promotable is not False:
            raise ValueError("development capability evidence is never promotable")
        if self.requires_parent_payload_gate is not True:
            raise ValueError("parent payload gate requirement must be explicit")
        if not _COMMIT_RE.fullmatch(self.commit_sha):
            raise ValueError("invalid commit SHA")
        if not isinstance(self.worktree_clean, bool):
            raise ValueError("worktree_clean must be boolean")
        if self.requested_nodeids != CAPABILITY_NODEIDS:
            raise ValueError("capability requested nodeids are not frozen")
        if len(set(self.collected_nodeids)) != len(self.collected_nodeids):
            raise ValueError("duplicate collected nodeid")
        if any(not isinstance(nodeid, str) for nodeid in self.collected_nodeids):
            raise ValueError("collected nodeids must be strings")
        if len({outcome.nodeid for outcome in self.outcomes}) != len(self.outcomes):
            raise ValueError("duplicate capability test outcome")
        if not isinstance(self.pytest_exit_code, int) or isinstance(self.pytest_exit_code, bool):
            raise ValueError("invalid pytest exit code")
        _validate_sha256(self.result_summary_sha256, "result summary hash")
        if not isinstance(self.created_at, str) or not _RFC3339_UTC_RE.fullmatch(
            self.created_at
        ):
            raise ValueError("invalid evidence timestamp")
        try:
            dt.datetime.strptime(self.created_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError("invalid evidence timestamp") from exc
        _validate_sha256(self.artifact_sha256, "artifact hash")
        expected_decision = self._derive_decision(
            self.collected_nodeids,
            self.outcomes,
            self.pytest_exit_code,
        )
        if self.decision is not expected_decision:
            raise ValueError("capability decision does not match test results")
        if self.result_summary_sha256 != self._compute_result_summary_sha256():
            raise ValueError("result summary hash mismatch")
        if self.artifact_sha256 != self.compute_artifact_sha256():
            raise ValueError("artifact hash mismatch")

    @classmethod
    def create(
        cls,
        *,
        commit_sha: str,
        worktree_clean: bool,
        sdk: SDKDistributionIdentity,
        requested_nodeids: tuple[str, ...],
        collected_nodeids: tuple[str, ...],
        outcomes: tuple[CapabilityTestOutcome, ...],
        pytest_exit_code: int,
        created_at: str,
    ) -> CapabilityRunEvidence:
        decision = cls._derive_decision(
            collected_nodeids,
            outcomes,
            pytest_exit_code,
        )
        runtime = CapabilityRuntimeIdentity.current()
        base = {
            "schema_version": 1,
            "artifact_kind": "capability_run_evidence",
            "profile_id": CAPABILITY_PROFILE_ID,
            "decision": decision,
            "promotable": False,
            "requires_parent_payload_gate": True,
            "commit_sha": commit_sha,
            "worktree_clean": worktree_clean,
            "sdk": sdk,
            "runtime": runtime,
            "requested_nodeids": requested_nodeids,
            "collected_nodeids": collected_nodeids,
            "outcomes": outcomes,
            "pytest_exit_code": pytest_exit_code,
            "created_at": created_at,
        }
        summary_hash = cls._summary_hash(
            collected_nodeids,
            outcomes,
            pytest_exit_code,
        )
        unsigned = cls._dict_from_values(**base, result_summary_sha256=summary_hash)
        artifact_hash = _sha256(_canonical_json(unsigned))
        return cls(
            **base,
            result_summary_sha256=summary_hash,
            artifact_sha256=artifact_hash,
        )

    @staticmethod
    def _derive_decision(
        collected_nodeids: tuple[str, ...],
        outcomes: tuple[CapabilityTestOutcome, ...],
        pytest_exit_code: int,
    ) -> CapabilityDecision:
        outcome_by_id = {outcome.nodeid: outcome for outcome in outcomes}
        capable = (
            pytest_exit_code == 0
            and collected_nodeids == CAPABILITY_NODEIDS
            and tuple(outcome_by_id) == CAPABILITY_NODEIDS
            and all(outcome_by_id[nodeid].passed for nodeid in CAPABILITY_NODEIDS)
        )
        if capable:
            return CapabilityDecision.CAPABLE_PINNED_ADAPTER
        return CapabilityDecision.CAPABILITY_RED

    @staticmethod
    def _summary_hash(
        collected_nodeids: tuple[str, ...],
        outcomes: tuple[CapabilityTestOutcome, ...],
        pytest_exit_code: int,
    ) -> str:
        return _sha256(
            _canonical_json(
                {
                    "collected_nodeids": list(collected_nodeids),
                    "outcomes": [outcome.to_dict() for outcome in outcomes],
                    "pytest_exit_code": pytest_exit_code,
                }
            )
        )

    def _compute_result_summary_sha256(self) -> str:
        return self._summary_hash(
            self.collected_nodeids,
            self.outcomes,
            self.pytest_exit_code,
        )

    @staticmethod
    def _dict_from_values(**values: Any) -> dict[str, Any]:
        return {
            "artifact_kind": values["artifact_kind"],
            "collected_nodeids": list(values["collected_nodeids"]),
            "commit_sha": values["commit_sha"],
            "created_at": values["created_at"],
            "decision": values["decision"].value,
            "outcomes": [outcome.to_dict() for outcome in values["outcomes"]],
            "profile_id": values["profile_id"],
            "promotable": values["promotable"],
            "requires_parent_payload_gate": values["requires_parent_payload_gate"],
            "pytest_exit_code": values["pytest_exit_code"],
            "requested_nodeids": list(values["requested_nodeids"]),
            "result_summary_sha256": values["result_summary_sha256"],
            "runtime": values["runtime"].to_dict(),
            "schema_version": values["schema_version"],
            "sdk": values["sdk"].to_dict(),
            "worktree_clean": values["worktree_clean"],
        }

    def unsigned_dict(self) -> dict[str, Any]:
        return self._dict_from_values(
            artifact_kind=self.artifact_kind,
            collected_nodeids=self.collected_nodeids,
            commit_sha=self.commit_sha,
            created_at=self.created_at,
            decision=self.decision,
            outcomes=self.outcomes,
            profile_id=self.profile_id,
            promotable=self.promotable,
            requires_parent_payload_gate=self.requires_parent_payload_gate,
            pytest_exit_code=self.pytest_exit_code,
            requested_nodeids=self.requested_nodeids,
            result_summary_sha256=self.result_summary_sha256,
            runtime=self.runtime,
            schema_version=self.schema_version,
            sdk=self.sdk,
            worktree_clean=self.worktree_clean,
        )

    def compute_artifact_sha256(self) -> str:
        return _sha256(_canonical_json(self.unsigned_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "artifact_sha256": self.artifact_sha256}

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CapabilityRunEvidence:
        if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_EVIDENCE_BYTES:
            raise ValueError("invalid capability evidence size")
        try:
            value = json.loads(raw, object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid capability evidence JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("capability evidence must be an object")
        expected = {
            "schema_version",
            "artifact_kind",
            "profile_id",
            "decision",
            "promotable",
            "requires_parent_payload_gate",
            "commit_sha",
            "worktree_clean",
            "sdk",
            "runtime",
            "requested_nodeids",
            "collected_nodeids",
            "outcomes",
            "pytest_exit_code",
            "result_summary_sha256",
            "created_at",
            "artifact_sha256",
        }
        _exact_fields(value, expected, "capability evidence")
        try:
            return cls(
                schema_version=value["schema_version"],
                artifact_kind=value["artifact_kind"],
                profile_id=value["profile_id"],
                decision=CapabilityDecision(value["decision"]),
                promotable=value["promotable"],
                requires_parent_payload_gate=value["requires_parent_payload_gate"],
                commit_sha=value["commit_sha"],
                worktree_clean=value["worktree_clean"],
                sdk=SDKDistributionIdentity.from_dict(value["sdk"]),
                runtime=CapabilityRuntimeIdentity.from_dict(value["runtime"]),
                requested_nodeids=tuple(value["requested_nodeids"]),
                collected_nodeids=tuple(value["collected_nodeids"]),
                outcomes=tuple(CapabilityTestOutcome.from_dict(item) for item in value["outcomes"]),
                pytest_exit_code=value["pytest_exit_code"],
                result_summary_sha256=value["result_summary_sha256"],
                created_at=value["created_at"],
                artifact_sha256=value["artifact_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid capability evidence: {exc}") from exc

    def write_atomic(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(self.to_json_bytes() + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def _verify_project_lock() -> str:
    pyproject_bytes = (_REPOSITORY / "pyproject.toml").read_bytes()
    lock_bytes = (_REPOSITORY / "uv.lock").read_bytes()
    pyproject = tomllib.loads(pyproject_bytes.decode("utf-8"))
    dependencies = pyproject.get("project", {}).get("dependencies", [])
    pins = [
        value
        for value in dependencies
        if isinstance(value, str)
        and re.sub(r"[-_.]+", "-", value.split("==", 1)[0].lower())
        == "lark-channel-sdk"
    ]
    if pins != [f"lark-channel-sdk=={LOCKED_LARK_CHANNEL_VERSION}"]:
        raise ValueError("pyproject must strictly pin the trusted Channel SDK")

    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    packages = [
        package
        for package in lock.get("package", [])
        if package.get("name") == "lark-channel-sdk"
    ]
    if len(packages) != 1 or packages[0].get("version") != LOCKED_LARK_CHANNEL_VERSION:
        raise ValueError("uv.lock Channel SDK package is not uniquely pinned")
    wheel_hashes = {
        wheel.get("hash")
        for wheel in packages[0].get("wheels", [])
        if isinstance(wheel, dict)
    }
    if wheel_hashes != {f"sha256:{LOCKED_LARK_CHANNEL_WHEEL_SHA256}"}:
        raise ValueError("uv.lock Channel SDK wheel hash is not trusted")
    return _sha256(
        _canonical_json(
            {
                "pyproject_sha256": _sha256(pyproject_bytes),
                "uv_lock_sha256": _sha256(lock_bytes),
            }
        )
    )


def prepare_controlled_sdk_import_cache(cache_root: Path) -> Path:
    """Force subsequent SDK imports to ignore source-adjacent bytecode caches."""
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    if any(root.iterdir()):
        raise ValueError("controlled SDK bytecode cache must be empty")
    resolved = root.resolve(strict=True)
    sys.pycache_prefix = str(resolved)
    sys.dont_write_bytecode = True
    return resolved


def collect_sdk_distribution_identity(
    *,
    require_controlled_import_cache: bool = False,
) -> SDKDistributionIdentity:
    if require_controlled_import_cache:
        prefix = sys.pycache_prefix
        if (
            not sys.dont_write_bytecode
            or not isinstance(prefix, str)
            or not prefix
            or any(
                name == "lark_channel" or name.startswith("lark_channel.")
                for name in sys.modules
            )
        ):
            raise ValueError("controlled SDK import cache is not active before import")
        cache_root = Path(prefix)
        if not cache_root.is_dir() or any(cache_root.iterdir()):
            raise ValueError("controlled SDK import cache is not empty")
    project_lock_sha256 = _verify_project_lock()
    distribution = importlib.metadata.distribution("lark-channel-sdk")
    name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"].lower())
    if name != "lark-channel-sdk":
        raise ValueError("unexpected Channel SDK distribution name")
    if distribution.version != LOCKED_LARK_CHANNEL_VERSION:
        raise ValueError("unexpected Channel SDK version")
    files = distribution.files
    if not files:
        raise ValueError("Channel SDK RECORD is unavailable")

    root = Path(distribution.locate_file("")).resolve()
    seen: set[str] = set()
    verified: list[list[Any]] = []
    record_package_files: set[str] = set()
    for package_path in files:
        relative = PurePosixPath(str(package_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe Channel SDK RECORD path")
        relative_text = relative.as_posix()
        if relative_text in seen:
            raise ValueError("duplicate Channel SDK RECORD path")
        seen.add(relative_text)
        located = Path(distribution.locate_file(package_path))
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("Channel SDK RECORD path contains symlink")
        try:
            located.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError("Channel SDK RECORD path escapes distribution") from exc
        if package_path.hash is None:
            if not relative_text.endswith(".dist-info/RECORD"):
                raise ValueError("Channel SDK RECORD entry lacks hash")
            continue
        if package_path.hash.mode != "sha256":
            raise ValueError("Channel SDK RECORD entry uses non-sha256 hash")
        if not located.is_file() or located.is_symlink():
            raise ValueError("Channel SDK RECORD file is missing or unsafe")
        content = located.read_bytes()
        if package_path.size != len(content):
            raise ValueError("Channel SDK RECORD size mismatch")
        padding = "=" * (-len(package_path.hash.value) % 4)
        expected_digest = base64.urlsafe_b64decode(package_path.hash.value + padding)
        actual_digest = hashlib.sha256(content).digest()
        if actual_digest != expected_digest:
            raise ValueError("Channel SDK RECORD hash mismatch")
        digest_hex = actual_digest.hex()
        verified.append([relative_text, len(content), digest_hex])
        if relative.parts and relative.parts[0] == "lark_channel":
            record_package_files.add(relative_text)

    package_root = root / "lark_channel"
    actual_package_files: set[str] = set()
    runtime_payload: list[list[Any]] = []
    for path in sorted(package_root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Channel SDK package contains symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        actual_package_files.add(relative)
        content = path.read_bytes()
        runtime_payload.append([relative, len(content), _sha256(content)])
    if actual_package_files != record_package_files:
        raise ValueError("Channel SDK package files differ from RECORD")

    import lark_channel.ws.client as sdk_client

    client_path = Path(sdk_client.__file__ or "").resolve()
    try:
        client_relative = client_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Channel SDK import is shadowed") from exc
    if client_relative != "lark_channel/ws/client.py":
        raise ValueError("Channel SDK import path is unexpected")

    return SDKDistributionIdentity(
        distribution_name=name,
        version=distribution.version,
        lock_wheel_sha256=LOCKED_LARK_CHANNEL_WHEEL_SHA256,
        observed_wheel_archive_sha256=None,
        installed_record_sha256=_sha256(_canonical_json(sorted(verified, key=lambda value: value[0]))),
        runtime_payload_sha256=_sha256(_canonical_json(sorted(runtime_payload, key=lambda value: value[0]))),
        record_verified=True,
        project_lock_sha256=project_lock_sha256,
    )
