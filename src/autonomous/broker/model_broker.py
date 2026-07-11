"""Model Broker - the only exit point for LLM model calls.

Enforces: budget reservation/settlement, rate-limiting, prompt-hash binding,
and ledger tracking for every model invocation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from ..domain import GoalActivationAuthorization, new_id
from ..policy.budget_manager import (
    BudgetManager,
    BudgetOverdraftError,
)


# ---------------------------------------------------------------------------
# ModelCall state machine
# ---------------------------------------------------------------------------


class ModelCallState(str, Enum):
    REQUESTED = "requested"
    EXECUTING = "executing"
    COMMITTED = "committed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ModelCall:
    """A single ledger entry for a model invocation."""

    call_id: str = field(default_factory=lambda: new_id("mcall"))
    state: ModelCallState = ModelCallState.REQUESTED
    model_id: str = ""
    prompt_hash: str = ""
    response_hash: str = ""
    budget_entry_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    cost_estimate: float = 0.0
    actual_cost: float = 0.0
    requested_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error: str = ""
    token_usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "state": self.state.value,
            "model_id": self.model_id,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "budget_entry_id": self.budget_entry_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "cost_estimate": self.cost_estimate,
            "actual_cost": self.actual_cost,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "token_usage": self.token_usage,
        }


@dataclass
class ModelCallResult:
    """Result of a model call through the broker."""

    success: bool
    call_id: str = ""
    response: dict = field(default_factory=dict)
    error: str = ""
    prompt_hash: str = ""
    response_hash: str = ""


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token-bucket rate limiter for model calls."""

    def __init__(self, max_calls_per_minute: int = 60, max_calls_per_hour: int = 1000):
        self._max_per_minute = max_calls_per_minute
        self._max_per_hour = max_calls_per_hour
        self._minute_window: list[float] = []
        self._hour_window: list[float] = []

    def check(self) -> bool:
        """Return True if a call is allowed."""
        now = time.time()
        self._minute_window = [t for t in self._minute_window if now - t < 60]
        self._hour_window = [t for t in self._hour_window if now - t < 3600]
        if len(self._minute_window) >= self._max_per_minute:
            return False
        if len(self._hour_window) >= self._max_per_hour:
            return False
        return True

    def record(self) -> None:
        """Record a call."""
        now = time.time()
        self._minute_window.append(now)
        self._hour_window.append(now)


# ---------------------------------------------------------------------------
# ModelBroker
# ---------------------------------------------------------------------------


class BudgetExhausted(Exception):
    """Raised when model call budget is exhausted."""


class RateLimited(Exception):
    """Raised when model calls are rate-limited."""


class AuthorizationInvalid(Exception):
    """Raised when authorization is expired or consumed."""


