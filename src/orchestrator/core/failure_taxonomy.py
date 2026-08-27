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
    # The worker produced NO changes and the work is provably still needed:
    # the branch the leaf was cut from does not carry an edit location the leaf
    # declared, or the project's verify command ran on that branch and refuted
    # the no-op. Written by the empty-diff path in ``orchestrator_review``,
    # which until 2026-08-26 recorded nothing at all -- so this was the one
    # failure shape the calibration set could never contain, and every rate
    # derived from it was computed over a denominator that excluded it.
    #
    # Minted rather than folded into a neighbour because both neighbours would
    # state something false. ``VERIFY_FAIL`` means the gate ran on the WORKER'S
    # OWN change and it failed; here there is no change, and on the
    # declared-path decline the gate may have passed outright. ``FIXABLE_IN_PLACE``
    # means retry-with-feedback is likely to work, which is the opposite of the
    # signal a worker that wrote nothing carries: ``leaf_triage`` reads zero
    # files touched as a push toward escalate or human. Recording either would
    # leave the table unable to separate "this model writes code that breaks the
    # build" from "this model writes no code at all".
    NO_OUTPUT = "no_output"
    # The worker's own RUN ended in failure: it self-reported ``failed`` to
    # ``api/internal.py`` and no change ever reached a review. Measured live on
    # 2026-08-26 (plan c03b3ff6, leaf 2): four attempts, ``triage_decision``
    # NULL throughout, and ``task_outcomes`` empty -- so the commonest failure a
    # worker produces was the one shape the calibration set could never contain.
    #
    # Minted rather than folded into either neighbour, because both would state
    # something false. ``NO_OUTPUT`` means the run SUCCEEDED and produced
    # nothing, and it is written only once that emptiness has been REFUTED (a
    # declared edit location absent, or the leaf's own verification failing);
    # neither fact is in hand here, and a run that failed may well have
    # committed and pushed before ``gh pr create`` aborted the script under
    # ``set -euo pipefail``. ``FIXABLE_IN_PLACE`` means retry-with-feedback will
    # probably work, which is a positive claim about a run whose output was
    # never judged at all. Keeping them apart is what lets ``summarize_outcomes``
    # separate "this model writes code that breaks the build" from "this model
    # writes no code" from "this model's runs do not finish".
    RUN_FAILED = "run_failed"


_COUNTS_AGAINST_WORKER: frozenset[FailureClass] = frozenset(
    {
        FailureClass.VERIFY_FAIL,
        FailureClass.FIXABLE_IN_PLACE,
        FailureClass.CONTEXT_OVERFLOW,
        FailureClass.TOO_BROAD,
        # Attributable, and it is the strongest evidence about worker capability
        # this table can hold. It is also the same line ``NoChangeDecision``
        # already draws: the two declines that produce this class are exactly
        # the two it marks ``worker_attributable``, which is what lets them buy
        # a triage brain call whose worst answer is terminal. A fact strong
        # enough to end a leaf permanently is strong enough to count in a rate.
        FailureClass.NO_OUTPUT,
        # Attributable for the reason the reviewer-error path is NOT: the worker
        # was handed the leaf, ran, and did not complete it. What is uncertain is
        # WHY, not whether the run is about the leaf. The one systematic cause
        # that is not about the worker -- the model endpoint never answering --
        # is peeled off upstream by ``is_provider_error`` over the container log
        # and never reaches this class.
        FailureClass.RUN_FAILED,
    }
)


def counts_against_worker(failure_class: FailureClass | str) -> bool:
    """Return True if the failure is attributable to the worker's capability."""
    return FailureClass(failure_class) in _COUNTS_AGAINST_WORKER
