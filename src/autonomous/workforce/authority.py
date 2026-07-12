"""Writer-authority fencing for the legacy Slock employee registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


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


class StaleAuthorityEpoch(RuntimeError):
    """A legacy mutation no longer owns the employee writer authority."""


class LegacyMutationGuard:
    """Validate legacy writes against a live authority snapshot."""

    _WRITABLE_MODES = frozenset(
        {AuthorityMode.LEGACY_WRITE, AuthorityMode.SHADOW_READ}
    )

    def __init__(
        self,
        snapshot_provider: Callable[[], AuthoritySnapshot],
        *,
        expected_epoch: int,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._expected_epoch = expected_epoch

    def assert_writable(
        self,
        operation: str,
        *,
        validated_epoch: int | None = None,
    ) -> int:
        snapshot = self._snapshot_provider()
        expected = (
            self._expected_epoch
            if validated_epoch is None
            else validated_epoch
        )
        if snapshot.epoch != expected or snapshot.mode not in self._WRITABLE_MODES:
            raise StaleAuthorityEpoch(
                f"legacy {operation} rejected: expected authority epoch "
                f"{expected}, got {snapshot.epoch} in {snapshot.mode.value}"
            )
        return snapshot.epoch
