"""Unit tests for the failure taxonomy enum and attribution helper."""

from __future__ import annotations

import pytest

from orchestrator.core.failure_taxonomy import FailureClass, counts_against_worker


class TestFailureClassValues:
    """Verify all seven enum members exist with correct string values."""

    def test_verify_fail(self) -> None:
        assert FailureClass.VERIFY_FAIL == "verify_fail"

    def test_fixable_in_place(self) -> None:
        assert FailureClass.FIXABLE_IN_PLACE == "fixable_in_place"

    def test_context_overflow(self) -> None:
        assert FailureClass.CONTEXT_OVERFLOW == "context_overflow"

    def test_too_broad(self) -> None:
        assert FailureClass.TOO_BROAD == "too_broad"

    def test_needs_stronger_model(self) -> None:
        assert FailureClass.NEEDS_STRONGER_MODEL == "needs_stronger_model"

    def test_worker_blocked(self) -> None:
        assert FailureClass.WORKER_BLOCKED == "worker_blocked"

    def test_provider_error(self) -> None:
        assert FailureClass.PROVIDER_ERROR == "provider_error"


class TestCountsAgainstWorker:
    """Parametrized attribution tests."""

    @pytest.mark.parametrize(
        ("failure_class", "expected"),
        [
            (FailureClass.VERIFY_FAIL, True),
            ("verify_fail", True),
            (FailureClass.FIXABLE_IN_PLACE, True),
            ("fixable_in_place", True),
            (FailureClass.CONTEXT_OVERFLOW, True),
            ("context_overflow", True),
            (FailureClass.TOO_BROAD, True),
            ("too_broad", True),
            (FailureClass.NEEDS_STRONGER_MODEL, False),
            ("needs_stronger_model", False),
            (FailureClass.WORKER_BLOCKED, False),
            ("worker_blocked", False),
            (FailureClass.PROVIDER_ERROR, False),
            ("provider_error", False),
        ],
    )
    def test_attribution(self, failure_class: FailureClass | str, expected: bool) -> None:
        assert counts_against_worker(failure_class) is expected
