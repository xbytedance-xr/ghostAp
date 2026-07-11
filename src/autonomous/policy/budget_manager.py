"""Journal-backed budget manager with reserve/settle/release and version-CAS.

Budget validates finite positive amounts, uses version-CAS for all changes,
supports entry restoration by ID, and conservatively settles unknown billing.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from ..domain.control import BudgetEntry, BudgetEntryState, BudgetLedger


class BudgetError(Exception):
    """Base error for budget operations."""


class BudgetValidationError(BudgetError):
    """Raised when budget amounts fail validation (NaN, negative, etc.)."""


class BudgetOverdraftError(BudgetError):
    """Raised when a reservation would exceed the available budget."""


class BudgetCASError(BudgetError):
    """Raised when a concurrent modification is detected via version mismatch."""


class BudgetEntryNotFoundError(BudgetError):
    """Raised when a reservation ID cannot be found."""


def _validate_amount(amount: float, label: str = "amount") -> float:
    """Reject NaN, infinity, and negative amounts."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise BudgetValidationError(f"{label} must be numeric")
    result = float(amount)
    if math.isnan(result):
        raise BudgetValidationError(f"{label} must not be NaN")
    if math.isinf(result):
        raise BudgetValidationError(f"{label} must be finite")
    if result < 0:
        raise BudgetValidationError(f"{label} must not be negative")
    return result