class ModelBroker:
    """Unique model invocation exit point with budget, rate-limit, and hash binding.

    Every model call goes through this broker. The broker:
    1. Validates authorization
    2. Checks rate limit
    3. Reserves budget BEFORE calling the model
    4. Executes the model call
    5. Settles budget AFTER (with actual cost)
    6. Records the call in the ledger with prompt/response hashes
    """

    def __init__(
        self,
        model_fn: Callable,
        budget_manager: BudgetManager,
        ledger_id: str,
        rate_limiter: Optional[RateLimiter] = None,
        default_model_id: str = "default",
        cost_per_call: float = 1.0,
    ):
        self._model_fn = model_fn
        self._budget_manager = budget_manager
        self._ledger_id = ledger_id
        self._rate_limiter = rate_limiter or RateLimiter()
        self._default_model_id = default_model_id
        self._cost_per_call = cost_per_call
        self._ledger: list[ModelCall] = []
        self._calls_by_id: dict[str, ModelCall] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call(
        self,
        authorization: GoalActivationAuthorization,
        prompt_ref: dict,
        model_id: Optional[str] = None,
        run_id: str = "",
        attempt_id: str = "",
    ) -> ModelCallResult:
        """Execute a model call with full safety pipeline.

        Args:
            authorization: Valid authorization for this goal execution.
            prompt_ref: The prompt data to send to the model.
            model_id: Optional model override. Falls back to default.
            run_id: Run context for ledger tracking.
            attempt_id: Attempt context for ledger tracking.

        Returns:
            ModelCallResult with response or error.
        """
        effective_model = model_id or self._default_model_id

        # 1. Validate authorization
        if not authorization.is_valid():
            return ModelCallResult(
                success=False,
                error="Authorization expired or consumed",
            )

        # 2. Rate limit check
        if not self._rate_limiter.check():
            return ModelCallResult(
                success=False,
                error="Rate limited",
            )

        # 3. Compute prompt hash for binding
        prompt_hash = _compute_hash(prompt_ref)

        # 4. Budget reservation
        try:
            budget_entry_id = self._budget_manager.reserve(
                self._ledger_id, "model_cost", self._cost_per_call
            )
        except BudgetOverdraftError:
            return ModelCallResult(
                success=False,
                error="Budget exhausted",
                prompt_hash=prompt_hash,
            )

        # 5. Create ledger entry in REQUESTED state
        call_entry = ModelCall(
            state=ModelCallState.REQUESTED,
            model_id=effective_model,
            prompt_hash=prompt_hash,
            budget_entry_id=budget_entry_id,
            run_id=run_id,
            attempt_id=attempt_id,
            cost_estimate=self._cost_per_call,
        )
        self._ledger.append(call_entry)
        self._calls_by_id[call_entry.call_id] = call_entry

        # 6. Transition to EXECUTING and invoke model
        call_entry.state = ModelCallState.EXECUTING
        self._rate_limiter.record()

        try:
            response = await self._model_fn(prompt_ref)
            response_data = response if isinstance(response, dict) else {"output": response}

            # 7. Compute response hash
            response_hash = _compute_hash(response_data)

            # 8. Settle budget with actual cost
            actual_cost = self._cost_per_call
            if isinstance(response, dict) and "usage" in response:
                usage = response["usage"]
                actual_cost = usage.get("total_tokens", 0) * 0.001  # rough estimate
                call_entry.token_usage = usage

            self._budget_manager.settle(
                budget_entry_id, actual_amount=actual_cost
            )

            # 9. Commit
            call_entry.state = ModelCallState.COMMITTED
            call_entry.response_hash = response_hash
            call_entry.actual_cost = actual_cost
            call_entry.completed_at = time.time()

            return ModelCallResult(
                success=True,
                call_id=call_entry.call_id,
                response=response_data,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
            )

        except Exception as exc:
            # Failed: release budget reservation
            self._budget_manager.release(budget_entry_id)
            call_entry.state = ModelCallState.FAILED
            call_entry.error = str(exc)
            call_entry.completed_at = time.time()

            return ModelCallResult(
                success=False,
                call_id=call_entry.call_id,
                error=str(exc),
                prompt_hash=prompt_hash,
            )

    # ------------------------------------------------------------------
    # Ledger queries
    # ------------------------------------------------------------------

    def get_call(self, call_id: str) -> Optional[ModelCall]:
        """Retrieve a specific model call by ID."""
        return self._calls_by_id.get(call_id)

    def get_ledger(self, run_id: Optional[str] = None) -> list[ModelCall]:
        """Get all model calls, optionally filtered by run_id."""
        if run_id is None:
            return list(self._ledger)
        return [c for c in self._ledger if c.run_id == run_id]

    def get_total_cost(self, run_id: Optional[str] = None) -> float:
        """Get total actual cost from committed calls."""
        calls = self.get_ledger(run_id)
        return sum(c.actual_cost for c in calls if c.state == ModelCallState.COMMITTED)

    def get_call_count(self, run_id: Optional[str] = None) -> int:
        """Count model calls (all states)."""
        return len(self.get_ledger(run_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_hash(data: Any) -> str:
    """Compute sha256 hex digest of JSON-serialized data."""
    raw = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()
