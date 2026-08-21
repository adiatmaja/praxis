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

_MAX_OUTPUT = 8000

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
    proc = await asyncio.create_subprocess_shell(  # nosec B602
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
