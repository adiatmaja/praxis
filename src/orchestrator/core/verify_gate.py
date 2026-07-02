"""Run an operator-configured verification command against a PR checkout.

This is the deterministic gate that runs BEFORE the brain review: a non-zero
exit (failed typecheck / tests / lint) fails the task cheaply and reliably,
catching the compile/test failure class that a language model reviewing a diff
misses. The command is trusted operator config, never taken from a PR.
"""

from __future__ import annotations

import asyncio


_MAX_OUTPUT = 8000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    half = _MAX_OUTPUT // 2
    return f"{text[:half]}\n...[truncated]...\n{text[-half:]}"


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
    """
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
        return False, f"verify command timed out after {timeout:.0f}s"
    text = _truncate(out.decode(errors="replace"))
    return proc.returncode == 0, text
