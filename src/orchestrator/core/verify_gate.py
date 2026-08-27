"""Run an operator-configured verification command against a PR checkout.

This is the deterministic gate that runs BEFORE the brain review: a non-zero
exit (failed typecheck / tests / lint) fails the task cheaply and reliably,
catching the compile/test failure class that a language model reviewing a diff
misses. The command is trusted operator config, never taken from a PR.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any


logger = logging.getLogger(__name__)

# The two phrases a review's stored scope sentence is CLASSIFIED by, shared
# between the orchestrator that writes them and the CLI that reads them back.
#
# ``_scope_glance`` in the CLI deliberately parses the review's own sentence
# rather than re-deriving the verdict from other state, so the glance in the
# table can never disagree with the full statement printed beside it. That
# design makes the phrase itself a contract across two packages, and the day
# ``_GATE_UNATTRIBUTED`` was added the CLI kept matching only the passing
# phrase and reported a gate that RAN and went RED as "no gate", at the one
# surface a human reads before approving a merge.
#
# Living here, in the module that already owns "does this project have a verify
# command", is what makes that impossible: an edit to either constant turns
# BOTH the producer's and the CLI's tests red. This module imports nothing but
# the standard library, so the CLI can read it without pulling in the engine.
SCOPE_VERIFY_PASSED = "verify gate passed"
SCOPE_VERIFY_UNATTRIBUTED = "not attributed to this task"

# Why the leaf's OWN declared verification did not settle the attribution.
# Two facts with two different remedies, rendered identically until
# 2026-08-27 -- so an operator whose leaf DID declare a check was told it had
# declared none, and sent to write one the plan already contained.
#
# Here rather than at either producer because there are TWO of them (the merge
# gate's scope sentence and the no-change decline) spelling near-identical
# prose about one fact, which is the drift this module already exists to stop.
LEAF_CHECK_NONE = "this task declared no runnable verification of its own"
LEAF_CHECK_NONDISCRIMINATING = (
    "this task's declared verification is the project command itself, so "
    "re-running it could only restate a result already shown to be about the "
    "repository"
)

_MAX_OUTPUT = 8000


class FailureComparison(Enum):
    """How a failing head run compared with a failing base-branch run.

    Three values, not two, because "the two failed the same way" and "nobody
    could tell whether they failed the same way" are opposite facts that had
    been sharing one arm. Both decline to attribute, so the DIFFERENCE is not
    in what they license but in what they may be said to have established --
    the same rule :func:`base_comparison_unavailable` states one level up.
    """

    FAILED_ALIKE = "failed_alike"
    FAILED_DIFFERENTLY = "failed_differently"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class VerifyRun:
    """A verify command's verdict, its output, and the runner's own exit code.

    ``returncode`` is the third fact and the reason this is a class rather than
    the tuple it replaced. It is the ONLY signal available at this seam that
    distinguishes two failures without parsing their text, and it is the
    runner's own classification rather than an inference drawn over one.

    ``None`` means the runner never classified this run -- today only a
    TIMEOUT, where the process was killed. That distinction is load-bearing and
    was measured: on Windows a killed process reports returncode 1, which is
    exactly what ``pytest`` reports for "the tests failed". Passing it on would
    make a hung base branch and a failing head look like one failure, and a
    hung base against a collection error look like two.

    Iterating yields exactly ``(passed, output)``, so every ``passed, output =
    await run_verify(...)`` call site keeps working unchanged, and so does
    every test that mocks this seam with a plain 2-tuple. Those mocks then
    carry no exit code, :func:`run_exit_code` reports ``None``, and the
    comparison degrades to ``INCOMPARABLE`` -- which is precisely the behaviour
    that predates it, so a fixture that never thought about exit codes keeps
    asserting what it always asserted.
    """

    passed: bool
    output: str
    returncode: int | None = None

    def __iter__(self) -> Iterator[Any]:
        """Yield ``(passed, output)`` for the existing 2-tuple call sites."""
        yield self.passed
        yield self.output


def run_exit_code(run: Any) -> int | None:
    """The runner's own exit code for a verify run, or None when unknown.

    Tolerant on purpose, and the tolerance is the contract rather than a
    convenience: this seam is mocked with plain 2-tuples across the suite, and
    a tuple carries no code. Reporting ``None`` there routes those fixtures to
    ``INCOMPARABLE``, the answer that changes nothing.

    The hazard that tolerance creates is real and is guarded elsewhere: were
    :func:`run_verify` to stop carrying the code, every comparison would
    silently become ``INCOMPARABLE`` for ever, the arm that depends on it would
    never fire in production, and every hand-fed test would stay green.
    ``tests/test_verify_failure_comparison.py`` runs a REAL subprocess against
    a known exit code for exactly that reason.

    Args:
        run: A :class:`VerifyRun`, or any 2-tuple standing in for one.

    Returns:
        The exit code, or ``None`` when the run carries no classification.
    """
    return getattr(run, "returncode", None)


def compare_failures(head_code: int | None, base_code: int | None) -> FailureComparison:
    """Say whether two FAILING runs of one command failed the same way.

    Measured live on 2026-08-27: a review's head run and base run of the same
    ``python -m pytest src/playground -q``. The head RAN the suite and three
    assertions failed; the base never ran a test at all, interrupted by a
    collection ``ImportError``. The whole comparison at the time was
    ``base.status != "failed"``, so those two counted as identical and a
    genuine leaf regression was excused as pre-existing.

    **Why this compares exit codes and nothing else.** Output equality fails in
    the DANGEROUS direction, and six noise sources were measured on one runner
    on one platform before this was written: durations, first-run environment
    lines, progress lines that lengthen when a worker adds PASSING tests, count
    lines that move for the same reason, absolute checkout paths (the base run
    happens in a fresh temporary directory and the head run in the pull-request
    checkout, so they differ for an identical tree), and traceback traversals
    whose ``../`` depth follows that directory's depth. Any one of them makes
    two runs of one unchanged tree read as different failures, which would
    charge a worker for repository health -- the error this repository has
    already made once, and the worse of the two.

    **Why an exit code is a mechanism and not a special case.** It is the
    runner's OWN classification, so it needs no parser and knows no language.
    Where a runner draws the distinction the answer is real: ``pytest`` returns
    1 for "tests failed" and 2 for "interrupted during collection", which is
    exactly the measured case, and a shell ``A && B`` chain reports whichever
    stage fell over. Where a runner does NOT draw it -- ``go test`` and ``cargo
    test`` return one code for a build error and a test failure alike -- the
    two codes are equal and this answers ``FAILED_ALIKE``, costing a signal
    rather than inventing one. It degrades to a weaker answer for an unknown
    runner, never to a wrong one.

    Args:
        head_code: The head run's exit code, or None when it has no
            classification (a timeout, or a caller that could not supply one).
        base_code: The base-branch run's exit code, on the same terms.

    Returns:
        ``FAILED_DIFFERENTLY`` only on a positive, measured difference between
        two real non-zero codes. ``FAILED_ALIKE`` when they match.
        ``INCOMPARABLE`` whenever either side is missing -- and for a ZERO,
        which means the run PASSED and this function was asked the wrong
        question. That last case degrades rather than raising: every caller is
        mid-review, where an exception would fail a task on a bug in Praxis,
        and ``INCOMPARABLE`` is the behaviour that predates this comparison.
    """
    if head_code is None or base_code is None:
        return FailureComparison.INCOMPARABLE
    if head_code == 0 or base_code == 0:
        logger.warning(
            "compare_failures was asked about a run that PASSED "
            "(head=%s, base=%s); reporting the comparison as unavailable",
            head_code,
            base_code,
        )
        return FailureComparison.INCOMPARABLE
    if head_code == base_code:
        return FailureComparison.FAILED_ALIKE
    return FailureComparison.FAILED_DIFFERENTLY


def base_failure_clause(
    comparison: FailureComparison,
    base_branch: str,
    head_code: int | None,
    base_code: int | None,
) -> str:
    """Name what the base branch did, in words that claim only what was shown.

    One function for all three sentences, on the same ground
    :func:`base_comparison_unavailable` is one function for its seat: "the same
    command fails identically on X" is a CLAIM, and until 2026-08-27 it was
    printed for every red base including the ones nobody had compared. The
    seats that print it live in two modules and reach it by different routes,
    so a second copy is how they come to disagree about one fact.

    Args:
        comparison: What :func:`compare_failures` decided.
        base_branch: The branch the head was compared against.
        head_code: The head run's exit code, for the evidence in the sentence.
        base_code: The base run's exit code, likewise.

    Returns:
        A clause, not a sentence: each seat embeds it in wording naming what
        the failure would have been attributed TO (this task, this plan).
    """
    if comparison is FailureComparison.FAILED_ALIKE:
        return (
            f"the same command fails identically on {base_branch} "
            f"(both exited {head_code})"
        )
    if comparison is FailureComparison.FAILED_DIFFERENTLY:
        return (
            f"the same command also fails on {base_branch}, but DIFFERENTLY: "
            f"the head exited {head_code} and {base_branch} exited {base_code}, "
            f"so the redness there is not the redness here"
        )
    return (
        f"the same command also fails on {base_branch}, but the two failures "
        f"could not be told apart (head exit={head_code if head_code is not None else '-'}, "
        f"{base_branch} exit={base_code if base_code is not None else '-'})"
    )


def base_comparison_unavailable(
    base_branch: str, status: str, reason: str | None
) -> str:
    """Name the base-branch comparison that could NOT be made, and why.

    Several seats run the project's verify command on a head (a pull-request
    head, a plan branch mid-wave, a completed plan branch) and then ask the
    BASE BRANCH whether the redness pre-dates the work, because a red command
    is only evidence about the work when the same command is green on the
    branch the work was cut from. When that second run produces no ANSWER --
    an ``error``, or any skip -- every one of them fails closed, and every one
    has to say the comparison is missing rather than implying one was made. The
    sentence reaches a human at the merge gate, is injected verbatim into the
    next worker's prompt by ``core/worker_bible``, and rides the
    ``plan_verify_failed`` event, so a seat that quietly rephrased it would
    have near-identical claims drifting apart with nothing to grep.

    **Not every seat has adopted it yet, and the un-adopted one is invisible to
    a grep for this function.** ``attribute_wave_verify_failure`` in
    ``core/orchestrator_dispatch.py`` still spells the clause out by hand; the
    text is byte-identical today, so nothing can detect it drifting. Derive the
    real adopter list with ``rg -n "base_comparison_unavailable" src/`` and the
    candidate seats with
    ``rg -n "run_verify|_verify_plan_branch|verify_gate_disabled" src/``,
    rather than trusting a count here. Proving the sharing needs a CROSS-MODULE
    mutation: one edit to the returned string must turn every adopter's tests
    red, and a seat whose tests stay green has not adopted it.

    Lives here, beside ``normalize_verify_cmd``, on the same ground the two
    ``SCOPE_*`` phrases do: this module already owns the vocabulary of the
    verify gate, imports nothing but the standard library, and is already
    imported by every seat that needs this (``orchestrator_review`` and
    ``orchestrator_dispatch`` both import ``normalize_verify_cmd`` from it),
    so adopting it is a one-line change and can never be circular.

    Args:
        base_branch: The branch that could not answer.
        status: The base run's ``_PlanVerifyResult.status`` -- ``"error"`` or
            ``"skipped"``. Never ``"passed"`` or ``"failed"``: those ARE
            answers and their seats take other arms entirely.
        reason: The skip reason, when there was one. ``None`` and ``""`` both
            render as ``-`` rather than as an empty parenthesis, so the reader
            can tell "no reason was recorded" from a truncated sentence.

    Returns:
        A clause, not a sentence: each seat embeds it in wording naming what
        the failure would have been attributed TO (this task, this plan).
    """
    return (
        f"the same command could not be run on {base_branch} "
        f"(status={status}, reason={reason or '-'})"
    )


# pytest returns exit code 5 when it collected no tests. For docs-only or
# config-only leaves this is expected, not a failure: a verify_cmd ending in
# ``pytest`` would otherwise doom every no-test change into a re-dispatch loop.
# We treat exit 5 as a pass ONLY when the output carries pytest's own
# no-tests-collected signal, so an unrelated command exiting 5 still fails.
_PYTEST_NO_TESTS_EXIT = 5
_PYTEST_NO_TESTS_SIGNAL = re.compile(r"no tests ran|no tests collected", re.IGNORECASE)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    half = _MAX_OUTPUT // 2
    return f"{text[:half]}\n...[truncated]...\n{text[-half:]}"


def normalize_verify_cmd(value: str | None) -> str | None:
    """Collapse a not-really-configured verify command onto ``None``.

    The single source of truth for "does this project have a verify command".
    ``None`` and ``""`` already meant "not configured" at every read site,
    because both are falsy. An all-whitespace string is TRUTHY, so it sailed
    past those guards and reached the shell, where a blank command exits 0 and
    the gate reported ``passed`` having executed nothing. A gate that greens a
    task on evidence it never gathered is worse than no gate at all, so every
    read of ``projects.verify_cmd`` funnels through here and a blank takes the
    existing ``skipped`` path instead.

    The value is returned UNCHANGED rather than stripped: it is trusted
    operator config that is echoed verbatim into logs and the worker Bible, and
    a silently rewritten command is its own small lie.

    Args:
        value: The raw ``projects.verify_cmd`` column value.

    Returns:
        ``value`` when it carries at least one non-whitespace character,
        otherwise ``None``.
    """
    if value is None or not value.strip():
        return None
    return value


async def run_verify(
    checkout_dir: str, verify_cmd: str, timeout: float = 600.0
) -> VerifyRun:
    """Run ``verify_cmd`` in ``checkout_dir``; report verdict, output and code.

    Args:
        checkout_dir: Working directory (a checked-out PR head).
        verify_cmd: Shell command string (e.g. ``npx tsc --noEmit && npm test``).
        timeout: Seconds before the command is killed and reported as failed.

    Returns:
        A :class:`VerifyRun`. It still unpacks as ``(passed, output)``, so no
        existing call site or mock changes; ``returncode`` is the addition, and
        it is the runner's own classification of the failure that
        :func:`compare_failures` needs. It is ``None`` for a TIMEOUT, because a
        killed process's exit status is not a classification -- measured on
        Windows, where a kill yields 1, indistinguishable from "tests failed".

    Raises:
        ValueError: If ``verify_cmd`` is blank or whitespace-only. Unreachable
            in production, and deliberately so: every call site resolves the
            command through :func:`normalize_verify_cmd` and takes the
            ``skipped`` path when it comes back ``None``. This exists for the
            call site that has not been written yet. Without it, a future
            caller that forgets to normalize gets a shell that exits 0 and a
            gate that reports ``passed``; with it, that caller fails loudly
            with a traceback naming this function. Never downgraded to a
            ``(False, ...)`` return: a blank command is a programming error in
            the caller, not a failing verification, and reporting it as a
            failed check would burn a task's retries on a bug in Praxis.
    """
    if not verify_cmd.strip():
        msg = (
            f"run_verify was handed a blank verify command ({verify_cmd!r}). "
            "A blank shell command exits 0, which would report the verify gate "
            "as passed having executed nothing. Resolve the command through "
            "verify_gate.normalize_verify_cmd and take the skipped path when "
            "it returns None."
        )
        raise ValueError(msg)
    # This DOES run `verify_cmd` through a shell, deliberately: the gate exists
    # to run an operator-configured command line. Bandit's B602 does not fire
    # here (it does not model asyncio's shell helpers), so a `# nosec B602`
    # suppressed nothing and read as though a scanner were watching this line.
    # Nothing is: the operator-supplied string is the trust boundary.
    proc = await asyncio.create_subprocess_shell(
        verify_cmd,
        cwd=checkout_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("verify command timed out after %.0fs: %s", timeout, verify_cmd)
        # ``returncode`` is deliberately NOT reported here. The process was
        # killed, so whatever it now carries describes the kill and not the
        # work: on Windows that is 1, the very code pytest returns for "the
        # tests failed". Passing it on would let a hung branch compare equal to
        # a failing one, and unequal to a branch that could not even collect.
        return VerifyRun(False, f"verify command timed out after {timeout:.0f}s")
    raw = out.decode(errors="replace")
    text = _truncate(raw)
    code = proc.returncode
    if code == 0:
        return VerifyRun(True, text, code)
    if code == _PYTEST_NO_TESTS_EXIT and _PYTEST_NO_TESTS_SIGNAL.search(raw):
        logger.info(
            "verify command exited 5 with no tests collected; treating as a pass: %s",
            verify_cmd,
        )
        return VerifyRun(True, text, code)
    return VerifyRun(False, text, code)
