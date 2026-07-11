"""Failure-class taxonomy for worker task outcomes."""

from __future__ import annotations

from enum import StrEnum


class FailureClass(StrEnum):
    """Classification of why a worker task failed."""

    VERIFY_FAIL = "verify_fail"
    FIXABLE_IN_PLACE = "fixable_in_place"
    CONTEXT_OVERFLOW = "context_overflow"
    TOO_BROAD = "too_broad"
    NEEDS_STRONGER_MODEL = "needs_stronger_model"
    WORKER_BLOCKED = "worker_blocked"
    PROVIDER_ERROR = "provider_error"


_COUNTS_AGAINST_WORKER: frozenset[FailureClass] = frozenset(
    {
        FailureClass.VERIFY_FAIL,
        FailureClass.FIXABLE_IN_PLACE,
        FailureClass.CONTEXT_OVERFLOW,
        FailureClass.TOO_BROAD,
    }
)


def counts_against_worker(failure_class: FailureClass | str) -> bool:
    """Return True if the failure is attributable to the worker's capability."""
    return FailureClass(failure_class) in _COUNTS_AGAINST_WORKER
