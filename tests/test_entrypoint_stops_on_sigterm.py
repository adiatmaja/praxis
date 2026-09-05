"""Both harness entrypoints stop promptly on SIGTERM and report the stop.

Measured in round 12 (probe 2): ``praxis stop`` took 33 s, because
``container.stop(timeout=30)`` sends SIGTERM to the entrypoint (PID 1 of the
container), bash defers a trap while a FOREGROUND command runs and, as PID 1,
ignores an unhandled TERM outright, so the agent ran on until Docker's SIGKILL.
A bare ``docker stop`` was worse: the harness exited 0 and reported
``completed`` with no PR, and the orchestrator had to fail it from the shape of
the callback.

The agent is therefore run in the BACKGROUND and waited on (``run_agent``),
which is the one shape in which a trap fires promptly; the TERM handler kills
the agent, says so in the log, and makes ``run_agent`` return 143, which the
EXIT trap turns into a ``failed`` callback.

The slice is extracted from the SHIPPED file, never copied here, and every
scenario runs against BOTH entrypoints (see ``test_entrypoint_reporting_honesty``
for why).
"""
# ruff: noqa: S101

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
ENTRYPOINTS = {
    "opencode": REPO / "docker" / "opencode-agent" / "entrypoint.sh",
    "agy": REPO / "docker" / "agy-agent" / "entrypoint.sh",
}


def _check_bash() -> str | None:
    bash_path = shutil.which("bash")
    if not bash_path:
        return None
    try:
        res = subprocess.run(  # noqa: S603
            [bash_path, "-c", "echo 1"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bash_path if res.returncode == 0 else None


_BASH = _check_bash()
pytestmark = pytest.mark.skipif(
    _BASH is None, reason="bash unavailable or non-functional"
)


def _slice(harness: str, start: str, stop: str) -> str:
    """Return the shipped lines from the ``start`` line through the ``stop`` line.

    Raises:
        AssertionError: If an anchor is missing. An empty slice would run zero
            lines and pass every assertion below.
    """
    lines = ENTRYPOINTS[harness].read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    started = False
    for line in lines:
        if not started and line == start:
            started = True
        if started:
            out.append(line)
            if line == stop:
                return "\n".join(out) + "\n"
    message = f"{harness}: slice anchors not found ({start!r} .. {stop!r})"
    raise AssertionError(message)


def _stop_machinery(harness: str) -> str:
    """The handler, the trap and ``run_agent``, as shipped."""
    return _slice(harness, "STOP_REQUESTED=0", "trap on_term TERM INT") + _slice(
        harness, "run_agent() {", "}"
    )


def _to_posix(path: Path) -> str:
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = f"/{text[0].lower()}{text[2:]}"
    return text


def _driver(harness: str, tmp_path: Path, agent_body: str) -> Path:
    log = tmp_path / f"{harness}-agent.log"
    script = tmp_path / f"{harness}-run.sh"
    script.write_text(
        "set -uo pipefail\n"
        + _stop_machinery(harness)
        + f"fake_agent() {{\n{agent_body}\n}}\n"
        + f'AGENT_LOG="{_to_posix(log)}"\n'
        + "run_agent fake_agent\n"
        + 'echo "rc=$?"\n',
        encoding="utf-8",
        newline="\n",
    )
    return script


@pytest.mark.parametrize("harness", sorted(ENTRYPOINTS))
def test_sigterm_stops_the_agent_within_seconds_and_says_so(
    harness: str, tmp_path: Path
) -> None:
    """The defect: TERM did nothing for 30 s, then SIGKILL, and no line said why."""
    script = _driver(
        harness,
        tmp_path,
        '    echo "agent running"\n    sleep 30\n    echo "agent survived the stop"',
    )
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        f'bash "{_to_posix(script)}" &\npid=$!\nsleep 1.5\nkill -TERM "$pid"\n'
        'wait "$pid"\necho "wrapper-done"\n',
        encoding="utf-8",
        newline="\n",
    )
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603
        [_BASH or "bash", str(wrapper)], capture_output=True, text=True, timeout=25
    )
    elapsed = time.monotonic() - started
    out = result.stdout + result.stderr
    assert "Received SIGTERM" in out, f"the stop was not reported: {out!r}"
    assert "rc=143" in out, f"run_agent did not return 143 after the stop: {out!r}"
    assert "agent survived the stop" not in out, "the agent ran on after the stop"
    assert elapsed < 10.0, f"the stop took {elapsed:.1f}s; the agent was not killed"


@pytest.mark.parametrize("harness", sorted(ENTRYPOINTS))
def test_run_agent_returns_the_agents_own_exit_status_not_tees(
    harness: str, tmp_path: Path
) -> None:
    """Backgrounding must not lose the exit code the whole state machine reads."""
    script = _driver(harness, tmp_path, '    echo "worked"\n    return 7')
    result = subprocess.run(  # noqa: S603
        [_BASH or "bash", str(script)], capture_output=True, text=True, timeout=20
    )
    out = result.stdout + result.stderr
    assert "rc=7" in out, out
    assert "worked" in out, "the agent's output must still stream to the log"
    assert (tmp_path / f"{harness}-agent.log").read_text(encoding="utf-8").strip() == (
        "worked"
    )