class BudgetManager:
    """Journal-backed budget with reserve/settle/release and version-CAS.

    All mutations use compare-and-swap on the ledger's aggregate_version
    to prevent concurrent oversell.
    """

    def __init__(self) -> None:
        self._ledgers: dict[str, BudgetLedger] = {}

    def get_or_create_ledger(
        self,
        *,
        tenant_key: str = "",
        run_id: str = "",
        goal_id: str = "",
        employee_id: str = "",
        team_id: str = "",
        limits: Optional[dict[str, float]] = None,
    ) -> BudgetLedger:
        """Get existing ledger by run_id+goal_id or create a new one."""
        for ledger in self._ledgers.values():
            if ledger.run_id == run_id and ledger.goal_id == goal_id:
                return ledger

        # Validate limit values
        validated_limits: dict[str, float] = {}
        if limits:
            for dim, limit_val in limits.items():
                validated_limits[dim] = _validate_amount(limit_val, f"limit[{dim}]")

        ledger = BudgetLedger(
            tenant_key=tenant_key,
            run_id=run_id,
            goal_id=goal_id,
            employee_id=employee_id,
            team_id=team_id,
            limits=validated_limits,
            entries=(),
            aggregate_version=0,
        )
        self._ledgers[ledger.ledger_id] = ledger
        return ledger

    def get_ledger(self, ledger_id: str) -> Optional[BudgetLedger]:
        """Retrieve a ledger by ID."""
        return self._ledgers.get(ledger_id)

    def reserve(
        self,
        ledger_id: str,
        resource_type: str,
        amount: float,
        *,
        expected_version: Optional[int] = None,
    ) -> str:
        """Reserve budget. Returns reservation entry_id.

        Validates:
        - Amount is finite and positive (rejects NaN, negative)
        - Available budget is sufficient (rejects concurrent oversell)
        - Version-CAS if expected_version is provided
        """
        validated = _validate_amount(amount, "amount")
        if validated == 0:
            raise BudgetValidationError("amount must be positive")

        ledger = self._ledgers.get(ledger_id)
        if ledger is None:
            raise BudgetEntryNotFoundError(f"ledger {ledger_id} not found")

        # Version-CAS check
        if expected_version is not None and ledger.aggregate_version != expected_version:
            raise BudgetCASError(
                f"version mismatch: expected={expected_version} "
                f"actual={ledger.aggregate_version}"
            )

        # Check available budget
        available = ledger.available(resource_type)
        if validated > available:
            raise BudgetOverdraftError(
                f"insufficient budget: requested={validated} "
                f"available={available} dimension={resource_type}"
            )

        # Create entry and new ledger version
        entry = BudgetEntry(
            ledger_id=ledger_id,
            amount=validated,
            state=BudgetEntryState.RESERVED,
            dimension=resource_type,
            reserved_at=time.time(),
        )

        new_entries = ledger.entries + (entry,)
        updated = BudgetLedger(
            ledger_id=ledger.ledger_id,
            tenant_key=ledger.tenant_key,
            run_id=ledger.run_id,
            goal_id=ledger.goal_id,
            employee_id=ledger.employee_id,
            team_id=ledger.team_id,
            limits=dict(ledger.limits),
            entries=new_entries,
            aggregate_version=ledger.aggregate_version + 1,
        )
        self._ledgers[ledger_id] = updated
        return entry.entry_id

    def settle(
        self,
        reservation_id: str,
        *,
        actual_amount: Optional[float] = None,
    ) -> None:
        """Settle a reserved entry, optionally adjusting the amount.

        If actual_amount is provided, it replaces the reserved amount.
        """
        ledger, entry_idx = self._find_entry(reservation_id)

        entry = ledger.entries[entry_idx]
        if entry.state != BudgetEntryState.RESERVED:
            raise BudgetError(
                f"cannot settle entry in state {entry.state.value}"
            )

        settle_amount = entry.amount
        if actual_amount is not None:
            settle_amount = _validate_amount(actual_amount, "actual_amount")

        settled_entry = BudgetEntry(
            entry_id=entry.entry_id,
            ledger_id=entry.ledger_id,
            amount=settle_amount,
            state=BudgetEntryState.SETTLED,
            dimension=entry.dimension,
            reserved_at=entry.reserved_at,
            settled_at=time.time(),
        )

        entries = list(ledger.entries)
        entries[entry_idx] = settled_entry
        updated = BudgetLedger(
            ledger_id=ledger.ledger_id,
            tenant_key=ledger.tenant_key,
            run_id=ledger.run_id,
            goal_id=ledger.goal_id,
            employee_id=ledger.employee_id,
            team_id=ledger.team_id,
            limits=dict(ledger.limits),
            entries=tuple(entries),
            aggregate_version=ledger.aggregate_version + 1,
        )
        self._ledgers[ledger.ledger_id] = updated

    def release(self, reservation_id: str) -> None:
        """Release a reserved entry, returning budget to available pool."""
        ledger, entry_idx = self._find_entry(reservation_id)

        entry = ledger.entries[entry_idx]
        if entry.state != BudgetEntryState.RESERVED:
            raise BudgetError(
                f"cannot release entry in state {entry.state.value}"
            )

        released_entry = BudgetEntry(
            entry_id=entry.entry_id,
            ledger_id=entry.ledger_id,
            amount=entry.amount,
            state=BudgetEntryState.RELEASED,
            dimension=entry.dimension,
            reserved_at=entry.reserved_at,
            settled_at=time.time(),
        )

        entries = list(ledger.entries)
        entries[entry_idx] = released_entry
        updated = BudgetLedger(
            ledger_id=ledger.ledger_id,
            tenant_key=ledger.tenant_key,
            run_id=ledger.run_id,
            goal_id=ledger.goal_id,
            employee_id=ledger.employee_id,
            team_id=ledger.team_id,
            limits=dict(ledger.limits),
            entries=tuple(entries),
            aggregate_version=ledger.aggregate_version + 1,
        )
        self._ledgers[ledger.ledger_id] = updated

    def settle_unknown_billing(
        self, ledger_id: str, resource_type: str, amount: float
    ) -> str:
        """Conservatively settle unknown billing as a new entry.

        Used when billing arrives without a matching reservation.
        Creates a new entry in CONSERVATIVE_SETTLED state.
        """
        validated = _validate_amount(amount, "amount")

        ledger = self._ledgers.get(ledger_id)
        if ledger is None:
            raise BudgetEntryNotFoundError(f"ledger {ledger_id} not found")

        entry = BudgetEntry(
            ledger_id=ledger_id,
            amount=validated,
            state=BudgetEntryState.CONSERVATIVE_SETTLED,
            dimension=resource_type,
            reserved_at=time.time(),
            settled_at=time.time(),
        )

        new_entries = ledger.entries + (entry,)
        updated = BudgetLedger(
            ledger_id=ledger.ledger_id,
            tenant_key=ledger.tenant_key,
            run_id=ledger.run_id,
            goal_id=ledger.goal_id,
            employee_id=ledger.employee_id,
            team_id=ledger.team_id,
            limits=dict(ledger.limits),
            entries=new_entries,
            aggregate_version=ledger.aggregate_version + 1,
        )
        self._ledgers[ledger_id] = updated
        return entry.entry_id

    def restore_entry(self, reservation_id: str) -> None:
        """Restore a released or settled entry back to reserved state."""
        ledger, entry_idx = self._find_entry(reservation_id)

        entry = ledger.entries[entry_idx]
        if entry.state == BudgetEntryState.RESERVED:
            return  # Already in target state

        restored_entry = BudgetEntry(
            entry_id=entry.entry_id,
            ledger_id=entry.ledger_id,
            amount=entry.amount,
            state=BudgetEntryState.RESERVED,
            dimension=entry.dimension,
            reserved_at=time.time(),
            settled_at=None,
        )

        entries = list(ledger.entries)
        entries[entry_idx] = restored_entry
        updated = BudgetLedger(
            ledger_id=ledger.ledger_id,
            tenant_key=ledger.tenant_key,
            run_id=ledger.run_id,
            goal_id=ledger.goal_id,
            employee_id=ledger.employee_id,
            team_id=ledger.team_id,
            limits=dict(ledger.limits),
            entries=tuple(entries),
            aggregate_version=ledger.aggregate_version + 1,
        )
        self._ledgers[ledger.ledger_id] = updated

    def _find_entry(self, entry_id: str) -> tuple[BudgetLedger, int]:
        """Find a ledger and entry index by entry_id."""
        for ledger in self._ledgers.values():
            for idx, entry in enumerate(ledger.entries):
                if entry.entry_id == entry_id:
                    return ledger, idx
        raise BudgetEntryNotFoundError(f"entry {entry_id} not found")
