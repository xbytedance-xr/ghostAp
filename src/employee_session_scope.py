"""Low-dependency typed outcome capture for employee session boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator


class EmployeeSessionOutcome(StrEnum):
    TIMEOUT = "timeout"
    CANCELED = "canceled"


@dataclass(slots=True)
class EmployeeSessionOutcomeCapture:
    outcome: EmployeeSessionOutcome | None = None


_CAPTURE: ContextVar[EmployeeSessionOutcomeCapture | None] = ContextVar(
    "employee_session_outcome_capture",
    default=None,
)


@contextmanager
def employee_session_outcome_capture() -> Iterator[EmployeeSessionOutcomeCapture]:
    if _CAPTURE.get() is not None:
        raise RuntimeError("nested employee outcome capture is forbidden")
    capture = EmployeeSessionOutcomeCapture()
    token = _CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _CAPTURE.reset(token)


def record_employee_session_outcome(outcome: str | EmployeeSessionOutcome) -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        capture.outcome = EmployeeSessionOutcome(outcome)


__all__ = [
    "EmployeeSessionOutcome",
    "EmployeeSessionOutcomeCapture",
    "employee_session_outcome_capture",
    "record_employee_session_outcome",
]
