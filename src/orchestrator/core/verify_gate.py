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

_MAX_OUTPUT = 8000


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
) -> tuple[bool, str]:
    """Run ``verify_cmd`` in ``checkout_dir``; return (passed, combined_output).

    Args:
        checkout_dir: Working directory (a checked-out PR head).
        verify_cmd: Shell command string (e.g. ``npx tsc --noEmit && npm test``).
        timeout: Seconds before the command is killed and reported as failed.

    Returns:
        (True, output) on exit 0; (False, output) on non-zero exit or timeout.

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
        return False, f"verify command timed out after {timeout:.0f}s"
    raw = out.decode(errors="replace")
    text = _truncate(raw)
    if proc.returncode == 0:
        return True, text
    if proc.returncode == _PYTEST_NO_TESTS_EXIT and _PYTEST_NO_TESTS_SIGNAL.search(raw):
        logger.info(
            "verify command exited 5 with no tests collected; treating as a pass: %s",
            verify_cmd,
        )
        return True, text
    return False, text
